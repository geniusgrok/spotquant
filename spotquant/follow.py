"""Record buys that follow an entry preview and the sells that close a sleeve. No order is sent.

External BTC, a deposit, or a balance drop that no account sell explains stays
unknown. Sleeves that were previewed on the same signal day share one cohort:
their buys are one fill, and each sleeve records an equal part of it. The
Research control: the peak uses owned fills and completed daily highs only
when the daily bar opens strictly after the first fill. The fill day's high
and session quotes never raise the peak.
"""
from __future__ import annotations

from decimal import Decimal as D
from .model import DAY, ORIGIN, SMA_WINDOW, Model
from .preview import BASE_STEP, MIN_NOTIONAL
from .types import Blocked, Unknown, floor_step, number

def _rearm(model, position: dict, owner: dict, window: int) -> bool:
    """Only this durable touch sale permits rejoining the still-long book."""
    order = owner.get('order', {})
    return bool(order.get('side') == 'SELL' and order.get('type') == 'MARKET'
                and owner.get('rearm', {}).get(str(window)) is True
                and getattr(model, 'shadow_in', False) and not position.get('repair'))


def day_open(timestamp: int) -> int:
    if type(timestamp) is not int or timestamp < ORIGIN:
        raise Unknown('fill time is before the model origin')
    return ORIGIN + (timestamp - ORIGIN) // DAY * DAY


def _base_delta(trade: dict) -> D:
    """Coins added to the balance. A BTC commission is part of that change."""
    commission = trade['commission'] if trade.get('commission_asset') == 'BTC' else D(0)
    if trade['buyer']:
        net = trade['qty'] - commission
        if net <= 0:
            raise Unknown('trade quantity is not usable')
        return net
    return -(trade['qty'] + commission)


def advance(position: dict, step: dict, model: Model) -> dict:
    """Move one completed bar onto a recorded position. Flags match the model rules."""
    open_ms = step['open_ms']
    if open_ms < position['entry_open_ms']:
        return position
    if position['through'] is not None and open_ms <= position['through']:
        return position
    low = step.get('low')
    close = step['close']
    peak = D(position['peak'])
    repair = position['repair']
    repair_peak = None if position['repair_peak'] is None else D(position['repair_peak'])
    prior_stop = peak * (D(1) - model.trail)
    breached = low is not None and low <= prior_stop
    if open_ms > position['first_ms']:
        peak = max(peak, step['high'])
        if repair and repair_peak is not None:
            repair_peak = max(repair_peak, step['high'])
    stop = peak * (D(1) - model.trail)
    if breached:
        protection = 'breached'
    elif close <= stop:
        protection = 'through_close'
    else:
        protection = 'resting'
    if repair:
        cap_high = step['cap_high']
        if (step['bull'] and cap_high is not None and model.cap_hand > 0
                and close >= cap_high * (D(1) - model.cap_hand)):
            repair = False
            repair_peak = None
    entry = D(position['entry_fill'])
    adverse = bool(
        not repair and model.adverse_stop > 0
        and close <= entry * (D(1) - model.adverse_stop)
    )
    updated = dict(position)
    updated['peak'] = format(peak, 'f')
    updated['repair'] = repair
    updated['repair_peak'] = None if repair_peak is None else format(repair_peak, 'f')
    updated['adverse'] = adverse
    updated['through'] = open_ms
    updated['protection'] = protection
    return updated


def replay(bars, *, entry_fill: D, first_ms: int, repair: bool, window: int = SMA_WINDOW) -> dict:
    """Replay completed bars so a fill adopted later still has the right flags."""
    model = Model(window)
    position = {
        'entry_fill': format(entry_fill, 'f'),
        'first_ms': first_ms,
        'entry_open_ms': day_open(first_ms),
        'peak': format(entry_fill, 'f'),
        'repair': repair,
        'repair_peak': format(entry_fill, 'f') if repair else None,
        'adverse': False,
        'through': None,
        'protection': 'resting',
    }
    for open_ms, _open, high, _low, close in bars:
        model.advance_open(open_ms, _open)
        model.update(open_ms, high, _low, close)
        position = advance(position, {
            'open_ms': open_ms,
            'high': high,
            'close': close,
            'bull': model.bull,
            'cap_high': model._view_cap_high(),
        }, model)
    return position


def normalize_trade(row: dict) -> dict:
    if not isinstance(row, dict):
        raise Unknown('trade row is incomplete')
    try:
        if 'commission' not in row or 'isBuyer' not in row or 'orderId' not in row:
            raise Unknown('trade row is incomplete')
        if type(row['isBuyer']) is not bool:
            raise Unknown('trade row is incomplete')
        commission = number(row['commission'], 'commission', nonnegative=True)
        trade_id = int(row['id'])
        order_id = int(row['orderId'])
        trade_time = int(row['time'])
        qty = number(row['qty'], 'qty', positive=True)
        quote = number(row['quoteQty'], 'quote', positive=True)
        price = number(row['price'], 'price', positive=True)
    except (KeyError, TypeError, ValueError, Blocked) as exc:
        raise Unknown('trade row is incomplete') from exc
    if commission > 0 and not row.get('commissionAsset'):
        raise Unknown('trade row is incomplete')
    return {
        'id': trade_id,
        'order_id': order_id,
        'time': trade_time,
        'qty': qty,
        'quote': quote,
        'price': price,
        'buyer': row['isBuyer'],
        'commission': commission,
        'commission_asset': row.get('commissionAsset'),
    }


def _one_order(trades) -> None:
    orders = {int(trade['order_id']) for trade in trades}
    if len(orders) != 1:
        raise Unknown('trades from more than one order have no recorded sleeve; refusing new risk')


def _region_buy(buys):
    """The net buy of one order; the caller supplies nonempty, buy-only fills."""
    _one_order(buys)
    gross = sum((trade['qty'] for trade in buys), D(0))
    quote = sum((trade['quote'] for trade in buys), D(0))
    net = sum((_base_delta(trade) for trade in buys), D(0))
    if gross <= 0 or quote <= 0 or net <= 0:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    return {
        'qty': net,
        'entry_fill': quote / gross,
        'first_ms': min(trade['time'] for trade in buys),
        'ids': [trade['id'] for trade in buys],
    }


def _closed(held: dict, sells: list, sold: D, tolerance: D):
    """An unallocated account sale must close the sole recorded position."""
    _one_order(sells)
    if len(held) != 1:
        raise Unknown('a sell on the account does not match one recorded sleeve group; refusing new risk')
    window, position = next(iter(held.items()))
    if (abs(D(position['qty']) - sold) > tolerance
            or any(trade['time'] < int(position['first_ms']) for trade in sells)):
        raise Unknown('a sell on the account does not match one recorded sleeve group; refusing new risk')
    return [window]


def apply_day(models: dict, positions: dict, follows: dict, accounted: set, open_ms: int,
              trades, history, owners=None) -> tuple[dict, dict, set, list]:
    """Apply one UTC day's fills before that day's bar updates the model.

    A sell closes the matching sleeves and consumes their entry signal before any
    later bar. The sleeve that sold does not buy again that day. Another flat
    sleeve can. Two orders, or two sleeve
    groups of the same size, are unknown. Trade ids already accounted are ignored,
    including a second fill that shares the first fill's millisecond.
    """
    positions = dict(positions)
    follows = dict(follows)
    accounted = set(accounted)
    day = [
        trade for trade in trades
        if trade['id'] not in accounted and day_open(trade['time']) == open_ms
    ]
    day.sort(key=lambda trade: (trade['time'], trade['id']))
    if owners is not None:
        return _owned_fills(models, positions, follows, accounted, day, owners, history)
    held = {window: item for window, item in positions.items() if item is not None}
    tolerance = BASE_STEP * (len(positions) + 1)
    closed = []
    sells = [trade for trade in day if not trade['buyer']]
    sold = sum((-_base_delta(trade) for trade in sells), D(0))
    if sold > tolerance:
        if not held:
            raise Unknown('a sell on the account does not match one recorded sleeve group; refusing new risk')
        closed = _closed(held, sells, sold, tolerance)
        for window in closed:
            # A read-only account sale has no durable touch-exit permission.
            models[window].note_flat(rearm=False)
            positions[window] = None
            follows[window] = None
            held.pop(window)
        accounted.update(trade['id'] for trade in sells)
    active = {
        window: item for window, item in follows.items()
        if positions.get(window) is None and item and item.get('signal_ms') is not None
        and int(item['signal_ms']) < open_ms
    }
    cohorts = sorted({int(item['signal_ms']) for item in active.values()})
    buys = [trade for trade in day if trade['buyer'] and trade['id'] not in accounted]
    if not buys or not cohorts:
        return positions, follows, accounted, closed
    # One signal day owns the buys. A second cohort on the same fill day is unknown.
    if len(cohorts) != 1:
        raise Unknown('trades from more than one order have no recorded sleeve; refusing new risk')
    found = _region_buy(buys)
    group = sorted(window for window, item in active.items() if int(item['signal_ms']) == cohorts[0])
    bars = history()
    share = floor_step(found['qty'] / len(group), BASE_STEP)
    given = D(0)
    for order, window in enumerate(group):
        qty = found['qty'] - given if order == len(group) - 1 else share
        given += qty
        built = replay(
            bars, entry_fill=found['entry_fill'], first_ms=found['first_ms'],
            repair=bool(active[window].get('repair')), window=window,
        )
        built['qty'] = format(qty, 'f')
        positions[window] = built
        follows[window] = None
    accounted.update(found['ids'])
    return positions, follows, accounted, closed


def _owned_fills(models, positions, follows, accounted, trades, owners, history):
    """Attribute fills by the order's durable allocation, including partial fills."""
    closed = []
    for trade in trades:
        owner = owners.get(str(trade['order_id']))
        if owner is None:
            raise Unknown('account trade has no durable order allocation')
        group = owner['sleeves']
        weights = {int(k): D(v) for k, v in owner['weights'].items()}
        total = sum(weights.values(), D(0))
        if total <= 0 or set(weights) != set(group):
            raise Unknown('invalid durable sleeve allocation')
        delta = abs(_base_delta(trade))
        given = D(0)
        gross_given = D(0)
        for index, window in enumerate(group):
            qty = delta - given if index == len(group) - 1 else delta * weights[window] / total
            given += qty
            prior = positions.get(window)
            if trade['buyer']:
                gross_qty = (trade['qty'] - gross_given if index == len(group) - 1
                             else trade['qty'] * weights[window] / total)
                gross_given += gross_qty
                if prior is None or prior.get('dust'):
                    built = replay(history(), entry_fill=trade['price'], first_ms=trade['time'],
                                   repair=bool(owner.get('repair', {}).get(str(window))), window=window)
                    old_qty = D(prior['qty']) if prior else D(0)
                    if old_qty:
                        built['entry_fill'] = format((D(prior['entry_fill']) * old_qty + trade['price'] * gross_qty)
                                                    / (old_qty + gross_qty), 'f')
                    # A new entry keeps only the retained dust's cost basis,
                    # never the original closed buy's full gross allocation.
                    built['entry_gross_qty'] = format(old_qty + gross_qty, 'f')
                    built['qty'] = format(old_qty + qty, 'f')
                    positions[window] = built
                else:
                    old_qty = D(prior['qty'])
                    if prior.get('entry_gross_qty') is None:
                        raise Unknown('partial buy has no durable gross fill amount')
                    old_gross = number(prior['entry_gross_qty'], 'entry gross quantity', positive=True)
                    built = dict(prior)
                    built['entry_fill'] = format((D(prior['entry_fill']) * old_gross + trade['price'] * gross_qty)
                                                / (old_gross + gross_qty), 'f')
                    built['entry_gross_qty'] = format(old_gross + gross_qty, 'f')
                    built['qty'] = format(old_qty + qty, 'f')
                    built['peak'] = format(max(D(prior['peak']), trade['price']), 'f')
                    if prior['repair'] and prior['repair_peak'] is not None:
                        built['repair_peak'] = format(max(D(prior['repair_peak']), trade['price']), 'f')
                    positions[window] = built
                follows[window] = None
            else:
                if prior is None or qty > D(prior['qty']) + BASE_STEP:
                    raise Unknown('allocated sell exceeds its recorded sleeve')
                remaining = max(D(0), D(prior['qty']) - qty)
                if remaining < BASE_STEP:
                    # A floored native sell does not remove fractional coins.
                    # Retain their proven sleeve ownership across restarts and
                    # reuse it at the next genuine entry; never infer a deposit.
                    positions[window] = (dict(prior, qty=format(remaining, 'f'), dust=True,
                                              protection='unplaceable_dust') if remaining else None)
                    follows[window] = None
                    models[window].note_flat(rearm=_rearm(models[window], prior, owner, window))
                    closed.append(window)
                else:
                    positions[window] = dict(prior, qty=format(remaining, 'f'))
            accounted.add(trade['id'])
    return positions, follows, accounted, closed


def unexplained(positions: dict, balance: D, mark: D) -> None:
    """A material gap between the recorded sleeves and the balance is unknown."""
    tolerance = BASE_STEP * (len(positions) + 1)
    recorded = sum((D(item['qty']) for item in positions.values() if item is not None), D(0))
    gap = balance - recorded
    if abs(gap) > tolerance and abs(gap) * mark >= MIN_NOTIONAL:
        if gap > 0:
            raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
        raise Unknown('BTC balance does not match the recorded spotquant fills; refusing new risk')
