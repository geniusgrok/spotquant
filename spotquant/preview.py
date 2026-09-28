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
            'reason': 'crash-reversal hold; protection remains a stop 28% under the running high',
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
            'reason': 'still above the SMA; protection would remain a stop 28% under the running high',
            'order': None,
            'protection': _protection(model, owned),
        }
    armed = model.enter or model.cap_enter
    if not armed:
        if model.sma is None:
            return _flat('SMA warmup is incomplete')
        if not model.bull:
            return _flat('completed daily close is not above its SMA')
        return _flat('entry is not armed: confirmation, fresh cross, or the crash filter')
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
            'crash reversal cleared the 400-day depth filter'
            if model.cap_enter and not model.enter
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
    if model.repair and model.repair_peak is not None:
        peak = model.repair_peak
    elif model.peak is not None:
        peak = model.peak
    else:
        peak = model.close
    order = {
        'symbol': 'BTCUSDT',
        'side': 'SELL',
        'type': 'STOP_LOSS',
        'stopPrice': _step(model.stop_price(peak), PRICE_STEP),
        'note': 'amended as the daily high ratchets; 28% is wider than trailingDelta',
    }
    if quantity is None:
        order['note'] = 'quantity would be the filled base amount; stop is amended as the high ratchets'
    else:
        order['quantity'] = _step(quantity, BASE_STEP)
    return order


def _step(value: D, step: D) -> str:
    return format(value.quantize(step), 'f')


def _flat(reason: str) -> dict:
    return {'action': 'flat', 'reason': reason, 'order': None, 'protection': None}
