"""Read-only spot decision. No order is built into a network request here."""
from __future__ import annotations

from decimal import Decimal as D

from .model import Model, percent
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
    if snapshot.get('open_orders'):
        raise Unknown('an open order is already on the account; not taking new risk')
    spend = D(snapshot['usdt_free'])
    if capital_limit is not None:
        spend = min(spend, capital_limit)
    return _decide(model, snapshot, entries_enabled=entries_enabled, owned_btc=owned_btc, budget=spend)


def _decide(model: Model, snapshot: dict, *, entries_enabled: bool, owned_btc: D, budget: D) -> dict:
    """One sleeve. ``budget`` is the USDT this sleeve may spend on an entry."""
    price = model.close
    owned = floor_step(owned_btc, BASE_STEP)
    if owned * price >= MIN_NOTIONAL and model.repair:
        return {
            'action': 'hold',
            'reason': (
                f'crash-reversal hold; SMA, blow-off, and the {percent(model.adverse_stop)} close '
                'stay off until the handoff'
            ),
            'order': None,
            'protection': _protection(model, owned, snapshot),
        }
    if owned * price >= MIN_NOTIONAL and model.adverse:
        return {
            'action': 'exit',
            'reason': (
                f'completed daily close is at least {percent(model.adverse_stop)} under the entry fill; '
                f'the sell is the next open, so the loss is not capped at {percent(model.adverse_stop)}'
            ),
            'loss_capped': False,
            'order': {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': _step(owned, BASE_STEP)},
            'protection': None,
        }
    if owned * price >= MIN_NOTIONAL and model.extended:
        return {
            'action': 'exit',
            'reason': f'completed daily close is extended at least {percent(model.extend)} above its SMA',
            'order': {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': _step(owned, BASE_STEP)},
            'protection': None,
        }
    if owned * price >= MIN_NOTIONAL and not model.bull:
        return {
            'action': 'exit',
            'reason': 'completed daily close is not above its SMA',
            'order': {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': _step(owned, BASE_STEP)},
            'protection': None,
        }
    if owned * price >= MIN_NOTIONAL and model.bull:
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
    """Say whether this stop can be rested. Do not add a limit price."""
    down = snapshot.get('ask_multiplier_down')
    up = snapshot.get('ask_multiplier_up')
    average = snapshot.get('avg_price')
    trailing = snapshot.get('trailing_max_bips')
    if down is None or up is None or average is None or trailing is None:
        return
    stop = D(order['stopPrice'])
    limit_ok = D(average) * D(down) <= stop <= D(average) * D(up)
    bips = int((model.trail * D(10000)).to_integral_value())
    trailing_ok = bips <= int(trailing)
    order['limit_placeable'] = limit_ok
    order['trailing_placeable'] = trailing_ok
    order['placeable'] = bool(limit_ok and trailing_ok)
    if order['placeable']:
        return
    extra = []
    if not limit_ok:
        extra.append('a limit at this stop is outside the symbol sell band')
    if not trailing_ok:
        extra.append('trailingDelta cannot express this distance')
    order['note'] = order['note'] + '. ' + '; '.join(extra)
    if 'price' in order:
        del order['price']


def _step(value: D, step: D) -> str:
    return format(value.quantize(step), 'f')


def _flat(reason: str) -> dict:
    return {'action': 'flat', 'reason': reason, 'order': None, 'protection': None}


def portfolio(views: dict, owned: dict, snapshot: dict, *, entries_enabled: bool,
              capital_limit: D | None) -> dict:
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
    coins = {window: floor_step(D(owned.get(window, 0)), BASE_STEP) for window in views}
    external = D(snapshot['btc']) - sum((D(owned.get(window, 0)) for window in views), D(0))
    if external * price >= MIN_NOTIONAL:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    if snapshot.get('open_orders'):
        raise Unknown('an open order is already on the account; not taking new risk')
    first = {
        window: _decide(model, snapshot, entries_enabled=entries_enabled, owned_btc=coins[window],
                        budget=D(0))
        for window, model in views.items()
    }
    exiting = [window for window, item in first.items() if item['action'] == 'exit']
    flat_count = sum(1 for window in views if coins[window] * price < MIN_NOTIONAL) + len(exiting)
    pool = D(snapshot['usdt_free']) + sum((coins[window] * price for window in exiting), D(0))
    if capital_limit is not None:
        pool = min(pool, capital_limit)
    budget = D(0) if flat_count == 0 else pool / flat_count
    decisions = {}
    for window, model in views.items():
        if first[window]['action'] == 'flat' and coins[window] * price < MIN_NOTIONAL:
            decisions[window] = _decide(
                model, snapshot, entries_enabled=entries_enabled, owned_btc=coins[window], budget=budget)
        else:
            decisions[window] = first[window]
    exits = [window for window, item in decisions.items() if item['action'] == 'exit']
    enters = [window for window, item in decisions.items() if item['action'] == 'enter']
    holds = [window for window, item in decisions.items() if item['action'] == 'hold']
    orders = []
    if exits:
        quantity = sum((D(decisions[window]['order']['quantity']) for window in exits), D(0))
        orders.append({
            'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET',
            'quantity': _step(quantity, BASE_STEP), 'sleeves': exits,
        })
    if enters:
        quote = sum((D(decisions[window]['order']['quoteOrderQty']) for window in enters), D(0))
        buy = {
            'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET',
            'quoteOrderQty': _step(quote, QUOTE_STEP), 'sleeves': enters,
        }
        if exits:
            buy['note'] = 'sized on the free USDT plus the estimated proceeds of the exits at this open'
        orders.append(buy)
    protections = []
    for window in sorted(views):
        item = decisions[window]
        if item['action'] in ('hold', 'enter') and item.get('protection'):
            protections.append(dict(item['protection'], sleeve=window))
    parts = []
    for label, group in (('exit', exits), ('enter', enters), ('hold', holds)):
        if group:
            parts.append(f'{label}: sleeves ' + ', '.join(str(window) for window in group))
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
    return out
