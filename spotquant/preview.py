"""Read-only spot decision. No order is built into a network request here."""
from __future__ import annotations

from decimal import Decimal as D

from .model import TRAIL, Model
from .types import Unknown, floor_step

MIN_NOTIONAL = D('5')
QUOTE_STEP = D('0.01')
BASE_STEP = D('0.00001')
MIN_QTY = D('0.00001')
# Binance spot trailingDelta is in bips. 20% == 2000, which is the venue maximum.
TRAILING_DELTA_BIPS = int(TRAIL * 10_000)


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
            'reason': 'still above the SMA; protection would remain a 20% trailingDelta',
            'order': None,
            'protection': _protection(owned),
        }
    if not model.bull:
        return _flat('completed daily close is not above its SMA')
    if model.sma is None:
        return _flat('SMA warmup is incomplete')
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
        'reason': 'a completed close after go-live is above its SMA and the account is flat',
        'order': {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'type': 'MARKET',
            'quoteOrderQty': _step(spend, QUOTE_STEP),
        },
        'protection': {
            'symbol': 'BTCUSDT',
            'side': 'SELL',
            'type': 'STOP_LOSS',
            'trailingDelta': TRAILING_DELTA_BIPS,
            'note': 'quantity would be the filled base amount, which this preview does not assume',
        },
    }


def _protection(quantity: D) -> dict:
    return {
        'symbol': 'BTCUSDT',
        'side': 'SELL',
        'type': 'STOP_LOSS',
        'trailingDelta': TRAILING_DELTA_BIPS,
        'quantity': _step(quantity, BASE_STEP),
    }


def _step(value: D, step: D) -> str:
    return format(value.quantize(step), 'f')


def _flat(reason: str) -> dict:
    return {'action': 'flat', 'reason': reason, 'order': None, 'protection': None}
