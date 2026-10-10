"""Demo-only probe of STOP_LOSS prices. It does not change the 28% target.

Without ``--execute`` it only reads the symbol filters and the average price.
With ``--execute`` it buys a tiny probe, posts one stop at each depth, records
the exchange ``msg`` verbatim, then cancels and sells the probe back.
"""
from __future__ import annotations

import time
from decimal import Decimal as D
from pathlib import Path

from .config import Config
from .demo_guard import assert_demo_config, install_demo_guard
from .demo_verify import (
    _cancel_resting, _flatten_probe, _holds, _next_token, _record_buy, _verification_nonce,
    confirm_order, dispatch_order, probe_quote, verification_identity,
)
from .execution import Lifecycle
from .preview import PRICE_STEP
from .state import State
from .types import Blocked, NotSent, Unknown, floor_step, serial

DEPTHS = (D('0.15'), D('0.20'), D('0.25'), D('0.28'))


def depth_stop(average, depth) -> D:
    """Tick-floored stop at ``depth`` under the fresh average price."""
    return floor_step(D(average) * (D(1) - D(depth)), PRICE_STEP)


def _attempts(average) -> list:
    return [{
        'depth': format(depth, 'f'),
        'stop_price': format(depth_stop(average, depth), 'f'),
        'sent': False,
    } for depth in DEPTHS]


def preview_probe(config, venue) -> dict:
    """Read filters and the four prices. No order is sent."""
    assert_demo_config(config)
    install_demo_guard(venue)
    snapshot = venue.snapshot(config.account_uid)
    return {
        'status': 'read_only',
        'native_execution_verified': False,
        'environment': 'demo',
        'write_attempted': False,
        'account_uid': config.account_uid,
        'avg_price': format(D(snapshot['avg_price']), 'f'),
        'last_price': format(D(snapshot['last_price']), 'f'),
        'percent_price_by_side': snapshot.get('percent_price_by_side'),
        'percent_price': snapshot.get('percent_price'),
        'attempts': _attempts(snapshot['avg_price']),
        'reason': 'Demo band probe preview; no order sent',
    }


def _probe_config(config) -> Config:
    directory = Path(config.state_dir).expanduser() / 'band-probe'
    directory.mkdir(parents=True, exist_ok=True)
    return Config(
        account_uid=config.account_uid,
        state_dir=str(directory),
        session_seconds=config.session_seconds,
        poll_seconds=config.poll_seconds,
        environment='demo',
        capital_limit_usdt=config.capital_limit_usdt,
        stop_price_percent_band=False,
    )


def _exchange_result(venue, row) -> dict:
    error = getattr(venue, 'last_exchange_error', None) or {}
    native = row[3] if row is not None else {}
    accepted = native.get('status') == 'NEW'
    return {
        'accepted': accepted,
        'order_status': native.get('status'),
        'order_id': native.get('orderId'),
        'http_status': None if accepted else error.get('http_status'),
        'code': None if accepted else error.get('code'),
        'msg': None if accepted else error.get('msg'),
    }


def execute_probe(config, venue, *, execute, sleep=time.sleep) -> dict:
    """Preview, or buy a probe and record one exchange answer per depth."""
    if not execute:
        report = preview_probe(config, venue)
        return report
    assert_demo_config(config, capital=True)
    install_demo_guard(venue)
    venue.stop_price_percent_band = False
    probe_config = _probe_config(config)
    report = {
        'status': 'failed',
        'native_execution_verified': False,
        'environment': 'demo',
        'write_attempted': False,
        'account_uid': config.account_uid,
        'attempts': [],
        'cleanup': None,
    }
    with State(probe_config.state_dir, probe_config.scope) as state:
        if hasattr(venue, 'bind_state'):
            venue.bind_state(state)
        lifecycle = Lifecycle(state, venue, probe_config)
        token = _next_token(state)
        nonce = _verification_nonce(state)
        ctx = {'venue': venue, 'config': probe_config, 'lifecycle': lifecycle,
               'state': state, 'token': token, 'nonce': nonce, 'sleep': sleep, 'entry': None}
        snapshot = venue.snapshot(probe_config.account_uid)
        report['avg_price'] = format(D(snapshot['avg_price']), 'f')
        report['last_price'] = format(D(snapshot['last_price']), 'f')
        report['percent_price_by_side'] = snapshot.get('percent_price_by_side')
        report['percent_price'] = snapshot.get('percent_price')
        if _holds(snapshot) or snapshot.get('orders'):
            report['reason'] = 'account already holds BTC or an open order; the probe will not adopt it'
            report['attempts'] = _attempts(snapshot['avg_price'])
            return serial(report)
        # The quote helper enforces the same ceiling the verification buy uses.
        probe_quote(snapshot, probe_config.capital_limit)
        bought = None
        try:
            bought = _record_buy(ctx, 'band-probe-buy')
            report['buy'] = {'status': bought.get('status'), 'order_id': bought.get('order_id'),
                             'client_id': bought.get('client_id')}
            if bought.get('status') != 'pass':
                report['reason'] = bought.get('reason') or 'probe buy did not fill'
                report['attempts'] = _attempts(snapshot['avg_price'])
                return serial(report)
            for depth in DEPTHS:
                fresh = venue.snapshot(probe_config.account_uid)
                price = depth_stop(fresh['avg_price'], depth)
                attempt = {
                    'depth': format(depth, 'f'),
                    'stop_price': format(price, 'f'),
                    'avg_price': format(D(fresh['avg_price']), 'f'),
                    'sent': True,
                }
                qty = floor_step(D(fresh['btc_free']), D('0.00001'))
                order = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                         'quantity': format(qty, 'f'), 'stopPrice': format(price, 'f')}
                identity = verification_identity(probe_config.scope, token, f'band-{format(depth, "f")}', nonce)
                try:
                    row = dispatch_order(lifecycle, identity, order, signal_ms=token)
                    row = confirm_order(lifecycle, identity, sleep=sleep)
                    attempt.update(_exchange_result(venue, row))
                    if row[3].get('status') == 'NEW':
                        _cancel_resting(lifecycle)
                except (Blocked, NotSent) as exc:
                    attempt.update(_exchange_result(venue, None))
                    attempt['accepted'] = False
                    attempt['error'] = str(exc)
                    if attempt.get('msg') is None:
                        attempt['msg'] = str(exc)
                except Unknown as exc:
                    attempt.update(_exchange_result(venue, None))
                    attempt['accepted'] = False
                    attempt['error'] = str(exc)
                    report['attempts'].append(attempt)
                    report['status'] = 'unknown'
                    report['reason'] = str(exc)
                    raise
                report['attempts'].append(attempt)
            rejected = [row for row in report['attempts'] if row.get('accepted') is not True]
            report['status'] = 'pass'
            report['reason'] = (None if not rejected
                                else 'one or more probe stops were rejected; messages are on the attempts')
        except Unknown:
            pass
        finally:
            if bought and bought.get('status') == 'pass':
                try:
                    _cancel_resting(lifecycle)
                except (Blocked, Unknown):
                    pass
                report['cleanup'] = _flatten_probe(ctx, 'band-probe')
                if report['cleanup'].get('status') != 'pass':
                    report['status'] = 'failed'
                    report['unprotected'] = True
                    report['reason'] = report['cleanup'].get('reason') or report.get('reason')
                else:
                    report['unprotected'] = False
        report['write_attempted'] = bool(getattr(venue, 'write_attempted', False))
        return serial(report)
