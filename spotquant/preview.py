"""Read-only spot decision. No order is built into a network request here."""
from __future__ import annotations

import copy
from decimal import Decimal as D

from .model import DAY, Model, percent
from .types import Unknown, floor_step

MIN_NOTIONAL = D('5')
QUOTE_STEP = D('0.01')
BASE_STEP = D('0.00001')
MIN_QTY = D('0.00001')
PRICE_STEP = D('0.01')


def preview(model: Model, snapshot: dict, *, entries_enabled: bool, capital_limit: D | None,
            owned_btc: D = D(0)) -> dict:
    """Say what a qualified executor would submit. The caller must not submit it.

    ``owned_btc`` is coins this system has a confirmed fill for. Any other BTC
    at or above the minimum notional is external and blocks a new order.
    """
    if model.close is None:
        return _flat('no completed daily close is available')
    price = model.close
    btc = D(snapshot['btc'])
    external = btc - owned_btc
    if external * price >= MIN_NOTIONAL:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    adding = entries_enabled and not snapshot.get('open_orders')
    spend = D(snapshot['usdt_free'])
    if capital_limit is not None:
        spend = min(spend, max(capital_limit - owned_btc * price, D(0)))
    decision = _decide(model, snapshot, entries_enabled=adding, owned_btc=owned_btc, budget=spend)
    if snapshot.get('open_orders') and decision['action'] == 'flat':
        decision['reason'] = decision['reason'] + '; an open order blocks a new buy'
    return decision


def _decide(model: Model, snapshot: dict, *, entries_enabled: bool, owned_btc: D, budget: D) -> dict:
    """One sleeve. ``budget`` is the USDT this sleeve may spend on an entry."""
    price = model.close
    owned = floor_step(owned_btc, BASE_STEP)
    if owned > 0:
        return _position_decision(model, snapshot, owned, price)
    armed = model.enter or model.cap_enter
    if not armed:
        if model.sma is None:
            return _flat('SMA warmup is incomplete')
        if not model.bull:
            return _flat('completed daily close is not above its SMA')
        return _flat('entry is not armed: confirmation, fresh cross, crash filter, or crash reversal')
    if not entries_enabled:
        return _flat('cold start: no completed daily close after the first checkpoint')
    spend = floor_step(max(budget, D(0)), QUOTE_STEP)
    if spend < MIN_NOTIONAL:
        return _flat('the sleeve budget is below the 5 USDT minimum notional')
    return {
        'action': 'enter',
        'reason': (
            (
                f'crash reversal: {percent(model.cap_drop)} down, then {percent(model.cap_bounce)} up, '
                f'still at least {percent(model.cap_depth)} under the {model.cap_window}-day high'
            )
            if model.cap_enter
            else 'two confirmed closes cleared the fresh-cross and crash filters'
        ),
        'order': {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'type': 'MARKET',
            'quoteOrderQty': _step(spend, QUOTE_STEP),
        },
        'protection': _protection(model, None, snapshot),
    }


def _position_decision(model: Model, snapshot: dict, owned: D, price: D) -> dict:
    """A sleeve with any coins is a position, even below the minimum notional.

    Dust is not a flat sleeve and is not a place to add risk. An exit is reported
    even when this sleeve alone cannot meet the minimum; the portfolio aggregates
    those exits before the minimum is applied.
    """
    breached = getattr(model, 'protection', 'resting') in ('breached', 'through_close')
    stop_through = model.position_peak is not None and model.stop_price(model.position_peak) >= price
    if breached or stop_through:
        return _exit(
            model, owned,
            'the resting stop is already through the completed close; the sell is the next open',
        )
    if model.repair:
        return {
            'action': 'hold',
            'reason': (
                f'crash-reversal hold; SMA, blow-off, and the {percent(model.adverse_stop)} close '
                'stay off until the handoff'
            ),
            'order': None,
            'protection': _protection(model, owned, snapshot),
        }
    if model.adverse:
        return _exit(
            model, owned,
            (
                f'completed daily close is at least {percent(model.adverse_stop)} under the entry fill; '
                f'the sell is the next open, so the loss is not capped at {percent(model.adverse_stop)}'
            ),
        )
    if model.extended:
        return _exit(
            model, owned,
            f'completed daily close is extended at least {percent(model.extend)} above its SMA',
        )
    if not model.bull:
        return _exit(model, owned, 'completed daily close is not above its SMA')
    if model.position_peak is not None:
        reason = (
            f'still above the SMA; protection is a stop {percent(model.trail)} '
            'under the high since the fill'
        )
    else:
        reason = (
            f'still above the SMA; no fill is recorded, so the stop stays {percent(model.trail)} '
            'under the completed close'
        )
    return {
        'action': 'hold',
        'reason': reason,
        'order': None,
        'protection': _protection(model, owned, snapshot),
    }


def _exit(model: Model, owned: D, reason: str) -> dict:
    tradable = owned * model.close >= MIN_NOTIONAL
    order = None
    if tradable:
        order = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': _step(owned, BASE_STEP)}
    return {
        'action': 'exit',
        'reason': reason if tradable else reason + '; this sleeve is below the minimum notional until it is aggregated',
        'loss_capped': False,
        'untradeable': not tradable,
        'order': order,
        'protection': None,
    }


def _protection(model: Model, quantity: D | None, snapshot: dict) -> dict:
    # No recorded fill yet. The bullish-streak high can start before the buy,
    # so it is not the stop. A crash reversal is still a repair hold on the
    # meter when the ordinary entry is also armed.
    trail = percent(model.trail)
    if quantity is None:
        peak = model.close
        if model.cap_enter:
            note = (
                f'quantity would be the filled base amount; crash-reversal stop starts {trail} under '
                'the completed close and is amended to the fill'
            )
        else:
            note = (
                f'quantity would be the filled base amount; stop starts {trail} under the completed '
                'close and is amended to the fill'
            )
    elif model.repair and model.repair_peak is not None:
        peak = model.repair_peak
        note = f'amended STOP_LOSS; {trail} under the high since the repair fill'
    elif model.position_peak is not None:
        peak = model.position_peak
        note = f'amended STOP_LOSS; {trail} under the high since the fill'
    else:
        peak = model.close
        note = f'amended STOP_LOSS; {trail} under the completed close until a fill is recorded'
    order = {
        'symbol': 'BTCUSDT',
        'side': 'SELL',
        'type': 'STOP_LOSS',
        'stopPrice': _step(model.stop_price(peak), PRICE_STEP),
        'note': note,
    }
    if quantity is not None:
        order['quantity'] = _step(quantity, BASE_STEP)
    _annotate_venue(order, model, snapshot)
    return order


def _annotate_venue(order: dict, model: Model, snapshot: dict) -> None:
    """Say whether this STOP_LOSS can be rested. Do not add a limit price.

    trailingDelta's maximum does not decide a fixed stopPrice. The sell band
    applies to a limit price, which this order does not have.
    """
    known = any(snapshot.get(key) is not None for key in (
        'min_price', 'max_price', 'avg_price', 'min_qty', 'trailing_max_bips',
    ))
    if not known:
        return
    trailing = snapshot.get('trailing_max_bips')
    if trailing is not None:
        bips = int((model.trail * D(10000)).to_integral_value())
        order['trailing_placeable'] = bips <= int(trailing)
        if not order['trailing_placeable']:
            order['note'] += '. trailingDelta cannot express this distance; this order is a fixed stopPrice'
    order['limit_placeable'] = None
    stop = D(order['stopPrice'])
    price_ok = True
    if snapshot.get('min_price') is not None and stop < D(snapshot['min_price']):
        price_ok = False
    if snapshot.get('max_price') is not None and stop > D(snapshot['max_price']):
        price_ok = False
    notional_ok = True
    reference = snapshot.get('avg_price') or model.close
    if 'quantity' in order and reference is not None:
        notional_ok = D(order['quantity']) * D(reference) >= MIN_NOTIONAL
        maximum = snapshot.get('max_notional')
        if maximum is not None and D(order['quantity']) * D(reference) > D(maximum):
            notional_ok = False
    qty_ok = _qty_ok(order.get('quantity'), snapshot)
    order['placeable'] = bool(price_ok and notional_ok and qty_ok)
    if 'price' in order:
        del order['price']


def _qty_ok(quantity, snapshot: dict) -> bool:
    if quantity is None:
        return True
    qty = D(quantity)
    for key in ('min_qty', 'market_min_qty'):
        if snapshot.get(key) is not None and qty < D(snapshot[key]):
            return False
    for key in ('max_qty', 'market_max_qty'):
        if snapshot.get(key) is not None and qty > D(snapshot[key]):
            return False
    return True


def _step(value: D, step: D) -> str:
    return format(floor_step(value, step).quantize(step), 'f')


def _flat(reason: str) -> dict:
    return {'action': 'flat', 'reason': reason, 'order': None, 'protection': None}


def portfolio(views: dict, owned: dict, snapshot: dict, *, entries_enabled: bool,
              capital_limit: D | None, consensus: bool = True) -> dict:
    """The sleeves book. ``views`` maps a window to its model, ``owned`` to its recorded coins.

    An armed sleeve would spend the free USDT plus the proceeds of this open's exits, divided
    by the sleeves that hold nothing after those exits. The proceeds are an estimate at the
    completed close. Nothing is submitted.
    """
    if not views:
        raise Unknown('no sleeve is configured')
    reference = next(iter(views.values()))
    if reference.close is None:
        return dict(_flat('no completed daily close is available'), sleeves={}, orders=[], protections=[])
    price = reference.close
    dust = {window: D(owned.get(window, 0)) for window, model in views.items()
            if getattr(model, '_owned_dust', False) and D(owned.get(window, 0)) >= BASE_STEP}
    if any(floor_step(qty, BASE_STEP) * D(snapshot['avg_price']) >= MIN_NOTIONAL for qty in dust.values()):
        raise Unknown('retained close residual is now placeable; owner reconciliation required')
    coins = {window: D(0) if getattr(views[window], '_owned_dust', False)
             else floor_step(D(owned.get(window, 0)), BASE_STEP) for window in views}
    external = D(snapshot['btc']) - sum((D(owned.get(window, 0)) for window in views), D(0))
    if external * price >= MIN_NOTIONAL:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    # An open order blocks a new buy. It does not hide an exit of coins we already record.
    adding_risk = entries_enabled and not snapshot.get('open_orders')
    first = {
        window: _decide(model, snapshot, entries_enabled=False, owned_btc=coins[window], budget=D(0))
        for window, model in views.items()
    }
    exiting = [window for window, item in first.items() if item['action'] == 'exit']
    flat_count = sum(1 for window in views if coins[window] == 0) + len(exiting)
    held_value = sum((coins[window] * price for window in views if window not in exiting), D(0))
    if dust:
        held_value += sum(dust.values(), D(0)) * D(snapshot['avg_price'])
    pool = D(snapshot['usdt_free']) + sum((coins[window] * price for window in exiting), D(0))
    if capital_limit is not None:
        # The limit is the whole exposure, including coins already held.
        pool = min(pool, max(capital_limit - held_value, D(0)))
    budget = D(0) if flat_count == 0 or not adding_risk else pool / flat_count
    decisions = {}
    for window, model in views.items():
        if first[window]['action'] == 'flat' and coins[window] == 0:
            decisions[window] = _decide(
                model, snapshot, entries_enabled=adding_risk, owned_btc=coins[window], budget=budget)
        else:
            decisions[window] = first[window]
    exits = [window for window, item in decisions.items() if item['action'] == 'exit']
    enters = [window for window, item in decisions.items() if item['action'] == 'enter']
    holds = [window for window, item in decisions.items() if item['action'] == 'hold']
    orders = []
    if exits:
        quantity = sum((coins[window] for window in exits), D(0))
        sell = {
            'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET',
            'quantity': _step(quantity, BASE_STEP), 'sleeves': exits,
        }
        if quantity * price >= MIN_NOTIONAL:
            orders.append(sell)
    if enters:
        quote = sum((D(decisions[window]['order']['quoteOrderQty']) for window in enters), D(0))
        buy = {
            'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET',
            'quoteOrderQty': _step(quote, QUOTE_STEP), 'sleeves': enters,
            'note': 'sized on an estimate of free USDT; a real buy waits until the sell has filled',
        }
        orders.append(buy)
    protections = _merge_protections(decisions, views, snapshot, reference)
    parts = []
    for label, group in (('exit', exits), ('enter', enters), ('hold', holds)):
        if group:
            parts.append(f'{label}: sleeves ' + ', '.join(str(window) for window in group))
    if snapshot.get('open_orders'):
        parts.append('open order blocks a new buy')
    if exits and not any(order['side'] == 'SELL' for order in orders):
        parts.append('exit quantity is below the minimum notional')
    action = 'exit' if exits else 'enter' if enters else 'hold' if holds else 'flat'
    out = {
        'action': action,
        'reason': '; '.join(parts) if parts else 'no sleeve holds coins or is armed to enter',
        'order': orders[0] if orders else None,
        'orders': orders,
        'protections': protections,
        'sleeves': {str(window): decisions[window] for window in sorted(views)},
    }
    if exits:
        out['loss_capped'] = False
        out['untradeable'] = not any(order['side'] == 'SELL' for order in orders)
    if consensus:
        consensus_allocation(out, views, snapshot, capital_limit)
    return out


def consensus_allocation(decision, views, snapshot, capital_limit):
    """Increase only a real new BUY when at least two bullish sleeves participate."""
    buys = [o for o in decision['orders'] if o['side'] == 'BUY']
    voters = sum(bool(views[w].bull) and decision['sleeves'][str(w)]['action'] in ('enter', 'hold')
                 for w in views)
    if buys and voters >= 2:
        budget = D(snapshot['usdt_free']) * D('.90')
        if capital_limit is not None:
            budget = min(budget, max(D(0), capital_limit - D(snapshot['btc']) * D(snapshot['avg_price'])))
        budget = floor_step(budget, QUOTE_STEP)
        if budget >= MIN_NOTIONAL:
            buys[0]['quoteOrderQty'] = str(max(D(buys[0]['quoteOrderQty']), budget))
            total = D(buys[0]['quoteOrderQty'])
            group = sorted(buys[0]['sleeves'])
            given = D(0)
            for index, window in enumerate(group):
                share = total - given if index == len(group) - 1 else total / len(group)
                given += share
                sleeve = decision['sleeves'][str(window)]
                sleeve['order'] = dict(sleeve['order'], quoteOrderQty=str(share),
                                       note='Advisory share of the pooled BUY allocation')


def _merge_protections(decisions: dict, views: dict, snapshot: dict, reference: Model) -> list:
    """Same stop price and the same side can be one order. Different prices stay separate."""
    groups: dict[str, list] = {}
    order = []
    for window in sorted(views):
        item = decisions[window]
        if item['action'] not in ('hold', 'enter') or not item.get('protection'):
            continue
        key = item['protection']['stopPrice']
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(window)
    merged = []
    for key in order:
        windows = groups[key]
        sample = dict(decisions[windows[0]]['protection'])
        quantity = sum(
            (D(decisions[window]['protection']['quantity']) for window in windows
             if 'quantity' in decisions[window]['protection']),
            D(0),
        )
        if quantity > 0:
            sample['quantity'] = _step(quantity, BASE_STEP)
            _annotate_venue(sample, views[windows[0]], snapshot)
        sample['sleeves'] = windows
        sample.pop('sleeve', None)
        merged.append(sample)
    return merged


def decision_view(model, position, owners):
    """Adaptive protection on a copy; catch-up and checkpoints keep the static trail."""
    view = copy.copy(model)
    atr = model.atr14
    if atr is None:
        return view
    from .execution import TERMINAL
    view.trail = min(D('.30'), max(D('.10'), 4 * atr / view.close))
    proven = [D(o['order']['stopPrice']) for o in owners.values()
              if position and view.sma_window in o['sleeves'] and o['order']['type'] == 'STOP_LOSS'
              and o.get('native_status') in (TERMINAL - {'REJECTED'}) | {'NEW', 'PARTIALLY_FILLED'}
              and o['signal_ms'] >= int(position['first_ms']) // DAY * DAY - DAY]
    view._stop_floor = max(proven, default=D(0))
    view.protection = 'resting'
    return view


def decision(views, owned, snapshot, *, positions, owners, entries_enabled, capital_limit,
             allocation_scale=D(1)):
    """Canonical ATR book: actual protection, exits first, then bounded new allocation."""
    views = {w: decision_view(v, positions.get(w), owners) for w, v in views.items()}
    out = portfolio(views, owned, snapshot, entries_enabled=entries_enabled, capital_limit=capital_limit)
    forced = False
    for w in views:
        sleeve = out['sleeves'][str(w)]
        stop = sleeve.get('protection')
        if (stop and 'quantity' in stop and D(stop['stopPrice']) >= D(snapshot['avg_price'])
                and sleeve['action'] != 'exit'):
            sleeve.update(action='exit', protection=None, order=None)
            forced = True
    if forced:
        group = [w for w in views if out['sleeves'][str(w)]['action'] == 'exit']
        quantity = sum((floor_step(D(owned[w]), BASE_STEP) for w in group), D(0))
        out['orders'] = [o for o in out['orders'] if o['side'] != 'SELL']
        if quantity * D(snapshot['avg_price']) >= MIN_NOTIONAL:
            out['orders'].append(dict(symbol='BTCUSDT', side='SELL', type='MARKET',
                                      quantity=str(quantity), sleeves=group))
    out['protections'] = _merge_protections(
        {w: out['sleeves'][str(w)] for w in views}, views, snapshot, next(iter(views.values())))
    sells = any(o['side'] == 'SELL' for o in out['orders'])
    free = D(snapshot['usdt_free'])
    cap_remaining = max(D(0), capital_limit - D(snapshot['btc']) * D(snapshot['avg_price'])) if capital_limit is not None else free
    for order in list(out['orders']):
        if order['side'] != 'BUY':
            continue
        quote = floor_step(min(D(order['quoteOrderQty']), free, cap_remaining) * allocation_scale, QUOTE_STEP)
        if sells or quote < MIN_NOTIONAL:
            out['orders'].remove(order)
            for w in order['sleeves']:
                if out['sleeves'][str(w)]['action'] == 'enter':
                    out['sleeves'][str(w)].update(action='flat', order=None, protection=None)
            continue
        order['quoteOrderQty'] = str(quote)
        for w in order['sleeves']:
            sleeve_order = out['sleeves'][str(w)].get('order')
            if sleeve_order and sleeve_order['side'] == 'BUY':
                sleeve_order['quoteOrderQty'] = str(quote / len(order['sleeves']))
        free -= quote
        cap_remaining -= quote
    out['order'] = out['orders'][0] if out['orders'] else None
    return out
