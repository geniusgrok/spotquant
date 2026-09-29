"""Record a buy that follows an entry preview. No order is sent.

External BTC, a deposit, or a sell this session did not see stays unknown.
The recorded peak starts at the fill. A daily high is included only when that
bar opens after the fill, or when the fill is within the first minute of the
bar. A wick from before the fill is not the stop.
"""
from __future__ import annotations

from decimal import Decimal as D

from .model import DAY, ORIGIN, Model
from .preview import BASE_STEP, MIN_NOTIONAL
from .types import Blocked, Unknown, number

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


def matched_buy(trades, balance: D, since_ms: int, mark: D):
    """The buy that explains ``balance``, or None when the account is flat.

    A material balance that is not the buys after the latest sell is unknown.
    """
    window = [trade for trade in trades if trade['time'] >= since_ms]
    last_sell = None
    for index, trade in enumerate(window):
        if not trade['buyer']:
            last_sell = index
    if last_sell is not None:
        window = window[last_sell + 1:]
    material = balance * mark >= MIN_NOTIONAL
    if not window:
        if material:
            raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
        return None
    if any(not trade['buyer'] for trade in window):
        raise Unknown('a sell is mixed into the followed buy; refusing new risk')
    gross = sum((trade['qty'] for trade in window), D(0))
    quote = sum((trade['quote'] for trade in window), D(0))
    net = sum((_base_delta(trade) for trade in window), D(0))
    if gross <= 0 or quote <= 0 or net <= 0:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    if abs(balance - net) > BASE_STEP:
        raise Unknown('BTC balance does not match the followed buy; refusing new risk')
    return {
        'qty': balance,
        'entry_fill': quote / gross,
        'first_ms': min(trade['time'] for trade in window),
    }


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


def replay(bars, *, entry_fill: D, first_ms: int, repair: bool) -> dict:
    """Replay completed bars so a fill adopted later still has the right flags."""
    model = Model()
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
