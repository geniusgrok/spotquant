"""Record buys that follow an entry preview and the sells that close a sleeve. No order is sent.

External BTC, a deposit, or a balance drop that no account sell explains stays
unknown. Sleeves that were previewed on the same signal day share one cohort:
their buys are one fill, and each sleeve records an equal part of it. The
recorded peak starts at the fill. A daily high is included only when that bar
opens after the fill, or when the fill is within the first minute of the bar.
A wick from before the fill is not the stop.
"""
from __future__ import annotations

from decimal import Decimal as D
from itertools import combinations

from .model import DAY, ORIGIN, SMA_WINDOW, Model
from .preview import BASE_STEP, MIN_NOTIONAL
from .types import Blocked, Unknown, floor_step, number

OPEN_FILL_MS = 60_000


def day_open(timestamp: int) -> int:
    if type(timestamp) is not int or timestamp < ORIGIN:
        raise Unknown('fill time is before the model origin')
    return ORIGIN + (timestamp - ORIGIN) // DAY * DAY


def _high_counts(open_ms: int, first_ms: int) -> bool:
    if open_ms > first_ms:
        return True
    return open_ms <= first_ms < open_ms + OPEN_FILL_MS


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
    high = step['high']
    close = step['close']
    peak = D(position['peak'])
    repair = position['repair']
    repair_peak = None if position['repair_peak'] is None else D(position['repair_peak'])
    if _high_counts(open_ms, position['first_ms']):
        peak = max(peak, high)
        if repair and repair_peak is not None:
            repair_peak = max(repair_peak, high)
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
    }
    for open_ms, high, _low, close in bars:
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
        commission = number(row.get('commission', '0'), 'commission')
        trade_id = int(row['id'])
        trade_time = int(row['time'])
        qty = number(row['qty'], 'qty', positive=True)
        quote = number(row['quoteQty'], 'quote', positive=True)
        price = number(row['price'], 'price', positive=True)
    except (KeyError, TypeError, ValueError, Blocked) as exc:
        raise Unknown('trade row is incomplete') from exc
    return {
        'id': trade_id,
        'time': trade_time,
        'qty': qty,
        'quote': quote,
        'price': price,
        'buyer': row.get('isBuyer') is True,
        'commission': commission,
        'commission_asset': row.get('commissionAsset'),
    }


def _out(trade: dict) -> D:
    """Coins that left the balance in a sell, including a BTC commission."""
    commission = trade['commission'] if trade.get('commission_asset') == 'BTC' else D(0)
    return trade['qty'] + commission


def _region_buy(region):
    """The net buy after the last sell in a cohort's time region, or None."""
    last_sell = None
    for index, trade in enumerate(region):
        if not trade['buyer']:
            last_sell = index
    if last_sell is not None:
        region = region[last_sell + 1:]
    if not region:
        return None
    gross = sum((trade['qty'] for trade in region), D(0))
    quote = sum((trade['quote'] for trade in region), D(0))
    net = sum((_base_delta(trade) for trade in region), D(0))
    if gross <= 0 or quote <= 0 or net <= 0:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    return {'qty': net, 'entry_fill': quote / gross, 'first_ms': min(trade['time'] for trade in region)}


def _closed(held: dict, sells: list, sold: D, flagged, tolerance: D):
    """The sleeves whose coins the sells left. An unflagged tie is unknown."""
    best_score = None
    best = []
    for size in range(1, len(held) + 1):
        for group in combinations(sorted(held), size):
            total = sum((D(held[window]['qty']) for window in group), D(0))
            if abs(total - sold) > tolerance:
                continue
            earliest = min(int(held[window]['first_ms']) for window in group)
            if any(trade['time'] < earliest for trade in sells):
                continue
            score = sum(1 for window in group if window in flagged)
            if best_score is None or score > best_score:
                best_score, best = score, [group]
            elif score == best_score:
                best.append(group)
    if not best:
        raise Unknown('a sell on the account does not match any recorded sleeve; refusing new risk')
    if len(best) > 1 and best_score == 0:
        raise Unknown('a sell on the account fits more than one sleeve; refusing new risk')
    return list(best[0])


def reconcile(positions: dict, follows: dict, trades, balance: D, mark: D, ledger_ms, flagged, history):
    """Match the account to the recorded sleeves. Returns positions, follows, ledger_ms, closed.

    A sell after the last accounted trade must leave exactly the coins of some recorded
    sleeves. A buy after a previewed entry is adopted per signal day. Any balance that
    the recorded sleeves and those buys do not explain, above the minimum notional, is
    unknown. ``history`` is called only when a cohort is adopted.
    """
    positions = dict(positions)
    follows = dict(follows)
    trades = sorted(trades, key=lambda trade: (trade['time'], trade.get('id', 0)))
    held = {window: item for window, item in positions.items() if item is not None}
    tolerance = BASE_STEP * (len(positions) + 1)
    closed = []
    if held:
        floor = min(int(item['first_ms']) for item in held.values())
        if ledger_ms is not None:
            floor = max(floor, int(ledger_ms) + 1)
        sells = [trade for trade in trades if not trade['buyer'] and trade['time'] >= floor]
        sold = sum((_out(trade) for trade in sells), D(0))
        if sold > tolerance:
            closed = _closed(held, sells, sold, flagged, tolerance)
            for window in closed:
                positions[window] = None
                follows[window] = None
                held.pop(window)
            ledger_ms = max(trade['time'] for trade in sells)
    active = {
        window: item for window, item in follows.items()
        if positions.get(window) is None and item and item.get('signal_ms') is not None
    }
    cohorts = sorted({int(item['signal_ms']) for item in active.values()})
    adopted = {}
    for index, signal in enumerate(cohorts):
        start = signal + DAY
        end = cohorts[index + 1] + DAY if index + 1 < len(cohorts) else None
        region = [trade for trade in trades if trade['time'] >= start and (end is None or trade['time'] < end)]
        found = _region_buy(region)
        if found is not None:
            adopted[signal] = found
    recorded = sum((D(item['qty']) for item in held.values()), D(0))
    expected = recorded + sum((item['qty'] for item in adopted.values()), D(0))
    gap = balance - expected
    if abs(gap) > tolerance and abs(gap) * mark >= MIN_NOTIONAL:
        if gap > 0:
            raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
        raise Unknown('BTC balance does not match the recorded spotquant fills; refusing new risk')
    if adopted:
        bars = history()
    for signal, found in adopted.items():
        group = sorted(window for window, item in active.items() if int(item['signal_ms']) == signal)
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
    return positions, follows, ledger_ms, closed
