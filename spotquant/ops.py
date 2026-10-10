"""Unattended session helpers: drawdown halt, kill switch, backup, heartbeats.

Strategy parameters are not changed here. A halt stops new buys only.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from time import time

from .notify import (
    ALERT_WINDOW_SECONDS, body_for, collect_alerts, deliver, heartbeat_message,
    missed_run_alert, remember_sent, subject_for, unsent,
)
from .types import Blocked, NotSent, Unknown, floor_step, number

HALT_FILENAME = 'HALT'
HEARTBEAT_FILENAME = 'heartbeat.json'
DEDUPE_FILENAME = 'alert-dedupe.json'
BACKUP_KEEP = 14
# The daily session is 00:45 UTC. A heartbeat from that run is late 26 hours
# later, so the hourly checker alerts from about 02:45 UTC the next day.
HEARTBEAT_MAX_AGE_SECONDS = 26 * 3600
BASE_STEP = D('0.00001')
MIN_NOTIONAL = D('5')


def halt_path(state) -> Path:
    return Path(state.directory) / HALT_FILENAME


def account_equity(snapshot: dict):
    if snapshot.get('last_price') is None or snapshot.get('btc') is None or snapshot.get('usdt_free') is None:
        return None
    locked = snapshot.get('usdt_locked') or 0
    return D(snapshot['usdt_free']) + D(locked) + D(snapshot['btc']) * D(snapshot['last_price'])


def note_equity(state, snapshot: dict, config) -> dict:
    """Remember initial and peak equity. Drawdown is measured from the peak."""
    equity = account_equity(snapshot)
    if equity is None:
        return {'equity': None, 'initial': None, 'peak': None, 'drawdown': None, 'buy_halt': None}
    prior = state.get('equity_mark') or {}
    if prior:
        initial = D(prior['initial'])
        peak = max(D(prior['peak']), equity)
    else:
        initial = peak = equity
    drawdown = D(0) if peak <= 0 else (peak - equity) / peak
    state.set('equity_mark', {
        'initial': format(initial, 'f'),
        'peak': format(peak, 'f'),
        'last': format(equity, 'f'),
        'drawdown': format(drawdown, 'f'),
    })
    return {
        'equity': format(equity, 'f'),
        'initial': format(initial, 'f'),
        'peak': format(peak, 'f'),
        'drawdown': format(drawdown, 'f'),
        'buy_halt': buy_halt_reason(state, config, snapshot, drawdown=drawdown),
    }


def buy_halt_reason(state, config, snapshot: dict, *, drawdown=None) -> str | None:
    """Why a new buy must not be sent. None means buys stay allowed."""
    if halt_path(state).exists():
        return 'halt file is present; new buys are stopped'
    limit = getattr(config, 'drawdown_halt_limit', None)
    if limit is None:
        return None
    if drawdown is None:
        mark = state.get('equity_mark') or {}
        if not mark:
            return None
        equity = account_equity(snapshot)
        if equity is None:
            return None
        peak = D(mark['peak'])
        drawdown = D(0) if peak <= 0 else (peak - equity) / peak
    if drawdown >= limit:
        return (f'drawdown {format(drawdown, "f")} reached max_drawdown_halt_pct '
                f'{format(limit, "f")}; new buys are stopped')
    return None


def backup_database(db_path: Path, dest_dir: Path, *, keep: int = BACKUP_KEEP, now: float | None = None) -> Path:
    """Online SQLite backup. Does not take the execution lock."""
    if keep < 1:
        raise Blocked('backup rotation must keep at least one file')
    db_path = Path(db_path)
    if not db_path.is_file():
        raise Blocked('state database is missing')
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(time() if now is None else now, timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest = dest_dir / f'intents-{stamp}.sqlite'
    source = sqlite3.connect(db_path, timeout=30)
    target = sqlite3.connect(dest)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()
    files = sorted(dest_dir.glob('intents-*.sqlite'))
    while len(files) > keep:
        files.pop(0).unlink()
    return dest


def _recorded_qty(state) -> tuple[D, bool]:
    """Owned sleeve quantity, and whether it is already marked untradable dust."""
    position = (state.get('positions') or {}).get('40')
    if not isinstance(position, dict):
        return D(0), False
    try:
        qty = number(position.get('qty'), 'position', nonnegative=True)
    except Blocked:
        return D(0), False
    return qty, bool(position.get('dust'))


def _tradable(qty: D, snapshot: dict, *, dust: bool) -> bool:
    if dust or qty <= 0:
        return False
    price = D(snapshot.get('avg_price') or snapshot.get('last_price') or 0)
    minimum = D(snapshot.get('min_notional') or MIN_NOTIONAL)
    return qty * price >= minimum and floor_step(qty, BASE_STEP) > 0


def _market_sells(lifecycle) -> list:
    return [row for row in lifecycle.rows()
            if (row[1].get('order') or {}).get('side') == 'SELL'
            and (row[1].get('order') or {}).get('type') == 'MARKET']


def _confirmed_sold(lifecycle) -> str | None:
    """Sum native executed quantities. The requested quantity is never used."""
    total = D(0)
    confirmed = False
    for _, payload, _, result in _market_sells(lifecycle):
        if result.get('orderId') is None or result.get('executedQty') is None:
            continue
        total += number(result['executedQty'], nonnegative=True)
        confirmed = True
    if confirmed:
        return format(total, 'f')
    if any(row[2] == 'rejected' for row in _market_sells(lifecycle)):
        return '0'
    return None


def _stop_resting(lifecycle) -> bool:
    return any(status == 'resting' and (payload.get('order') or {}).get('type') == 'STOP_LOSS'
               for _, payload, status, _ in lifecycle.rows())


def _unresolved_sell(lifecycle) -> bool:
    return any(row[2] in ('unknown', 'resting', 'canceling') for row in _market_sells(lifecycle))


def _account_exit(state, venue, lifecycle, config) -> None:
    """Apply confirmed sells with the same book path a session uses."""
    from .model import SLEEVES, Model
    from .session import _commit, _fold
    saved = state.get('models')
    if not saved:
        raise Unknown('kill-switch requires the recorded model checkpoint')
    models = {window: Model.restore(saved[str(window)]) for window in SLEEVES}
    state._execution_owners = lifecycle.owners()
    state._staged = None
    snapshot = venue.snapshot(config.account_uid)
    positions, follows, _exit_through = _fold(state, venue, models, snapshot)
    _commit(state, models, positions, follows, False, models[SLEEVES[0]].last)


def _restore_protection(lifecycle, state, venue, config, bar) -> bool:
    """Put a resting stop back on a remainder. False means it is still unprotected."""
    snapshot = venue.snapshot(config.account_uid)
    snapshot['stop_price_percent_band'] = config.stop_price_percent_band is True
    positions = state.get('positions') or {}
    follows = state.get('follows') or {}
    qty, dust = _recorded_qty(state)
    if not _tradable(qty, snapshot, dust=dust):
        return _stop_resting(lifecycle)
    try:
        for _ in range(2):
            if _stop_resting(lifecycle):
                return True
            if not lifecycle._protect_unsold(bar, positions, follows, snapshot):
                break
    except (Unknown, Blocked):
        return _stop_resting(lifecycle)
    return _stop_resting(lifecycle)


def _kill_bar(state, lifecycle) -> int:
    for _, payload, status, _ in _market_sells(lifecycle):
        if status in ('prepared', 'unknown', 'resting', 'canceling') and payload.get('signal_ms') is not None:
            return int(payload['signal_ms'])
    position = (state.get('positions') or {}).get('40')
    if isinstance(position, dict) and position.get('first_ms') is not None:
        return int(position['first_ms'])
    saved = state.get('models') or {}
    body = (saved.get('40') or {}).get('body') or {}
    if body.get('last') is None:
        raise Unknown('kill-switch has no recorded position time')
    return int(body['last'])


def _finish_kill(state, config, lifecycle, *, status: str, reason: str | None, cancelled: list,
                 extra: D, manual_takeover: bool, env=None, smtp_ssl=None, smtp_plain=None,
                 urlopen=None, now: float | None = None) -> dict:
    qty, dust = _recorded_qty(state)
    report = {
        'status': status,
        'environment': config.environment,
        'cancelled': list(cancelled),
        'sold': _confirmed_sold(lifecycle),
        'residual_btc': format(qty, 'f'),
        'dust': dust or (qty > 0 and status == 'pass'),
        'manual_takeover': manual_takeover,
        'unexplained_btc': format(extra, 'f'),
        'native_execution_verified': False,
    }
    if reason:
        report['reason'] = reason
    if status != 'pass':
        report['notifications'] = dispatch_notifications(
            Path(state.directory), report, exit_code=2, now=now, env=env,
            smtp_ssl=smtp_ssl, smtp_plain=smtp_plain, urlopen=urlopen)
    return report


def kill_switch(state, venue, config, *, confirm: bool, env=None, smtp_ssl=None, smtp_plain=None,
                urlopen=None, now: float | None = None) -> dict:
    """Cancel this state's orders and sell its recorded position through the normal exit lifecycle.

    The sell intent, its client id, and any cancel id are saved before those
    requests are sent. ``sold`` counts only native executed quantity. BTC this
    state does not record is not sold. ``confirm`` must be true or nothing is sent.
    """
    if confirm is not True:
        raise Blocked('kill-switch requires --confirm')
    if config.environment not in ('demo', 'live'):
        raise Blocked('kill-switch requires demo or live')
    if config.capital_limit is None:
        raise Blocked('kill-switch requires a capital ceiling')
    from .execution import Lifecycle
    snapshot = venue.snapshot(config.account_uid)
    owned, dust = _recorded_qty(state)
    account_btc = D(snapshot['btc'])
    extra = max(account_btc - owned, D(0))
    minimum = D(snapshot.get('min_notional') or MIN_NOTIONAL)
    if owned <= 0 and account_btc * D(snapshot['last_price']) >= minimum:
        raise Blocked('kill-switch will not adopt BTC that has no recorded position')
    notify = dict(env=env, smtp_ssl=smtp_ssl, smtp_plain=smtp_plain, urlopen=urlopen, now=now)
    if owned > 0 and not state.get('models'):
        raise Unknown('kill-switch requires the recorded model checkpoint')
    lifecycle = Lifecycle(state, venue, config)
    cancelled = []

    def finish(status, reason=None, takeover=False):
        return _finish_kill(state, config, lifecycle, status=status, reason=reason,
                            cancelled=cancelled, extra=extra, manual_takeover=takeover, **notify)

    try:
        lifecycle.recover()
    except Unknown as exc:
        return finish('unknown', str(exc), takeover=True)
    if not _market_sells(lifecycle) and not _tradable(owned, snapshot, dust=dust):
        try:
            lifecycle.verify(snapshot)
        except Unknown as exc:
            return finish('unknown', str(exc), takeover=not _stop_resting(lifecycle))
        return finish('pass')
    bar = _kill_bar(state, lifecycle)
    follows = state.get('follows') or {}
    unresolved = None
    # One session allows twelve actions. A finished session clock is not reused:
    # this command is the operator's exit, not a continuation of that clock.
    for _ in range(12):
        try:
            lifecycle.recover()
        except Unknown as exc:
            return finish('unknown', str(exc), takeover=True)
        if _unresolved_sell(lifecycle):
            return finish('unknown', 'sell is not confirmed; the original identity is not sent again',
                          takeover=True)
        try:
            _account_exit(state, venue, lifecycle, config)
        except Unknown as exc:
            return finish('unknown', str(exc), takeover=not _stop_resting(lifecycle))
        prepared = [row for row in _market_sells(lifecycle) if row[2] == 'prepared']
        fresh = venue.snapshot(config.account_uid)
        qty, dust = _recorded_qty(state)
        # Size from the recorded position. A resting stop locks btc_free until it is cancelled.
        free = floor_step(qty, BASE_STEP)
        if not prepared and not _tradable(free, fresh, dust=dust):
            unresolved = None
            break
        try:
            lifecycle.verify(fresh)
        except Unknown as exc:
            return finish('unknown', str(exc), takeover=not _stop_resting(lifecycle))
        if not prepared:
            order = {
                'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET',
                'quantity': format(free, 'f'), 'sleeves': [40],
            }
            identity = lifecycle.prepare(order, bar, state.get('positions') or {}, follows)
            prepared = [row for row in lifecycle.rows() if row[0] == identity]
        if prepared[0][2] != 'prepared':
            continue
        try:
            for stop_id, payload, status, _ in list(lifecycle.rows()):
                if status == 'resting' and (payload.get('order') or {}).get('type') == 'STOP_LOSS':
                    lifecycle.cancel(stop_id)
                    cancelled.append(stop_id)
            lifecycle.send(prepared[0][0])
        except Unknown as exc:
            return finish('unknown', str(exc), takeover=True)
        except (Blocked, NotSent) as exc:
            protected = _restore_protection(lifecycle, state, venue, config, bar)
            status = 'partial' if protected else 'unknown'
            reason = str(exc) if protected else str(exc) + '; residual position is unprotected'
            return finish(status, reason, takeover=not protected)
    else:
        unresolved = unresolved or 'kill-switch stopped before the recorded position was flat'
    qty, dust = _recorded_qty(state)
    fresh = venue.snapshot(config.account_uid)
    if _unresolved_sell(lifecycle):
        return finish('unknown', 'sell is not confirmed; the original identity is not sent again',
                      takeover=True)
    if unresolved or _tradable(qty, fresh, dust=dust):
        protected = _stop_resting(lifecycle) or _restore_protection(lifecycle, state, venue, config, bar)
        status = 'partial' if protected else 'unknown'
        reason = unresolved or 'recorded position remains after kill-switch'
        if not protected:
            reason += '; residual position is unprotected'
        return finish(status, reason, takeover=not protected)
    try:
        lifecycle.verify(fresh)
    except Unknown as exc:
        return finish('unknown', str(exc), takeover=not _stop_resting(lifecycle))
    return finish('pass')


def write_heartbeat(directory: Path, report: dict, now: float | None = None) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        'at': time() if now is None else now,
        'status': report.get('status'),
        'environment': report.get('environment'),
    }
    path = directory / HEARTBEAT_FILENAME
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
    os.replace(temporary, path)
    return path


def read_heartbeat(directory: Path) -> dict | None:
    path = Path(directory) / HEARTBEAT_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def dispatch_notifications(directory: Path, report: dict, *, exit_code: int = 0, now: float | None = None,
                           heartbeat: bool = False, env=None, smtp_ssl=None, smtp_plain=None,
                           urlopen=None) -> dict:
    """Send new alerts and, when asked, one daily heartbeat. Delivery failure is returned, not hidden."""
    now = time() if now is None else now
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    items = collect_alerts(report, exit_code=exit_code)
    if heartbeat:
        beat = heartbeat_message(report)
        beat['day'] = day
        items.append(beat)
    pending = unsent(Path(directory) / DEDUPE_FILENAME, items, now, ALERT_WINDOW_SECONDS)
    sent = []
    errors = []
    delivered = []
    for item in pending:
        try:
            channels = deliver(subject_for(item), body_for(item, environment=report.get('environment')),
                               env=env, smtp_ssl=smtp_ssl, smtp_plain=smtp_plain, urlopen=urlopen)
        except Blocked as exc:
            errors.append({'key': item['key'], 'reason': str(exc)})
            continue
        delivered.append(item)
        sent.append({'key': item['key'], 'channels': channels, 'subject': subject_for(item)})
    if delivered:
        remember_sent(Path(directory) / DEDUPE_FILENAME, delivered, now)
    return {'sent': sent, 'skipped': len(items) - len(pending), 'errors': errors}


def live_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return Path(env.get('SPOTQUANT_LIVE_ENABLE_FILE', '/etc/spotquant/LIVE_ENABLED')).is_file()


def execute_ops(config, venue, *, expect_environment: str, run_session, env=None, smtp_ssl=None,
                smtp_plain=None, urlopen=None, now: float | None = None) -> dict:
    """One scheduled session. A busy lock is skipped. Live needs the enable file."""
    if config.environment != expect_environment:
        raise Blocked(f'ops-run expected environment {expect_environment}')
    if expect_environment == 'live' and not live_enabled(env):
        raise Blocked('live execution is not explicitly enabled')
    try:
        report = run_session(config, venue)
    except Blocked as exc:
        if 'execution lock' in str(exc):
            return {'status': 'pass', 'skipped': True, 'reason': str(exc),
                    'environment': config.environment, 'native_execution_verified': False}
        raise
    if not isinstance(report, dict):
        report = {'status': 'unknown', 'reason': 'session returned no report',
                  'environment': config.environment}
    directory = Path(config.state_dir).expanduser()
    write_heartbeat(directory, report, now)
    exit_code = 0 if report.get('status') not in ('unknown', 'blocked', 'failed', 'partial') else 2
    notes = dispatch_notifications(directory, report, exit_code=exit_code, now=now, heartbeat=True,
                                   env=env, smtp_ssl=smtp_ssl, smtp_plain=smtp_plain, urlopen=urlopen)
    return dict(report, notifications=notes)


def check_missed_run(directory: Path, *, now: float | None = None, max_age: float = HEARTBEAT_MAX_AGE_SECONDS,
                     env=None, smtp_ssl=None, smtp_plain=None, urlopen=None) -> dict:
    now = time() if now is None else now
    item = missed_run_alert(read_heartbeat(directory), now, max_age)
    if item is None:
        return {'status': 'pass', 'sent': [], 'reason': 'heartbeat is current'}
    pending = unsent(Path(directory) / DEDUPE_FILENAME, [item], now, ALERT_WINDOW_SECONDS)
    if not pending:
        return {'status': 'pass', 'sent': [], 'reason': 'missed-run alert already sent'}
    try:
        channels = deliver(subject_for(item), body_for(item), env=env, smtp_ssl=smtp_ssl,
                           smtp_plain=smtp_plain, urlopen=urlopen)
    except Blocked as exc:
        return {'status': 'blocked', 'sent': [], 'reason': str(exc)}
    remember_sent(Path(directory) / DEDUPE_FILENAME, pending, now)
    return {'status': 'pass', 'sent': [{'key': item['key'], 'channels': channels}], 'reason': item['detail']}
