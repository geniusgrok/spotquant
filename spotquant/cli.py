"""One Binance spot observation entrypoint. Unqualified execution stays blocked."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .binance import Binance
from .config import load
from .model import SLEEVES
from .session import RECORDED_LIMITS, clear_stale, cycle, run
from .state import State
from .types import Blocked, Unknown, serial

CREDENTIALS = {
    'live': ('SPOTQUANT_BINANCE_KEY', 'SPOTQUANT_BINANCE_SECRET'),
    'demo': ('SPOTQUANT_BINANCE_DEMO_KEY', 'SPOTQUANT_BINANCE_DEMO_SECRET'),
}


def connect(config, *, demo_execute=False):
    """Read-only adapter. Demo credentials never go to the live host."""
    names = CREDENTIALS[config.environment]
    key, secret = (os.environ.get(name, '') for name in names)
    if not key or not secret:
        raise Blocked(
            f'explicit Binance {config.environment} read credentials required ({names[0]}, {names[1]})')
    return Binance(key=key, secret=secret, environment=config.environment, capital_limit=config.capital_limit,
                   demo_execution_uid=config.account_uid if demo_execute else None)


def _base_report(config) -> dict:
    return dict(
        status='read_only', exchange='Binance', environment=config.environment,
        symbol='BTCUSDT', market='spot', leverage='0', sleeves=list(SLEEVES),
        qualification='NOT_QUALIFIED',
        write_attempted=False, observation_current=False,
        recorded_limits=dict(RECORDED_LIMITS),
        reason='Account observation only',
    )


def _failure_report(config, exc) -> dict:
    report = _base_report(config)
    report.update(
        status='unknown' if isinstance(exc, Unknown) else 'blocked',
        reason=str(exc),
        observation_current=False,
        write_attempted=False,
    )
    clear_stale(report)
    return report


def _persist(config, report: dict) -> dict:
    with State(config.state_dir, config.scope) as state:
        state.report(report)
    return report


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
            report['reason'] = 'Invalid observation or state'
            report['status'] = 'unknown'
        state.report(report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='spotquant',
        description='Spotquant Binance BTCUSDT spot observation. Execution stays blocked.',
    )
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'run'):
        command = commands.add_parser(name)
        command.add_argument('--config', default='config.json')
        if name == 'run':
            command.add_argument('--execute', action='store_true',
                                 help='Blocked. Native qualification stays NOT_QUALIFIED.')
    exported = commands.add_parser('snapshot', help='Fresh read-only account JSON for research.operations combine')
    exported.add_argument('--config', default='config.json')
    exported.add_argument('--out', type=Path, required=True)
    demo = commands.add_parser('demo-check', help='Owner-operated bounded Demo execution; does not qualify live')
    demo.add_argument('--config', required=True)
    demo.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'run' and args.execute:
            # Before config, credentials, and network.
            raise Blocked('Native qualification is NOT_QUALIFIED; execution unavailable')
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
            report = run(config, connect(config, demo_execute=args.execute), execute=args.execute)
        elif args.command == 'status':
            report = observe(args.config)
        else:
            config = load(args.config)
            try:
                venue = connect(config)
            except (Blocked, Unknown) as exc:
                report = _persist(config, _failure_report(config, exc))
            else:
                report = run(config, venue)
    except (Blocked, Unknown) as exc:
        report = dict(status='unknown' if isinstance(exc, Unknown) else 'blocked', reason=str(exc))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        report = dict(status='unknown', reason='Invalid input or unexpected schema; no success inferred')
    print(json.dumps(serial(report), indent=2, allow_nan=False))
    print(report['status'] + ': ' + report.get('reason', ''), file=sys.stderr)
    return 2 if report['status'] in ('unknown', 'blocked', 'failed', 'partial') else 0
