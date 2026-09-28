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
            'protection': _protection(model, owned),
        }
    if owned * price >= MIN_NOTIONAL and model.adverse:
        return {
            'action': 'exit',
            'reason': 'completed daily close is at least 4% under the entry fill',
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
        return {
            'action': 'hold',
            'reason': 'still above the SMA; protection is a stop 28% under the bullish-streak high',
            'order': None,
            'protection': _protection(model, owned),
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
        'protection': _protection(model, None),
    }


def _protection(model: Model, quantity: D | None) -> dict:
    # No fill exists yet. A bullish-streak high from before the buy can already
    # sit through the close, and a crash reversal is a repair hold on the meter
    # even when the ordinary entry is also armed. Anchor the preview stop on
    # the completed close; the meter replaces that anchor with the fill open.
    if quantity is None:
        peak = model.close
        if model.cap_enter:
            note = (
                'quantity would be the filled base amount; crash-reversal stop starts 28% under '
                'the completed close and is amended to the fill open'
            )
        else:
            note = (
                'quantity would be the filled base amount; stop starts 28% under the completed '
                'close and is amended to the fill open'
            )
    elif model.repair and model.repair_peak is not None:
        peak = model.repair_peak
        note = 'amended STOP_LOSS; 28% under the repair high during a crash reversal'
    elif model.peak is not None:
        peak = model.peak
        note = 'amended STOP_LOSS; 28% under the bullish-streak high'
    else:
        peak = model.close
        note = 'amended STOP_LOSS; 28% under the completed close'
    order = {
        'symbol': 'BTCUSDT',
        'side': 'SELL',
        'type': 'STOP_LOSS',
        'stopPrice': _step(model.stop_price(peak), PRICE_STEP),
        'note': note,
    }
    if quantity is not None:
        order['quantity'] = _step(quantity, BASE_STEP)
    return order


def _step(value: D, step: D) -> str:
    return format(value.quantize(step), 'f')


def _flat(reason: str) -> dict:
    return {'action': 'flat', 'reason': reason, 'order': None, 'protection': None}
