"""Unattended session helpers: drawdown halt, kill switch, backup, heartbeats.

Strategy parameters are not changed here. A halt stops new buys only.
"""
from __future__ import annotations

import hashlib
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
from .state import client_id
from .types import Blocked, floor_step, number

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


def _intent_ids(state) -> set[str]:
    return {row[0] for row in state.db.execute('SELECT id FROM intents')}


def kill_switch(state, venue, config, *, confirm: bool) -> dict:
    """Cancel this state's orders and sell its recorded position.

    BTC that the state does not record is not sold. ``confirm`` must be true
    or nothing is sent.
    """
    if confirm is not True:
        raise Blocked('kill-switch requires --confirm')
    if config.environment not in ('demo', 'live'):
        raise Blocked('kill-switch requires demo or live')
    if config.capital_limit is None:
        raise Blocked('kill-switch requires a capital ceiling')
    snapshot = venue.snapshot(config.account_uid)
    positions = state.get('positions') or {}
    position = positions.get('40')
    owned = D(0)
    if isinstance(position, dict) and not position.get('dust'):
        try:
            owned = number(position.get('qty'), 'position', nonnegative=True)
        except Blocked:
            owned = D(0)
    account_btc = D(snapshot['btc'])
    price = D(snapshot['last_price'])
    extra = account_btc - owned
    ours = _intent_ids(state)
    cancelled = []
    for order in snapshot.get('orders') or []:
        if order.get('client_id') not in ours:
            continue
        cancel_id = 'sq-' + hashlib.sha256(
            (order['client_id'] + '|kill-switch').encode()).hexdigest()[:30]
        venue.cancel(order['client_id'], order_id=order['order_id'], cancel_id=cancel_id)
        cancelled.append(order['client_id'])
    fresh = venue.snapshot(config.account_uid)
    if owned <= 0 and D(fresh['btc']) * D(fresh['last_price']) >= D(fresh.get('min_notional') or MIN_NOTIONAL):
        raise Blocked('kill-switch will not adopt BTC that has no recorded position')
    free = floor_step(min(owned, D(fresh['btc_free'])), BASE_STEP)
    sold = None
    if free > 0 and free * D(fresh['avg_price']) >= D(fresh.get('min_notional') or MIN_NOTIONAL):
        identity = client_id(config.scope, int(time()) // 86400, 'kill-switch-sell')
        venue.submit(identity, {
            'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': format(free, 'f'),
        })
        sold = format(free, 'f')
    return {
        'status': 'pass',
        'environment': config.environment,
        'cancelled': cancelled,
        'sold': sold,
        'unexplained_btc': format(max(extra, D(0)), 'f'),
        'native_execution_verified': False,
    }


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
