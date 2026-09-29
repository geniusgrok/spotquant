"""Read-only spot decision. No order is built into a network request here."""
from __future__ import annotations

from decimal import Decimal as D

from .model import Model
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
    owned = floor_step(owned_btc, BASE_STEP)
    if owned * price >= MIN_NOTIONAL and model.repair:
        return {
            'action': 'hold',
            'reason': 'crash-reversal hold; SMA, blow-off, and the 4% close stay off until the handoff',
            'order': None,
            'protection': _protection(model, owned, snapshot),
        }
    if owned * price >= MIN_NOTIONAL and model.adverse:
        return {
            'action': 'exit',
            'reason': (
                'completed daily close is at least 4% under the entry fill; '
                'the sell is the next open, so the loss is not capped at 4%'
            ),
            'loss_capped': False,
            'order': {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': _step(owned, BASE_STEP)},
            'protection': None,
        }
    if owned * price >= MIN_NOTIONAL and model.extended:
        return {
            'action': 'exit',
            'reason': 'completed daily close is extended at least 60% above its SMA',
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
            reason = 'still above the SMA; protection is a stop 28% under the high since the fill'
        else:
            reason = (
                'still above the SMA; no fill is recorded, so the stop stays 28% under the completed close'
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
    spend = D(snapshot['usdt_free'])
    if capital_limit is not None:
        spend = min(spend, capital_limit)
    spend = floor_step(spend, QUOTE_STEP)
    if spend < MIN_NOTIONAL:
        return _flat('free USDT is below the 5 USDT minimum notional')
    return {
        'action': 'enter',
        'reason': (
            'crash reversal: 8% down, then 6% up, still at least half under the 400-day high'
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
    if quantity is None:
        peak = model.close
        if model.cap_enter:
            note = (
                'quantity would be the filled base amount; crash-reversal stop starts 28% under '
                'the completed close and is amended to the fill'
            )
        else:
            note = (
                'quantity would be the filled base amount; stop starts 28% under the completed '
                'close and is amended to the fill'
            )
    elif model.repair and model.repair_peak is not None:
        peak = model.repair_peak
        note = 'amended STOP_LOSS; 28% under the high since the repair fill'
    elif model.position_peak is not None:
        peak = model.position_peak
        note = 'amended STOP_LOSS; 28% under the high since the fill'
    else:
        peak = model.close
        note = 'amended STOP_LOSS; 28% under the completed close until a fill is recorded'
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
