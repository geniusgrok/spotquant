"""One Binance spot observation entrypoint. Unqualified execution stays blocked."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .binance import Binance
from .config import load
from .session import cycle, run
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


def observe(config_path) -> dict:
    config = load(config_path)
    venue = connect(config)
    with State(config.state_dir, config.scope) as state:
        current = cycle(venue, state, config)
        report = dict(
            status='read_only', exchange='Binance', environment=config.environment,
            symbol='BTCUSDT', market='spot', leverage='0', qualification='NOT_QUALIFIED',
            write_attempted=False, reason='Account observation only',
        )
        report.update(current)
        if state.pending():
            report.update(status='unknown', reason='Durable intents remain unresolved; no new risk authorized')
        report['pending_intents'] = len(state.pending())
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
                                 help='Blocked. Economic and native qualification are not met.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'run' and args.execute:
            # Before config, credentials, and network.
            raise Blocked('Native spot validation and economic acceptance remain incomplete; execution unavailable')
        if args.command == 'status':
            report = observe(args.config)
        else:
            config = load(args.config)
            report = run(config, connect(config))
    except (Blocked, Unknown) as exc:
        report = dict(status='unknown' if isinstance(exc, Unknown) else 'blocked', reason=str(exc))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        report = dict(status='unknown', reason='Invalid input or unexpected schema; no success inferred')
    print(json.dumps(serial(report), indent=2, allow_nan=False))
    print(report['status'] + ': ' + report.get('reason', ''), file=sys.stderr)
    return 2 if report['status'] in ('unknown', 'blocked', 'failed', 'partial') else 0
