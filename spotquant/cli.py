"""Binance spot observation and explicitly capped owner-operated sessions."""
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
        description='BTCUSDT spot observation. Live orders stay off unless a capped run is authorized.',
    )
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'run'):
        command = commands.add_parser(name)
        command.add_argument('--config', default='config.json')
        if name == 'run':
            command.add_argument('--execute', action='store_true',
                                 help='With --authorize-uid, run a capped live session. Otherwise blocked.')
            command.add_argument('--authorize-uid', help='Repeat the live account UID for this invocation')
    exported = commands.add_parser('snapshot', help='Export a fresh read-only BTC account snapshot')
    exported.add_argument('--config', default='config.json')
    exported.add_argument('--out', type=Path, required=True)
    demo = commands.add_parser('demo-check', help='Owner-operated bounded Demo execution')
    demo.add_argument('--config', required=True)
    demo.add_argument('--execute', action='store_true')
    demo.add_argument('--authorize-uid', help='Repeat the dedicated Demo account UID for this invocation')
    args = parser.parse_args(argv)
    try:
        if args.command == 'run' and args.execute and not args.authorize_uid:
            # Before config, credentials, and network.
            raise Blocked('Live execution is unavailable')
        if args.command == 'snapshot':
            from .snapshot import export
            config = load(args.config)
            report = dict(export(config, connect(config)), status='read_only')
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
