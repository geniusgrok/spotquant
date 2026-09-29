"""One Binance spot observation entrypoint. Unqualified execution stays blocked."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .binance import Binance
from .config import load
from .session import RECORDED_LIMITS, clear_stale, cycle, run
from .state import State
from .types import Blocked, Unknown, serial

CREDENTIALS = {
    'live': ('SPOTQUANT_BINANCE_KEY', 'SPOTQUANT_BINANCE_SECRET'),
    'demo': ('SPOTQUANT_BINANCE_DEMO_KEY', 'SPOTQUANT_BINANCE_DEMO_SECRET'),
}


def connect(config):
    """Read-only adapter. Demo credentials never go to the live host."""
    names = CREDENTIALS[config.environment]
    key, secret = (os.environ.get(name, '') for name in names)
    if not key or not secret:
        raise Blocked(
            f'explicit Binance {config.environment} read credentials required ({names[0]}, {names[1]})')
    return Binance(key=key, secret=secret, environment=config.environment, capital_limit=config.capital_limit)


def _base_report(config) -> dict:
    return dict(
        status='read_only', exchange='Binance', environment=config.environment,
        symbol='BTCUSDT', market='spot', leverage='0', qualification='NOT_QUALIFIED',
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
    args = parser.parse_args(argv)
    try:
        if args.command == 'run' and args.execute:
            # Before config, credentials, and network.
            raise Blocked('Native qualification is NOT_QUALIFIED; execution unavailable')
        if args.command == 'status':
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
