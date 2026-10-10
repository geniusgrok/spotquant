"""Owner-run Binance Spot Demo checks for the live order path.

Scenarios call ``Binance.submit``, ``cancel``, and ``query`` and the durable
``Lifecycle`` identity rules. SMA proximity and the 4% adverse exit use forced
prices and are marked ``simulated_trigger``. HTTP 429 is injected. Nothing here
changes strategy rules or sets ``native_execution_verified``.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from decimal import Decimal as D
from pathlib import Path

from .binance import allow_demo_bnb_discount
from .demo_guard import DEMO_ORIGIN, assert_demo_config, install_demo_guard, refuse_non_demo_url
from .execution import Lifecycle
from .model import Model
from .preview import BASE_STEP, PRICE_STEP, QUOTE_STEP, _position_decision, _qty_ok, _step
from .state import State, client_id
from .types import Blocked, NotSent, Unknown, floor_step, number, serial

ORDER_SCENARIOS = (
    'market-buy', 'stop-place', 'stop-replace', 'adverse-exit', 'sma-exit', 'graceful-stop',
)
FAULT_SCENARIOS = ('clock-skew', 'rate-limit', 'network-loss', 'kill-restart')
PROBE_QUOTE = D('15')
ROOT = Path(__file__).resolve().parents[1]
LIMITATIONS = (
    'Demo 撮合与深度是模拟的，滑点未被验证。',
    '场景通过也不会把 native_execution_verified 设为真，也不能当作主网证据。',
    '均线靠近卖出和 4% 不利收盘退出使用构造价格，记录里带 simulated_trigger。',
    'HTTP 429 由可替换传输注入，不是向 Demo 灌请求造出来的。',
)
CHECKPOINT_KEYS = ('models', 'rule', 'execution_anchor', 'positions', 'follows', 'entries_after')
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH'}


def select_scenarios(names, faults) -> tuple:
    if names is None:
        return ORDER_SCENARIOS + (FAULT_SCENARIOS if faults else ())
    if not names or any(type(name) is not str or not name for name in names) or len(names) != len(set(names)):
        raise Blocked('unknown verification scenario')
    allowed = set(ORDER_SCENARIOS + FAULT_SCENARIOS)
    if any(name not in allowed for name in names):
        raise Blocked('unknown verification scenario')
    if any(name in FAULT_SCENARIOS for name in names) and not faults:
        raise Blocked('fault scenarios only run with --faults')
    return tuple(names)


def _nonce_ok(nonce) -> bool:
    return isinstance(nonce, str) and len(nonce) == 32 and all(c in '0123456789abcdef' for c in nonce)


def verification_identity(scope: str, token: int, operation: str, nonce: str) -> str:
    """Stable for one state directory and sequence, and different for every new directory.

    ``nonce`` is stored in that directory. A fresh directory therefore cannot
    repeat a client id that an earlier directory already sent.
    """
    if (type(token) is not int or token <= 0 or not _nonce_ok(nonce)
            or not operation or any(item in operation for item in ('/', ' '))):
        raise Blocked('verification identity is not stable')
    return client_id(scope, token, f'verify-{nonce}-{operation}')


def _verification_nonce(state) -> str:
    saved = state.get('verification_nonce')
    if saved is None:
        nonce = secrets.token_hex(16)
        state.set('verification_nonce', nonce)
        return nonce
    if _nonce_ok(saved):
        return saved
    raise Blocked('verification identity is not stable')


def protection_peak(fill_price, quote, *, quote_after_fill: bool):
    """Peak for the first stop: the verified fill, then a later session quote."""
    peak = number(fill_price, 'fill', positive=True)
    if quote_after_fill:
        peak = max(peak, number(quote, 'quote', positive=True))
    return peak


def protection_price(fill_price, quote, *, quote_after_fill: bool):
    """28% under the verified peak, floored to the BTCUSDT tick."""
    peak = protection_peak(fill_price, quote, quote_after_fill=quote_after_fill)
    return floor_step(Model(40).stop_price(peak), PRICE_STEP)


def simulated_exit_decision(kind: str, quantity, snapshot: dict):
    """Force one real exit rule with synthetic prices. The sell is separate."""
    if kind not in ('sma', 'adverse'):
        raise Blocked('unknown forced exit')
    model = Model(40)
    model.shadow_in = True
    model.bull = True
    model.extended = False
    model.repair = False
    model.need_reset = False
    entry = D('100')
    model.entry = entry
    model.position_peak = entry
    model.sma = entry
    view = dict(snapshot)
    view.setdefault('avg_price', snapshot.get('last_price') or entry)
    if kind == 'adverse':
        model.close = entry * (D(1) - model.adverse_stop)
        model.adverse = model.close <= entry * (D(1) - model.adverse_stop)
        if model.adverse is not True:
            raise Blocked('adverse formula did not arm')
        view['last_price'] = entry
    else:
        model.close = entry
        model.adverse = False
        view['last_price'] = model.sma * (D(1) + model.touch)
    decision = _position_decision(model, view, number(quantity, 'quantity', positive=True))
    evidence = {
        'kind': kind,
        'simulated': True,
        'entry': format(entry, 'f'),
        'close': format(model.close, 'f'),
        'sma': format(model.sma, 'f'),
        'last_price': format(D(view['last_price']), 'f'),
        'adverse': model.adverse is True,
        'touch': format(model.touch, 'f'),
        'adverse_stop': format(model.adverse_stop, 'f'),
    }
    return decision, evidence


def probe_quote(snapshot: dict, capital) -> str:
    minimum = D(snapshot['min_notional'])
    quote = floor_step(max(PROBE_QUOTE, minimum), QUOTE_STEP)
    exposure = D(snapshot['btc']) * D(snapshot['last_price'])
    if quote > capital or exposure + quote > capital:
        raise Blocked('verification order would exceed the 100 USDT capital ceiling')
    return _step(quote, QUOTE_STEP)


def _order_payload(order: dict, signal_ms: int) -> dict:
    weight = '1' if order.get('side') == 'BUY' else order['quantity']
    return {
        'order': {key: order[key] for key in ('symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice')
                  if key in order},
        'sleeves': [40],
        'weights': {'40': weight},
        'signal_ms': signal_ms,
        'repair': {'40': False},
        'rearm': {'40': False},
        'verification': True,
    }


def _intent(lifecycle, identity):
    return next((row for row in lifecycle.rows() if row[0] == identity), None)


def _intent_ids(lifecycle) -> tuple:
    return tuple(row[0] for row in lifecycle.rows())


def _reject_unconfirmed(lifecycle) -> None:
    for identity, _, status, _ in lifecycle.rows():
        if status in ('unknown', 'canceling', 'partial'):
            raise Blocked('an unconfirmed verification order is still open; reconcile it before another order '
                          + identity)


def _recover_unknown(lifecycle, identity):
    """Read the saved identity back. Do not submit it again."""
    try:
        lifecycle.recover()
    except (Unknown, Blocked):
        pass
    row = _intent(lifecycle, identity)
    if row is not None and row[3].get('orderId') and (
            row[2] in ('resting', 'settled') or row[3].get('status') in TERMINAL | {'NEW', 'PARTIALLY_FILLED'}):
        return row
    raise Unknown('order outcome is unknown; the same identity was not resent')


def dispatch_order(lifecycle, identity, order, *, signal_ms):
    """Persist, submit once, then read back. ``Unknown`` is queried, never resent."""
    if not getattr(lifecycle.venue, '_demo_guard', False):
        raise Blocked(f'demo verification only sends to {DEMO_ORIGIN}')
    payload = _order_payload(order, signal_ms)
    current = _intent(lifecycle, identity)
    if current is not None and current[2] in ('unknown', 'resting', 'canceling', 'partial'):
        try:
            lifecycle.recover()
        except (Unknown, Blocked):
            pass
        recovered = _intent(lifecycle, identity)
        if recovered[2] in ('unknown', 'canceling', 'partial') and not recovered[3].get('orderId'):
            raise Unknown('order outcome is unknown; the same identity was not resent')
        return recovered
    if current is not None and current[2] == 'settled':
        return current
    if current is not None and current[2] == 'rejected':
        raise Blocked('durable verification order was rejected; it was not resent')
    if current is None:
        lifecycle.save(identity, payload, 'prepared', {})
    elif current[2] == 'prepared':
        payload = current[1]
    else:
        raise Blocked('durable verification order is in an unexpected state')
    lifecycle.save(identity, payload, 'unknown', {})
    try:
        accepted = lifecycle.venue.submit(identity, payload['order'])
    except NotSent:
        lifecycle.save(identity, payload, 'prepared', {'not_sent': True})
        raise
    except Blocked:
        lifecycle.save(identity, payload, 'rejected', {})
        raise
    except Unknown:
        return _recover_unknown(lifecycle, identity)
    if (not isinstance(accepted, dict) or type(accepted.get('orderId')) is not int
            or accepted['orderId'] <= 0 or accepted.get('clientOrderId') != identity
            or accepted.get('symbol') != 'BTCUSDT'):
        if isinstance(accepted, dict):
            lifecycle.save(identity, payload, 'unknown', accepted)
        return _recover_unknown(lifecycle, identity)
    lifecycle.save(identity, payload, 'unknown', accepted)
    try:
        lifecycle.recover()
    except (Unknown, Blocked):
        return _recover_unknown(lifecycle, identity)
    return _intent(lifecycle, identity)


def confirm_order(lifecycle, identity, *, sleep, attempts=6):
    row = _intent(lifecycle, identity)
    for attempt in range(attempts):
        status = row[3].get('status')
        kind = row[1]['order'].get('type')
        if status in TERMINAL or (status == 'NEW' and kind == 'STOP_LOSS'):
            return row
        if attempt + 1 == attempts:
            break
        sleep(0.5)
        try:
            lifecycle.recover()
        except (Unknown, Blocked):
            pass
        row = _intent(lifecycle, identity)
    return row


def remember_fills(state, venue, order_id, since_ms) -> dict:
    trades = [row for row in state.trades(venue, since_ms) if row['order_id'] == order_id]
    if not trades:
        raise Unknown('filled order has no account trade')
    gross = sum((row['qty'] for row in trades), D(0))
    quote = sum((row['quote'] for row in trades), D(0))
    if gross <= 0 or quote <= 0:
        raise Unknown('filled order has no account trade')
    assets = []
    bnb_commission = False
    for row in trades:
        asset = row.get('commission_asset')
        if asset and asset not in assets:
            assets.append(asset)
        if asset == 'BNB':
            bnb_commission = True
    return {
        'price': quote / gross,
        'qty': gross,
        'time_ms': min(row['time'] for row in trades),
        'ids': [row['id'] for row in trades],
        'commission_assets': assets,
        'bnb_commission': bnb_commission,
    }


def _since(result, venue) -> int:
    stamp = result.get('time') if type(result.get('time')) is int else result.get('transactTime')
    if type(stamp) is not int:
        stamp = venue._timestamp()
    return max(0, stamp - 60_000)


def _holds(snapshot) -> bool:
    return D(snapshot['btc']) * D(snapshot['last_price']) >= D(snapshot['min_notional'])


def _checkpoint(state) -> bool:
    return any(state.get(key) is not None for key in CHECKPOINT_KEYS)


def _next_token(state) -> int:
    current = state.get('verification_seq')
    token = current + 1 if type(current) is int and current >= 0 else 1
    state.set('verification_seq', token)
    return token


class LostResponse:
    """Send the POST, then drop the response so the caller must query."""

    def __init__(self, opener):
        self.opener = opener
        self.post_count = 0
        self.lost = 0

    def __call__(self, method, url, headers):
        refuse_non_demo_url(url)
        if method != 'POST':
            return self.opener(method, url, headers)
        self.post_count += 1
        result = self.opener(method, url, headers)
        if self.lost == 0:
            self.lost += 1
            raise Unknown('response lost after the request was dispatched')
        return result


def _as_triple(opened):
    if not isinstance(opened, tuple) or len(opened) not in (2, 3):
        raise Unknown('Binance response is incomplete')
    body = opened[1]
    if isinstance(body, str):
        body = body.encode()
    headers = opened[2] if len(opened) == 3 else {}
    return opened[0], body, dict(headers)


def _body_code(body):
    try:
        payload = json.loads(body.decode() if isinstance(body, (bytes, bytearray)) else body)
    except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
        return None
    if isinstance(payload, dict) and type(payload.get('code')) is int:
        return payload['code']
    return None


def _cancel_resting(lifecycle) -> list:
    identities = [row[0] for row in lifecycle.rows()
                  if row[2] == 'resting' and row[1]['order'].get('type') == 'STOP_LOSS']
    for identity in identities:
        lifecycle.cancel(identity)
    return identities


def _record_buy(ctx, operation):
    venue, config = ctx['venue'], ctx['config']
    snapshot = venue.snapshot(config.account_uid)
    if _holds(snapshot):
        return {'status': 'fail', 'reason': 'account already holds BTC; verification will not adopt it',
                'resent': False, 'simulated_trigger': False}
    _reject_unconfirmed(ctx['lifecycle'])
    quote = probe_quote(snapshot, config.capital_limit)
    order = {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': quote}
    identity = verification_identity(config.scope, ctx['token'], operation, ctx['nonce'])
    row = dispatch_order(ctx['lifecycle'], identity, order, signal_ms=ctx['token'])
    row = confirm_order(ctx['lifecycle'], identity, sleep=ctx['sleep'])
    result = row[3]
    if result.get('status') != 'FILLED' or not result.get('orderId'):
        return {'status': 'fail', 'reason': 'market buy was not filled', 'client_id': identity,
                'order_status': result.get('status'), 'resent': False, 'simulated_trigger': False}
    fill = remember_fills(ctx['state'], venue, result['orderId'], _since(result, venue))
    if result.get('cummulativeQuoteQty') not in (None, '') and result.get('executedQty') not in (None, ''):
        native = number(result['cummulativeQuoteQty']) / number(result['executedQty'], positive=True)
        if abs(native - fill['price']) > D('0.00000001'):
            raise Unknown('trade average differs from the native cumulative price')
    ctx['entry'] = {
        'price': format(fill['price'], 'f'),
        'time_ms': fill['time_ms'],
        'order_id': result['orderId'],
        'client_id': identity,
        'quote': quote,
    }
    charged_bnb = fill.get('bnb_commission') is True
    return {
        'status': 'fail' if charged_bnb else 'pass',
        'reason': 'buy fill commission asset is BNB' if charged_bnb else None,
        'client_id': identity,
        'order_id': result['orderId'],
        'fill_price': ctx['entry']['price'],
        'quote_order_qty': quote,
        'trade_ids': fill['ids'],
        'commission_assets': fill.get('commission_assets') or [],
        'bnb_commission': charged_bnb,
        'demo_bnb_discount_allowance': getattr(venue, 'demo_bnb_discount_allowance', False) is True,
        'resent': False,
        'simulated_trigger': False,
    }


def scenario_market_buy(ctx):
    return _record_buy(ctx, 'market-buy')


def _load_entry(ctx):
    if ctx.get('entry'):
        return ctx['entry']
    filled = [row for row in ctx['lifecycle'].rows()
              if row[1]['order'].get('side') == 'BUY' and row[3].get('status') == 'FILLED']
    if not filled:
        return None
    result = max(filled, key=lambda row: row[3].get('time') or row[3].get('transactTime') or 0)[3]
    if result.get('cummulativeQuoteQty') in (None, '') or result.get('executedQty') in (None, ''):
        return None
    price = number(result['cummulativeQuoteQty']) / number(result['executedQty'], positive=True)
    stamp = result.get('time') if type(result.get('time')) is int else result.get('transactTime')
    return {
        'price': format(price, 'f'),
        'time_ms': stamp if type(stamp) is int else 0,
        'order_id': result.get('orderId'),
        'client_id': result.get('clientOrderId'),
    }


def _place_stop(ctx, operation):
    venue, config = ctx['venue'], ctx['config']
    entry = _load_entry(ctx)
    if entry is None:
        return {'status': 'skipped', 'reason': 'no verified buy fill is available for a stop',
                'simulated_trigger': False}
    snapshot = venue.snapshot(config.account_uid)
    qty = floor_step(D(snapshot['btc']), BASE_STEP)
    if qty <= 0 or not _qty_ok(qty, snapshot):
        return {'status': 'fail', 'reason': 'BTC balance cannot be protected with a native stop',
                'simulated_trigger': False}
    if qty * D(snapshot['avg_price']) < D(snapshot['min_notional']):
        return {'status': 'fail', 'reason': 'position is below the venue minimum notional',
                'simulated_trigger': False}
    observed = snapshot.get('quote_observed_ms')
    quote_after = type(observed) is int and type(entry.get('time_ms')) is int and observed >= entry['time_ms']
    stop = protection_price(entry['price'], snapshot['last_price'], quote_after_fill=quote_after)
    if stop >= D(snapshot['last_price']):
        return {'status': 'fail', 'reason': 'stop is already crossed at the latest price',
                'stop_price': format(stop, 'f'), 'simulated_trigger': False}
    _reject_unconfirmed(ctx['lifecycle'])
    order = {
        'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
        'quantity': _step(qty, BASE_STEP), 'stopPrice': _step(stop, PRICE_STEP),
    }
    identity = verification_identity(config.scope, ctx['token'], operation, ctx['nonce'])
    row = dispatch_order(ctx['lifecycle'], identity, order, signal_ms=ctx['token'])
    row = confirm_order(ctx['lifecycle'], identity, sleep=ctx['sleep'])
    actual = row[3].get('stopPrice')
    matched = row[3].get('status') == 'NEW' and actual is not None and number(actual) == number(order['stopPrice'])
    return {
        'status': 'pass' if matched else 'fail',
        'reason': None if matched else 'native stop price or status differs from the 28% trail',
        'client_id': identity,
        'order_id': row[3].get('orderId'),
        'stop_price': order['stopPrice'],
        'exchange_stop_price': None if actual is None else str(actual),
        'fill_price': entry['price'],
        'peak_price': format(protection_peak(entry['price'], snapshot['last_price'],
                                             quote_after_fill=quote_after), 'f'),
        'trail': '0.28',
        'resent': False,
        'simulated_trigger': False,
    }


def scenario_stop_place(ctx):
    return _place_stop(ctx, 'stop-place')


def scenario_stop_replace(ctx):
    lifecycle = ctx['lifecycle']
    resting = [row for row in lifecycle.rows()
               if row[2] == 'resting' and row[1]['order'].get('type') == 'STOP_LOSS']
    if len(resting) != 1:
        return {'status': 'skipped', 'reason': 'stop replacement needs exactly one resting verification stop',
                'simulated_trigger': False}
    old_id, old_payload, _, _ = resting[0]
    entry = _load_entry(ctx)
    if entry is None:
        return {'status': 'skipped', 'reason': 'no verified buy fill is available for a stop',
                'simulated_trigger': False}
    snapshot = ctx['venue'].snapshot(ctx['config'].account_uid)
    observed = snapshot.get('quote_observed_ms')
    quote_after = type(observed) is int and type(entry.get('time_ms')) is int and observed >= entry['time_ms']
    stop = protection_price(entry['price'], snapshot['last_price'], quote_after_fill=quote_after)
    new_price = _step(stop, PRICE_STEP)
    forced = number(new_price) == number(old_payload['order']['stopPrice'])
    clock = ctx['monotonic']
    clock()
    lifecycle.cancel(old_id)
    cancel_done = clock()
    snapshot = ctx['venue'].snapshot(ctx['config'].account_uid)
    qty = floor_step(D(snapshot['btc_free']), BASE_STEP)
    if qty <= 0 or not _qty_ok(qty, snapshot):
        return {'status': 'fail', 'reason': 'cancel left no free BTC for the replacement stop',
                'simulated_trigger': forced, 'forced_rereplace': forced}
    order = {
        'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
        'quantity': _step(qty, BASE_STEP), 'stopPrice': new_price,
    }
    identity = verification_identity(ctx['config'].scope, ctx['token'], 'stop-replace', ctx['nonce'])
    row = dispatch_order(lifecycle, identity, order, signal_ms=ctx['token'])
    row = confirm_order(lifecycle, identity, sleep=ctx['sleep'])
    place_done = clock()
    local_ms = int((place_done - cancel_done) * 1000)
    canceled = _intent(lifecycle, old_id)
    new_time = row[3].get('time') if type(row[3].get('time')) is int else row[3].get('transactTime')
    old_update = canceled[3].get('updateTime')
    exchange_ms = new_time - old_update if type(new_time) is int and type(old_update) is int else None
    resting_now = [item for item in lifecycle.rows()
                   if item[2] == 'resting' and item[1]['order'].get('type') == 'STOP_LOSS']
    price_ok = row[3].get('status') == 'NEW' and number(row[3].get('stopPrice')) == number(new_price)
    single = len(resting_now) == 1 and resting_now[0][0] == identity
    canceled_ok = canceled[2] == 'settled' and canceled[3].get('status') == 'CANCELED'
    measured = type(exchange_ms) is int and exchange_ms >= 0 and local_ms >= 0
    passed = price_ok and single and canceled_ok and measured
    return {
        'status': 'pass' if passed else 'fail',
        'reason': None if passed else 'stop replacement did not confirm a single stop and a measured gap',
        'client_id': identity,
        'order_id': row[3].get('orderId'),
        'canceled_client_id': old_id,
        'stop_price': new_price,
        'previous_stop_price': old_payload['order'].get('stopPrice'),
        'forced_rereplace': forced,
        'simulated_trigger': forced,
        'unprotected_window_ms': local_ms,
        'exchange_unprotected_window_ms': exchange_ms,
        'resent': False,
        'note': ('会话报价没有抬高保护价，本次为测量撤单到重挂的空窗而按同一正确价格重挂。'
                 if forced else '保护价按成交后的验证高点上移后撤旧挂新。'),
    }


def _sell_free(ctx, operation):
    _cancel_resting(ctx['lifecycle'])
    snapshot = ctx['venue'].snapshot(ctx['config'].account_uid)
    free = floor_step(D(snapshot['btc_free']), BASE_STEP)
    if free <= 0 or free * D(snapshot['avg_price']) < D(snapshot['min_notional']) or not _qty_ok(free, snapshot):
        return {'status': 'fail', 'reason': 'no free BTC to sell', 'resent': False}
    order = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': _step(free, BASE_STEP)}
    identity = verification_identity(ctx['config'].scope, ctx['token'], operation, ctx['nonce'])
    row = dispatch_order(ctx['lifecycle'], identity, order, signal_ms=ctx['token'])
    row = confirm_order(ctx['lifecycle'], identity, sleep=ctx['sleep'])
    if row[3].get('status') != 'FILLED':
        return {'status': 'fail', 'reason': 'sell was not filled', 'client_id': identity,
                'order_status': row[3].get('status'), 'resent': False}
    fill = remember_fills(ctx['state'], ctx['venue'], row[3]['orderId'], _since(row[3], ctx['venue']))
    ctx['entry'] = None
    return {'status': 'pass', 'client_id': identity, 'order_id': row[3].get('orderId'),
            'trade_ids': fill['ids'], 'resent': False}


def _scenario_exit(ctx, kind):
    snapshot = ctx['venue'].snapshot(ctx['config'].account_uid)
    qty = floor_step(D(snapshot['btc']), BASE_STEP)
    bought = None
    stopped = None
    if qty <= 0 or qty * D(snapshot['avg_price']) < D(snapshot['min_notional']):
        bought = _record_buy(ctx, f'market-buy-before-{kind}')
        if bought.get('status') != 'pass':
            return {'status': 'skipped', 'reason': 'no position for the exit', 'buy': bought,
                    'simulated_trigger': True, 'trigger': kind}
        stopped = _place_stop(ctx, f'stop-before-{kind}')
        snapshot = ctx['venue'].snapshot(ctx['config'].account_uid)
        qty = floor_step(D(snapshot['btc']), BASE_STEP)
    decision, evidence = simulated_exit_decision(kind, qty, snapshot)
    if decision.get('action') != 'exit' or not decision.get('order'):
        return {'status': 'fail', 'reason': 'forced prices did not produce a tradable exit',
                'simulated_trigger': True, 'trigger': kind, 'synthetic_inputs': evidence}
    _reject_unconfirmed(ctx['lifecycle'])
    sale = _sell_free(ctx, f'{kind}-exit')
    stop_failed = stopped is not None and stopped.get('status') != 'pass'
    passed = sale.get('status') == 'pass' and not stop_failed
    record = {
        'status': 'pass' if passed else 'fail',
        'reason': None if passed else (sale.get('reason') or 'exit filled but the stop before it did not confirm'),
        'simulated_trigger': True,
        'trigger': kind,
        'decision_reason': decision.get('reason'),
        'synthetic_inputs': evidence,
        'client_id': sale.get('client_id'),
        'order_id': sale.get('order_id'),
        'order_status': 'FILLED' if sale.get('status') == 'pass' else sale.get('order_status'),
        'resent': False,
        'note': '触发价格是验证程序构造的，不是当时行情。卖单使用真实下单与回读。',
    }
    if bought:
        record['probe_buy'] = {'client_id': bought.get('client_id'), 'order_id': bought.get('order_id')}
    if stopped:
        record['stop_before_exit'] = {'status': stopped.get('status'), 'client_id': stopped.get('client_id'),
                                      'stop_price': stopped.get('stop_price')}
    return record


def scenario_adverse_exit(ctx):
    return _scenario_exit(ctx, 'adverse')


def scenario_sma_exit(ctx):
    return _scenario_exit(ctx, 'sma')


def demo_check_argv(config_path, uid) -> list:
    return [sys.executable, '-m', 'spotquant', 'demo-check', '--config', str(config_path),
            '--execute', '--authorize-uid', str(uid)]


def session_cycle_ready(report) -> bool:
    """True after demo-check has committed one observation, not a checkpoint refusal."""
    if not isinstance(report, dict):
        return False
    if 'missing model checkpoint' in str(report.get('reason', '')):
        return False
    return (report.get('cycles', 0) >= 1 and report.get('observation_current') is True
            and isinstance(report.get('model_bull'), dict))


def write_graceful_config(config, config_path):
    """Point the child at an empty directory so verification intents cannot block it.

    A session with orders and no model checkpoint refuses every cycle. The child
    therefore uses ``<state_dir>/graceful-session`` and a longer session so one
    daily catch-up can finish before the parent sends SIGINT.
    """
    source = json.loads(Path(config_path).read_text(encoding='utf-8'))
    if not isinstance(source, dict):
        raise Blocked('graceful stop configuration cannot be read')
    parent = Path(config.state_dir).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    child_state = parent / 'graceful-session'
    if child_state.resolve() == parent:
        raise Blocked('graceful stop needs its own state directory')
    source['state_dir'] = str(child_state)
    source['environment'] = 'demo'
    source['session_seconds'] = max(int(source.get('session_seconds') or config.session_seconds), 900)
    poll = int(source.get('poll_seconds') or config.poll_seconds)
    if poll > source['session_seconds']:
        source['poll_seconds'] = min(poll, source['session_seconds'])
    path = parent / 'graceful-session.json'
    path.write_text(json.dumps(source, indent=2) + '\n', encoding='utf-8')
    return path, child_state


def assess_graceful(judged, report, *, real_cycle: bool) -> dict:
    out = dict(judged)
    text = json.dumps(report, ensure_ascii=False) if isinstance(report, dict) else ''
    if 'missing model checkpoint' in text:
        out['status'] = 'fail'
        out['reason'] = 'missing model checkpoint for durable state'
        out['real_cycle'] = False
        return out
    out['real_cycle'] = bool(real_cycle)
    if out.get('status') == 'pass' and not real_cycle:
        out['status'] = 'fail'
        out['reason'] = 'graceful stop did not finish a real session cycle before SIGINT'
    return out


def judge_graceful(report, *, report_updated: bool) -> dict:
    if not report_updated or not isinstance(report, dict):
        return {'status': 'fail', 'reason': 'session report was not updated after SIGINT',
                'simulated_trigger': False}
    reason = report.get('stop_reason')
    closeout = report.get('closeout_attempted') is True
    if reason in ('interrupted', 'requested') and closeout:
        return {'status': 'pass', 'stop_reason': reason, 'closeout_attempted': True,
                'manual_takeover': report.get('manual_takeover'), 'simulated_trigger': False}
    return {'status': 'fail', 'stop_reason': reason, 'closeout_attempted': bool(report.get('closeout_attempted')),
            'reason': 'SIGINT did not finish as a protective closeout', 'simulated_trigger': False}


def interrupt_child(argv, *, popen=subprocess.Popen, sleep=time.sleep, interrupt_after=2.0, timeout=240,
                    until=None):
    proc = popen(argv, start_new_session=True, cwd=str(ROOT))
    started = time.time()
    deadline = started + timeout
    ready = False
    while proc.poll() is None and time.time() < deadline:
        if until is not None:
            if until():
                ready = True
                break
        elif time.time() - started >= interrupt_after:
            break
        sleep(0.05 if until is None else 0.25)
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        code = proc.wait(timeout=10)
        return {'returncode': code, 'timed_out': True, 'real_cycle': ready}
    return {'returncode': code, 'timed_out': False, 'real_cycle': ready}


def scenario_graceful_stop(ctx):
    path = ctx.get('config_path')
    if not path:
        return {'status': 'fail', 'reason': 'graceful stop needs the configuration path',
                'simulated_trigger': False, 'real_cycle': False}
    child_config, child_state = write_graceful_config(ctx['config'], path)
    latest = child_state / 'latest.json'
    before = latest.read_text(encoding='utf-8') if latest.exists() else ''

    def until():
        try:
            text = latest.read_text(encoding='utf-8')
        except OSError:
            return False
        try:
            return session_cycle_ready(json.loads(text))
        except json.JSONDecodeError:
            return False

    outcome = interrupt_child(
        demo_check_argv(child_config, ctx['config'].account_uid), popen=ctx['popen'], sleep=ctx['sleep'],
        interrupt_after=ctx['interrupt_after'], timeout=ctx['graceful_timeout'], until=until)
    after = latest.read_text(encoding='utf-8') if latest.exists() else ''
    updated = bool(after) and after != before
    try:
        report = json.loads(after) if updated else {}
    except json.JSONDecodeError:
        report = {}
        updated = False
    judged = assess_graceful(judge_graceful(report, report_updated=updated), report,
                             real_cycle=outcome.get('real_cycle') is True)
    judged['returncode'] = outcome['returncode']
    judged['timed_out'] = outcome['timed_out']
    judged['child_state_dir'] = str(child_state)
    if outcome['timed_out'] and judged.get('reason') != 'missing model checkpoint for durable state':
        judged['status'] = 'fail'
        judged['reason'] = ('graceful stop did not finish a real session cycle before the timeout'
                            if not outcome.get('real_cycle') else
                            'graceful stop did not finish before the timeout')
    return judged


def scenario_clock_skew(ctx):
    """Skew the signed timestamp so Demo returns -1021, then allow one GET retry."""
    venue = ctx['venue']
    samples = []
    inner = venue._opener

    def opener(method, url, headers):
        refuse_non_demo_url(url)
        status, body, headers_out = _as_triple(inner(method, url, headers))
        samples.append({'method': method, 'code': _body_code(body)})
        return status, body, headers_out

    if venue._offset_ms is None:
        venue._timestamp()
    venue._opener = opener
    venue._offset_ms = venue._offset_ms + 120_000
    error = None
    try:
        venue._get('/api/v3/account', signed=True)
    except (Unknown, Blocked) as exc:
        error = str(exc)
    finally:
        venue._opener = inner
    codes = [row['code'] for row in samples]
    posts = [row for row in samples if row['method'] != 'GET']
    passed = error is None and codes.count(-1021) == 1 and not posts
    return {
        'status': 'pass' if passed else 'fail',
        'reason': None if passed else (error or 'clock skew did not produce one -1021 retry'),
        'forced_clock_offset': True,
        'simulated_transport': False,
        'codes': codes,
        'resent_post': bool(posts),
        'simulated_trigger': False,
    }


def scenario_rate_limit(ctx):
    """Inject one HTTP 429. The next call waits; it does not bypass the saved backoff."""
    venue, state = ctx['venue'], ctx['state']
    inner = venue._opener
    fired = {'done': False}

    def opener(method, url, headers):
        refuse_non_demo_url(url)
        if not fired['done'] and method == 'GET' and 'signature=' in url:
            fired['done'] = True
            return 429, b'{"code":-1003,"msg":"simulated rate limit"}', {'Retry-After': '1'}
        return inner(method, url, headers)

    venue._opener = opener
    blocked = None
    try:
        try:
            venue._get('/api/v3/account', signed=True)
        except Unknown as exc:
            blocked = str(exc)
    finally:
        venue._opener = inner
    reached = {'n': 0}

    def counting(method, url, headers):
        refuse_non_demo_url(url)
        reached['n'] += 1
        return inner(method, url, headers)

    venue._opener = counting
    second = None
    try:
        try:
            venue._get('/api/v3/account', signed=True)
            second = 'sent'
        except Unknown as exc:
            second = str(exc)
    finally:
        venue._opener = inner
    clock = ctx['monotonic']
    for _ in range(8):
        if clock() >= getattr(venue, '_retry_after_at', 0):
            break
        ctx['sleep'](min(0.5, max(0, venue._retry_after_at - clock())))
    recovered = True
    try:
        venue._get('/api/v3/account', signed=True)
    except (Unknown, Blocked):
        recovered = False
    saved = state.get('rate_limits') or {}
    passed = (blocked and 'rate limit' in blocked and second and 'backoff' in second
              and reached['n'] == 0 and 'demo-api.binance.com' in saved and recovered
              and clock() >= venue._retry_after_at)
    return {
        'status': 'pass' if passed else 'fail',
        'reason': None if passed else 'rate-limit backoff did not block and then expire',
        'simulated_transport': True,
        'simulated_trigger': False,
        'blocked': blocked,
        'while_backing_off': second,
        'requests_during_backoff': reached['n'],
        'persisted_hosts': sorted(saved),
        'resent': False,
    }


def scenario_network_loss(ctx):
    venue = ctx['venue']
    snapshot = venue.snapshot(ctx['config'].account_uid)
    if _holds(snapshot):
        return {'status': 'skipped', 'reason': 'network-loss buy needs a flat account',
                'simulated_transport': True, 'simulated_trigger': False}
    _reject_unconfirmed(ctx['lifecycle'])
    quote = probe_quote(snapshot, ctx['config'].capital_limit)
    order = {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': quote}
    identity = verification_identity(ctx['config'].scope, ctx['token'], 'network-loss', ctx['nonce'])
    guarded = venue._opener
    lost = LostResponse(guarded)
    venue._opener = lost
    row = None
    blocked_reason = None
    unknown_reason = None
    try:
        try:
            row = dispatch_order(ctx['lifecycle'], identity, order, signal_ms=ctx['token'])
        except NotSent as exc:
            blocked_reason = str(exc)
            row = _intent(ctx['lifecycle'], identity)
        except Blocked as exc:
            blocked_reason = str(exc)
            row = _intent(ctx['lifecycle'], identity)
        except Unknown as exc:
            unknown_reason = str(exc)
            row = _intent(ctx['lifecycle'], identity)
    finally:
        venue._opener = guarded
    posts = lost.post_count
    if blocked_reason and posts == 0:
        return {
            'status': 'blocked',
            'reason': blocked_reason,
            'client_id': identity,
            'order_id': None if not row else row[3].get('orderId'),
            'post_count': 0,
            'response_lost': False,
            'recovered_by_query': False,
            'resent': False,
            'simulated_transport': True,
            'simulated_trigger': False,
            'flatten': None,
        }
    for _ in range(4):
        if row and row[3].get('orderId'):
            break
        ctx['sleep'](0.5)
        try:
            ctx['lifecycle'].recover()
        except (Unknown, Blocked):
            pass
        row = _intent(ctx['lifecycle'], identity)
    flattened = None
    if row and row[3].get('status') == 'FILLED':
        flattened = _sell_free(ctx, 'network-loss-flatten')
    found = bool(row and row[3].get('orderId'))
    if posts != 1:
        status, detail = 'fail', 'lost response was resent or was not sent'
    elif not found:
        status, detail = 'unknown', unknown_reason or 'order outcome is unknown; the same identity was not resent'
    elif flattened is not None and flattened.get('status') != 'pass':
        status, detail = 'fail', flattened.get('reason')
    else:
        status, detail = 'pass', None
    return {
        'status': status,
        'reason': detail,
        'client_id': identity,
        'order_id': None if not row else row[3].get('orderId'),
        'post_count': posts,
        'response_lost': lost.lost == 1,
        'recovered_by_query': found,
        'resent': posts > 1,
        'simulated_transport': True,
        'simulated_trigger': False,
        'flatten': None if flattened is None else {'status': flattened.get('status'),
                                                   'client_id': flattened.get('client_id')},
    }


def hold_for_kill(directory, identity, ready) -> None:
    """Hold the account lock until SIGKILL. This process does not submit."""
    path = Path(ready)
    with State(directory, identity) as state:
        if state.get('identity') != identity:
            raise Blocked('state directory belongs to another account or environment')
        path.write_text(str(os.getpid()), encoding='utf-8')
        while True:
            time.sleep(3600)


def _order_ids(snapshot) -> tuple:
    return tuple(sorted(row['order_id'] for row in snapshot.get('orders') or []))


def scenario_kill_restart(config, venue, token, nonce, *, sleep=time.sleep):
    """Leave one unknown buy, SIGKILL a lock holder, and recover without a second order."""
    identity = verification_identity(config.scope, token, 'kill-restart', nonce)
    order = {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': '10.00'}
    with State(config.state_dir, config.scope) as state:
        lifecycle = Lifecycle(state, venue, config)
        try:
            _reject_unconfirmed(lifecycle)
        except Blocked as exc:
            return {'status': 'skipped', 'reason': str(exc), 'simulated_trigger': False, 'resent': False}
        before_orders = _order_ids(venue.snapshot(config.account_uid))
        lifecycle.save(identity, _order_payload(order, token), 'unknown', {})
        before_ids = _intent_ids(lifecycle)
    ready = Path(config.state_dir).expanduser() / 'verification-hold.ready'
    if ready.exists():
        ready.unlink()
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'from spotquant.demo_verify import hold_for_kill\nimport sys\n'
         'hold_for_kill(sys.argv[1], sys.argv[2], sys.argv[3])\n',
         str(Path(config.state_dir).expanduser()), config.scope, str(ready)],
        start_new_session=True, cwd=str(ROOT))
    error = None
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not ready.exists():
            if proc.poll() is not None:
                return {'status': 'fail', 'reason': 'kill holder exited before the lock was held',
                        'simulated_trigger': False, 'resent': False}
            sleep(0.05)
        if not ready.exists():
            return {'status': 'fail', 'reason': 'kill holder did not acquire the state lock',
                    'simulated_trigger': False, 'resent': False}
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        if ready.exists():
            ready.unlink()
    ids = ()
    row = None
    after_orders = ()
    for _ in range(40):
        try:
            with State(config.state_dir, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                try:
                    lifecycle.recover()
                except (Unknown, Blocked) as exc:
                    error = str(exc)
                ids = _intent_ids(lifecycle)
                row = _intent(lifecycle, identity)
            after_orders = _order_ids(venue.snapshot(config.account_uid))
            break
        except Blocked:
            sleep(0.05)
    else:
        return {'status': 'fail', 'reason': 'state lock was not released after SIGKILL',
                'simulated_trigger': False, 'resent': False}
    still_unknown = (row is not None and row[2] == 'unknown' and not row[3].get('orderId'))
    passed = (ids == before_ids and after_orders == before_orders and still_unknown
              and error is not None and 'never resubmitted' in error)
    return {
        'status': 'pass' if passed else 'fail',
        'reason': None if passed else 'restart changed orders or resubmitted the unknown identity',
        'client_id': identity,
        'intent_ids_unchanged': ids == before_ids,
        'exchange_orders_unchanged': after_orders == before_orders,
        'still_unknown': still_unknown,
        'recovery_error': error,
        'resent': False,
        'simulated_trigger': False,
        'reconcile_note': '此场景故意留下一张没有交易所编号的未知买单。对账应报告未确认，不能把它删掉当作未发送。',
    }


SCENARIOS = {
    'market-buy': scenario_market_buy,
    'stop-place': scenario_stop_place,
    'stop-replace': scenario_stop_replace,
    'adverse-exit': scenario_adverse_exit,
    'sma-exit': scenario_sma_exit,
    'clock-skew': scenario_clock_skew,
    'rate-limit': scenario_rate_limit,
    'network-loss': scenario_network_loss,
}


def _run_one(name, ctx) -> dict:
    started = time.time()
    try:
        record = SCENARIOS[name](ctx)
    except NotSent as exc:
        record = {'status': 'blocked', 'reason': str(exc), 'resent': False}
    except Unknown as exc:
        record = {'status': 'unknown', 'reason': str(exc), 'resent': False}
    except Blocked as exc:
        record = {'status': 'fail', 'reason': str(exc), 'resent': False}
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        record = {'status': 'fail', 'reason': 'verification scenario failed before a conclusion', 'resent': False}
    record['scenario'] = name
    record['elapsed_seconds'] = round(time.time() - started, 3)
    return serial(record)


def _rollup(records) -> str:
    if not records:
        return 'failed'
    statuses = [row.get('status') for row in records]
    if 'unknown' in statuses:
        return 'unknown'
    if 'blocked' in statuses:
        return 'blocked'
    if any(status != 'pass' for status in statuses):
        return 'failed'
    return 'pass'


def _reason(status, records=()) -> str:
    if status == 'pass':
        return 'Demo verification scenarios passed; mainnet execution is not verified'
    if status == 'unknown':
        return 'Demo verification has an unconfirmed order; it was not resent'
    if status == 'blocked':
        for row in records:
            if row.get('status') == 'blocked' and row.get('reason'):
                return row['reason']
        return 'Demo verification stopped before sending; a precondition is not met'
    return 'Demo verification scenarios did not all pass'


def _bnb_total(account) -> D:
    if not isinstance(account, dict) or not isinstance(account.get('balances'), list):
        raise Unknown('account response is missing balances')
    total = D(0)
    for row in account['balances']:
        if not isinstance(row, dict) or row.get('asset') != 'BNB':
            continue
        total += number(row.get('free', '0'), 'BNB', nonnegative=True)
        total += number(row.get('locked', '0'), 'BNB', nonnegative=True)
    return total


def assert_demo_fee_preflight(venue) -> dict:
    """Allow a Demo buy only when the stuck BNB discount has no BNB to spend.

    The check is repeated inside each ``submit`` from a fresh account read.
    A positive BNB balance, or any non-Demo host, still refuses the buy.
    """
    try:
        payload = venue._get('/api/v3/account/commission', {'symbol': 'BTCUSDT'}, signed=True)
        account = venue._get('/api/v3/account', signed=True)
    except (Unknown, Blocked) as exc:
        raise Unknown(f'Demo fee discount could not be read ({exc})') from exc
    discount = payload.get('discount') if isinstance(payload, dict) else None
    if (not isinstance(discount, dict) or type(discount.get('enabledForAccount')) is not bool
            or type(discount.get('enabledForSymbol')) is not bool):
        raise Unknown('commission discount mode is incomplete')
    third_asset = discount['enabledForAccount'] and discount['enabledForSymbol']
    bnb = _bnb_total(account)
    allowance = third_asset and allow_demo_bnb_discount(venue.environment, venue.base, bnb)
    if third_asset and not allowance:
        if bnb > 0:
            raise Blocked(
                'Demo BNB balance is not zero while the BNB fee discount is enabled; the buy stays refused')
        raise Blocked('BNB fee discount is enabled; the buy stays refused')
    return {'demo_bnb_discount_allowance': allowance}


def render_verification_md(report: dict) -> str:
    lines = [
        '# Demo 执行核对',
        '',
        f"- 结论：{report.get('status')}",
        f"- 账户：{report.get('account_uid')}",
        f"- 资金上限：{report.get('capital_limit_usdt')} USDT",
        '- native_execution_verified：false',
        f"- Demo BNB 折扣豁免：{report.get('demo_bnb_discount_allowance') is True}",
        '',
    ]
    for item in report.get('limitations') or []:
        lines.append(f'- {item}')
    lines.extend(['', '## 场景', ''])
    rows = report.get('scenarios') or []
    if not rows:
        lines.append('无。预览未下单。')
    else:
        lines.append('| 场景 | 结果 | 说明 |')
        lines.append('| --- | --- | --- |')
        for row in rows:
            detail = row.get('reason') or row.get('note') or ''
            if row.get('unprotected_window_ms') is not None:
                detail = f"本地空窗 {row['unprotected_window_ms']} ms；交易所空窗 {row.get('exchange_unprotected_window_ms')} ms"
            lines.append(f"| {row.get('scenario')} | {row.get('status')} | {str(detail).replace('|', '/')} |")
    lines.append('')
    return '\n'.join(lines)


def _atomic(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def _guard_text(report, venue) -> dict:
    body = serial(report)
    text = json.dumps(body, ensure_ascii=False)
    for secret in (getattr(venue, 'key', ''), getattr(venue, 'secret', '')):
        if secret and secret in text:
            raise Blocked('verification report would contain a credential')
    return body


def write_verification(directory, report, venue) -> dict:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = dict(report, results_dir=str(directory))
    body = _guard_text(report, venue)
    _atomic(directory / 'demo-verification.json', json.dumps(body, indent=2, ensure_ascii=False) + '\n')
    _atomic(directory / 'demo-verification.md', render_verification_md(body))
    return body


def _preview(snapshot) -> dict:
    return {
        'btc': format(D(snapshot['btc']), 'f'),
        'usdt_free': format(D(snapshot['usdt_free']), 'f'),
        'last_price': format(D(snapshot['last_price']), 'f'),
        'can_trade': snapshot.get('can_trade') is True,
        'fee_mode': snapshot.get('fee_mode'),
    }


def execute_verification(config, venue, *, execute, faults=False, scenarios=None, out=None,
                         config_path=None, sleep=time.sleep, monotonic=time.monotonic,
                         popen=subprocess.Popen, interrupt_after=2.0, graceful_timeout=900):
    assert_demo_config(config, capital=True)
    install_demo_guard(venue)
    fee_preflight = assert_demo_fee_preflight(venue)
    names = select_scenarios(scenarios, faults)
    out_dir = Path(out) if out is not None else Path(config.state_dir).expanduser() / 'verification'
    venue._monotonic = monotonic
    report = {
        'status': 'read_only',
        'native_execution_verified': False,
        'environment': 'demo',
        'account_uid': config.account_uid,
        'capital_limit_usdt': str(config.capital_limit),
        'execute': execute,
        'faults': bool(faults),
        'limitations': list(LIMITATIONS),
        'scenarios': [],
        'planned_scenarios': list(names),
        'demo_bnb_discount_allowance': fee_preflight['demo_bnb_discount_allowance'],
    }
    deferred = [name for name in names if name in ('graceful-stop', 'kill-restart')]
    token = None
    with State(config.state_dir, config.scope) as state:
        if hasattr(venue, 'bind_state'):
            venue.bind_state(state)
        if _checkpoint(state):
            raise Blocked('demo verification only uses a state directory without a strategy checkpoint')
        if not execute:
            snapshot = venue.snapshot(config.account_uid)
            report['preview'] = _preview(snapshot)
            report['reason'] = 'Demo verification preview; no order sent'
            state.report(serial(report))
            return write_verification(out_dir, report, venue)
        if any(status in ('unknown', 'canceling', 'partial')
               for _, _, status, _ in Lifecycle(state, venue, config).rows()):
            raise Blocked('an unconfirmed verification order is still open; reconcile it before another order')
        token = _next_token(state)
        nonce = _verification_nonce(state)
        ctx = {
            'config': config,
            'config_path': config_path,
            'venue': venue,
            'state': state,
            'lifecycle': Lifecycle(state, venue, config),
            'token': token,
            'nonce': nonce,
            'sleep': sleep,
            'monotonic': monotonic,
            'popen': popen,
            'interrupt_after': interrupt_after,
            'graceful_timeout': graceful_timeout,
            'entry': None,
        }
        for name in names:
            if name in deferred:
                continue
            report['scenarios'].append(_run_one(name, ctx))
            write_verification(out_dir, report, venue)
        state.report(serial(report))
    if 'graceful-stop' in names:
        ctx = {
            'config': config,
            'config_path': config_path,
            'sleep': sleep,
            'popen': popen,
            'interrupt_after': interrupt_after,
            'graceful_timeout': graceful_timeout,
        }
        started = time.time()
        try:
            record = scenario_graceful_stop(ctx)
        except (Blocked, Unknown, OSError, ValueError) as exc:
            record = {'status': 'fail', 'reason': str(exc), 'simulated_trigger': False}
        record['scenario'] = 'graceful-stop'
        record['elapsed_seconds'] = round(time.time() - started, 3)
        report['scenarios'].append(serial(record))
        write_verification(out_dir, report, venue)
    if 'kill-restart' in names:
        started = time.time()
        try:
            record = scenario_kill_restart(config, venue, token, nonce, sleep=sleep)
        except (Blocked, Unknown, OSError, ValueError) as exc:
            record = {'status': 'fail', 'reason': str(exc), 'resent': False, 'simulated_trigger': False}
        record['scenario'] = 'kill-restart'
        record['elapsed_seconds'] = round(time.time() - started, 3)
        report['scenarios'].append(serial(record))
    report['status'] = _rollup(report['scenarios'])
    report['reason'] = _reason(report['status'], report['scenarios'])
    if 'graceful-stop' not in names:
        with State(config.state_dir, config.scope) as state:
            state.report(serial(report))
    return write_verification(out_dir, report, venue)
