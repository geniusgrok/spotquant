"""Binance spot observation and explicitly capped sessions.

The 28% figure is the trail target. Deployment sets stop_price_percent_band,
so the STOP_LOSS stopPrice can be the percent-band floor instead.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .binance import Binance
from .config import load
from .crowding import RULE
from .model import SLEEVES
from .session import RECORDED_LIMITS, clear_stale, cycle, run
from .state import State
from .types import Blocked, Unknown, serial

CREDENTIALS = {
    'live': ('SPOTQUANT_BINANCE_KEY', 'SPOTQUANT_BINANCE_SECRET'),
    'demo': ('SPOTQUANT_BINANCE_DEMO_KEY', 'SPOTQUANT_BINANCE_DEMO_SECRET'),
}


def connect(config, *, execute_orders=False):
    """Default read-only adapter. Demo credentials go only to the Demo host."""
    names = CREDENTIALS[config.environment]
    key, secret = (os.environ.get(name, '') for name in names)
    if not key or not secret:
        raise Blocked(
            f'explicit Binance {config.environment} read credentials required ({names[0]}, {names[1]})')
    return Binance(key=key, secret=secret, environment=config.environment, capital_limit=config.capital_limit,
                   demo_execution_uid=config.account_uid if execute_orders else None)


def _base_report(config) -> dict:
    return dict(
        status='read_only', exchange='Binance', environment=config.environment,
        symbol='BTCUSDT', market='spot', leverage='0', sleeves=list(SLEEVES),
        write_attempted=False, observation_current=False,
        recorded_limits=dict(RECORDED_LIMITS),
        runtime_identity={'rule': RULE, 'source_sha': os.environ.get('SPOTQUANT_SOURCE_SHA')},
        reason='Account observation only',
    )


def _failure_report(config, exc) -> dict:
    return dict(
        _base_report(config),
        status='unknown' if isinstance(exc, Unknown) else 'blocked',
        reason=str(exc),
    )


def observe(config_path) -> dict:
    config = load(config_path)
    with State(config.state_dir, config.scope) as state:
        try:
            venue = connect(config)
            if hasattr(venue, 'bind_state'):
                venue.bind_state(state)
            current = cycle(venue, state, config)
            report = _base_report(config)
            report.update(current)
            if state.pending():
                report.update(
                    status='unknown',
                    reason='Durable intents remain unresolved; no new risk authorized',
                    observation_current=False,
                )
                clear_stale(report)
            report['pending_intents'] = len(state.pending())
        except (Blocked, Unknown) as exc:
            report = _failure_report(config, exc)
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            report = _failure_report(config, Unknown('Invalid observation or state'))
        state.report(report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='spotquant',
        description=(
            'BTCUSDT spot observation. A write session needs --execute and a matching '
            '--authorize-uid. 28% is the trail target; stop_price_percent_band can place '
            'the STOP_LOSS at the percent-band floor. ops-run is the 00:45 UTC timer entry. '
            'kill-switch always writes HALT first, before any query, snapshot, cancel, or sell. '
            'The fresh-cross mark is committed before bind and any query. '
            'Without --confirm nothing is written and nothing is sent.'
        ),
    )
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'run'):
        command = commands.add_parser(name)
        command.add_argument('--config', default='config.json')
        if name == 'run':
            command.add_argument('--execute', action='store_true',
                                 help='With a matching --authorize-uid, run one capped session. Otherwise blocked.')
            command.add_argument('--authorize-uid', help='Repeat the live account UID for this invocation')
    exported = commands.add_parser('snapshot', help='Export a fresh read-only BTC account snapshot')
    exported.add_argument('--config', default='config.json')
    exported.add_argument('--out', type=Path, required=True)
    demo = commands.add_parser('demo-check', help='Owner-operated bounded Demo execution')
    demo.add_argument('--config', required=True)
    demo.add_argument('--execute', action='store_true')
    demo.add_argument('--authorize-uid', help='Repeat the dedicated Demo account UID for this invocation')
    verify = commands.add_parser('demo-verify', help='Record Spot Demo execution scenarios. Never sends to mainnet.')
    verify.add_argument('--config', required=True)
    verify.add_argument('--execute', action='store_true')
    verify.add_argument('--authorize-uid', help='Repeat the dedicated Demo account UID for this invocation')
    verify.add_argument('--faults', action='store_true',
                        help='Also run kill -9, lost-response, clock-skew, and injected HTTP 429 scenarios')
    verify.add_argument('--scenario', action='append', help='Run one scenario. Repeat to select several.')
    verify.add_argument('--out', type=Path, help='Directory for the JSON and Markdown report')
    reconcile = commands.add_parser('demo-reconcile', help='Compare the Demo ledger with exchange order and trade history')
    reconcile.add_argument('--config', required=True)
    reconcile.add_argument('--out', type=Path, help='Directory for the JSON and Markdown report')
    probe = commands.add_parser(
        'demo-band-probe',
        help='Demo-only STOP_LOSS depth probe. Without --execute it only reads filters and prices.')
    probe.add_argument('--config', required=True)
    probe.add_argument('--execute', action='store_true')
    probe.add_argument('--authorize-uid', help='Repeat the dedicated Demo account UID before any order')
    ops = commands.add_parser('ops-run', help='Scheduler entry. Live also requires the enable file.')
    ops.add_argument('--config', required=True)
    ops.add_argument('--environment', required=True, choices=('demo', 'live'))
    tested = commands.add_parser('notify-test', help='Send one test email (and optional webhook).')
    tested.add_argument('--config', required=True)
    checker = commands.add_parser('alert-check', help='Alert when the daily heartbeat is missing.')
    checker.add_argument('--config', required=True)
    failed = commands.add_parser('notify-failure', help='Alert that a systemd unit failed.')
    failed.add_argument('--config', required=True)
    failed.add_argument('--source', required=True)
    backup = commands.add_parser('backup', help='Online SQLite backup with rotation.')
    backup.add_argument('--config', required=True)
    backup.add_argument('--dest', type=Path)
    switch = commands.add_parser(
        'kill-switch',
        help='kill-switch always writes HALT first. Without --confirm nothing is written and nothing is sent.',
        description=(
            'kill-switch always writes HALT first, before any query, snapshot, cancel, or sell. '
            'The fresh-cross mark is committed before bind and any query, so a later failure still leaves it. '
            'Without --confirm nothing is written and nothing is sent. '
            'Delete HALT to resume; the current book then needs a fresh cross.'
        ))
    switch.add_argument('--config', required=True)
    switch.add_argument('--authorize-uid', required=True)
    switch.add_argument('--confirm', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'run' and args.execute and not args.authorize_uid:
            # Before config, credentials, and network.
            raise Blocked('Live execution is unavailable')
        if args.command == 'snapshot':
            from .snapshot import export
            config = load(args.config)
            with State(config.state_dir, config.scope) as state:
                venue = connect(config)
                if hasattr(venue, 'bind_state'):
                    venue.bind_state(state)
                report = dict(export(config, venue), status='read_only')
            with args.out.open('x') as stream:
                json.dump(report, stream, indent=2)
                stream.write('\n')
        elif args.command == 'demo-check':
            config = load(args.config)
            if config.environment != 'demo' or config.capital_limit is None:
                raise Blocked('Demo validation requires Demo UID, separate persistent state and capital ceiling')
            if args.execute and args.authorize_uid != config.account_uid:
                raise Blocked('Demo execution requires matching --authorize-uid')
            report = run(config, connect(config, execute_orders=args.execute), execute=args.execute)
        elif args.command == 'demo-verify':
            from .demo_guard import assert_demo_config
            from .demo_verify import execute_verification
            config = load(args.config)
            assert_demo_config(config, capital=True)
            if args.execute and args.authorize_uid != config.account_uid:
                raise Blocked('Demo verification requires matching --authorize-uid')
            report = execute_verification(
                config, connect(config, execute_orders=args.execute), execute=args.execute,
                faults=args.faults, scenarios=args.scenario, out=args.out, config_path=args.config)
        elif args.command == 'demo-band-probe':
            from .band_probe import execute_probe
            from .demo_guard import assert_demo_config
            config = load(args.config)
            assert_demo_config(config, capital=args.execute)
            if args.execute and args.authorize_uid != config.account_uid:
                raise Blocked('Demo band probe requires matching --authorize-uid')
            report = execute_probe(config, connect(config, execute_orders=args.execute), execute=args.execute)
        elif args.command == 'demo-reconcile':
            from .demo_guard import assert_demo_config
            from .reconcile import execute_reconcile
            config = load(args.config)
            assert_demo_config(config)
            report = execute_reconcile(config, connect(config), out=args.out)
        elif args.command == 'ops-run':
            from .ops import execute_ops
            config = load(args.config)
            report = execute_ops(config, connect(config, execute_orders=True),
                                 expect_environment=args.environment,
                                 run_session=lambda current, venue: run(current, venue, execute=True))
        elif args.command == 'notify-test':
            from .notify import body_for, deliver, subject_for
            config = load(args.config)
            item = {'kind': 'heartbeat', 'title': '通知测试',
                    'detail': '这是一封测试邮件，不是交易告警。'}
            channels = deliver(subject_for(item), body_for(item, environment=config.environment))
            report = {'status': 'pass', 'channels': channels, 'subject': subject_for(item),
                      'environment': config.environment}
        elif args.command == 'alert-check':
            from .ops import check_missed_run
            config = load(args.config)
            report = check_missed_run(Path(config.state_dir).expanduser())
        elif args.command == 'notify-failure':
            from .ops import dispatch_notifications
            config = load(args.config)
            report = dispatch_notifications(
                Path(config.state_dir).expanduser(),
                {'status': 'unknown', 'environment': config.environment,
                 'reason': f'systemd unit failed: {args.source}'},
                exit_code=1)
            report['status'] = 'blocked' if report['errors'] else 'pass'
        elif args.command == 'backup':
            from .ops import backup_database
            config = load(args.config)
            dest = args.dest or (Path(config.state_dir).expanduser() / 'backups')
            path = backup_database(Path(config.state_dir).expanduser() / 'intents.sqlite', dest)
            report = {'status': 'pass', 'backup': str(path), 'environment': config.environment}
        elif args.command == 'kill-switch':
            from .ops import _arm_halt, _record_fresh_cross, dispatch_notifications, kill_switch
            # Before config, the state directory, the venue, and the alert path.
            if not args.confirm:
                raise Blocked('kill-switch requires --confirm')
            config = load(args.config)
            if args.authorize_uid != config.account_uid:
                raise Blocked('kill-switch requires matching --authorize-uid')
            venue = connect(config, execute_orders=True)
            with State(config.state_dir, config.scope) as state:
                try:
                    # After the UID check and the state lock. Before backoff restore and any query.
                    _arm_halt(state)
                    _record_fresh_cross(state)
                    if hasattr(venue, 'bind_state'):
                        venue.bind_state(state)
                    report = kill_switch(state, venue, config, confirm=args.confirm)
                except (Blocked, Unknown, OSError) as exc:
                    failed = isinstance(exc, Unknown) or isinstance(exc, OSError)
                    report = {
                        'status': 'unknown' if failed else 'blocked',
                        'reason': str(exc),
                        'environment': config.environment,
                        'manual_takeover': failed,
                        'native_execution_verified': False,
                    }
                    report['notifications'] = dispatch_notifications(
                        Path(config.state_dir), report, exit_code=2)
        elif args.command == 'run' and args.execute:
            config = load(args.config)
            if config.environment != 'live' or config.capital_limit is None:
                raise Blocked('Live execution requires live environment and a positive capital ceiling')
            if args.authorize_uid != config.account_uid:
                raise Blocked('Live execution requires matching --authorize-uid')
            report = run(config, connect(config, execute_orders=True), execute=True)
        elif args.command == 'status':
            report = observe(args.config)
        else:
            config = load(args.config)
            try:
                venue = connect(config)
            except (Blocked, Unknown) as exc:
                report = _failure_report(config, exc)
                with State(config.state_dir, config.scope) as state:
                    state.report(report)
            else:
                report = run(config, venue)
    except (Blocked, Unknown) as exc:
        report = dict(status='unknown' if isinstance(exc, Unknown) else 'blocked', reason=str(exc))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        report = dict(status='unknown', reason='Invalid input or unexpected schema; no success inferred')
    print(json.dumps(serial(report), indent=2, allow_nan=False))
    print(report['status'] + ': ' + report.get('reason', ''), file=sys.stderr)
    return 2 if report['status'] in ('unknown', 'blocked', 'failed', 'partial') else 0
