"""Manual archive observation and two-account read-only risk summary. No network writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

from research.forward import SPEC, ledger, rules_sha256
from research.market import file_digest, load_daily
from research.rebuild import iso, source_identity
from spotquant.model import DAY
from spotquant.state import State
from spotquant.types import Blocked, number


def combined(snapshots, now_ms):
    if len(snapshots) != 2 or {row.get('market') for row in snapshots} != {'spot', 'perpetual'}:
        raise ValueError('provide exactly one spot and one perpetual account snapshot')
    identities, stamps, prices, equities, quantities = [], [], [], [], []
    for row in snapshots:
        if row.get('known') is not True:
            raise ValueError('unknown account cannot contribute zero to a combined report')
        uid = row.get('account_uid')
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdigit() or int(uid) <= 0:
            raise ValueError('explicit account UID required')
        if row.get('symbol') != 'BTCUSDT' or row.get('environment') not in ('demo', 'live'):
            raise ValueError('BTCUSDT and explicit account environment required')
        stamp = row.get('observed_at_ms')
        if type(stamp) is not int or not 0 <= now_ms - stamp <= 120000:
            raise ValueError('snapshot is stale or future-dated')
        identities.append((row['environment'], uid, row['market']))
        stamps.append(stamp)
        prices.append(number(row['btc_price_usdt'], positive=True))
        equities.append(number(row['equity_usdt'], positive=True))
        quantities.append(number(row['btc_position']))
        if row['market'] == 'spot' and quantities[-1] < 0:
            raise ValueError('spot cannot short or borrow')
    if len(set(identities)) != 2 or len({row[0] for row in identities}) != 1:
        raise ValueError('duplicate account or mixed demo/live environments')
    if max(stamps) - min(stamps) > 5000 or len(set(prices)) != 1:
        raise ValueError('snapshots need timestamps within 5 seconds and one common valuation price')
    equity, net, gross = sum(equities), sum(quantities), sum(abs(qty) for qty in quantities)
    price = prices[0]
    return {'status': 'read_only', 'symbol': 'BTCUSDT', 'equity_usdt': str(equity),
            'btc_net': str(net), 'btc_gross': str(gross), 'net_notional_usdt': str(net * price),
            'gross_notional_usdt': str(gross * price), 'gross_exposure_over_equity': str(gross * price / equity),
            'linear_pnl_if_btc_down_10pct_usdt': str(-D('.1') * price * net),
            'accounts': [dict(zip(('environment', 'uid', 'market'), identity)) for identity in identities],
            'snapshot_times_ms': stamps, 'btc_price_usdt': str(price),
            'inputs_verified_remotely': False, 'write_attempted': False,
            'limitations': 'Supplied snapshots; equity includes PnL, not added collateral. Linear stress omits liquidation, funding and exit costs.',
            'new_risk_authorized': False}


def observe(market, directory, extra=()):
    now_ms = int(time.time() * 1000)
    midnight = now_ms // DAY * DAY
    rule = rules_sha256()
    if json.loads(SPEC.read_text())['forward']['rules_sha256'] != rule:
        raise ValueError('forward rules differ from the frozen specification')
    source = source_identity()
    if source['dirty']:
        raise ValueError('commit source before recording forward observations')
    with State(directory, 'observation:BTCUSDT:spot:P4') as state:
        if state.get('rules_sha256') not in (None, rule):
            raise ValueError('observation directory belongs to different frozen rules')
        state.set('rules_sha256', rule)
        report = {'observed_at_ms': now_ms, 'recorded_date_utc': iso(now_ms)[:10],
                  'rules_sha256': rule, 'source': source, 'mode': 'market_archive_only',
                  'account_observed': False, 'write_attempted': False, 'native_qualification': 'NOT_QUALIFIED'}
        try:
            bars = load_daily(market, midnight, extra=extra, require_through=midnight)
            for root in (market, *extra):
                for path in (root / '1d').rglob('BTCUSDT-1d-*.zip'):
                    if not Path(str(path) + '.CHECKSUM').exists():
                        raise ValueError(f'missing official checksum: {path.name}')
            account = ledger(bars)
            report.update(status='observed', market_through=iso(bars[-1][0])[:10],
                          market_sha256=file_digest(market, extra=extra), simulated_ledger=account,
                          backfill=True, note='Ledger is recomputed from archives; recorded_date is the actual observation day.')
        except (OSError, ValueError) as exc:
            report.update(status='unknown', reason=str(exc), market_through=None)
        history = directory / 'observations'
        history.mkdir(parents=True, exist_ok=True)
        target = history / f'{now_ms}-{uuid.uuid4().hex[:8]}.json'
        with target.open('x') as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
        prior = [json.loads(path.read_text()) for path in history.glob('*.json')]
        dates = {row['recorded_date_utc'] for row in prior if row['status'] == 'observed'}
        first = min(row['recorded_date_utc'] for row in prior)
        span = (datetime.fromisoformat(report['recorded_date_utc']) - datetime.fromisoformat(first)).days + 1
        report['operations'] = {'calendar_days_since_first_attempt': span, 'days_with_valid_observation': len(dates),
                                'missing_or_failed_days': span - len(dates),
                                'thirty_day_observation_complete': span >= 30 and len(dates) == span,
                                'profitability_proven': False, 'native_execution_proven': False}
        state.report(report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    observed = sub.add_parser('observe')
    observed.add_argument('--market', type=Path, default=Path('/tmp/spotquant-market/klines'))
    observed.add_argument('--extra', type=Path, action='append', default=[])
    observed.add_argument('--refresh', action='store_true', help='verify frozen inputs and download completed forward days')
    observed.add_argument('--state', type=Path, default=Path.home() / '.local/state/spotquant/observations')
    summary = sub.add_parser('combine')
    summary.add_argument('snapshots', type=Path, nargs=2)
    args = parser.parse_args(argv)
    try:
        if args.command == 'observe':
            if args.refresh:
                from research.restore_btc import restore
                forward = args.market.parent / 'forward'
                restore(args.market, forward)
                if forward not in args.extra:
                    args.extra.append(forward)
            report = observe(args.market, args.state, args.extra)
        else:
            blobs = [path.read_bytes() for path in args.snapshots]
            report = combined([json.loads(blob) for blob in blobs], int(time.time() * 1000))
            report['input_sha256'] = [hashlib.sha256(blob).hexdigest() for blob in blobs]
    except (OSError, ValueError, KeyError, TypeError, Blocked) as exc:
        report = {'status': 'unknown', 'reason': str(exc), 'write_attempted': False,
                  'new_risk_authorized': False}
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if report['status'] == 'unknown' else 0


if __name__ == '__main__':
    raise SystemExit(main())
