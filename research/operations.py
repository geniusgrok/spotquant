"""Manual archive observation and two-account read-only risk summary. No network writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from research.forward import SPEC, ledger, rules_sha256
from research.market import file_digest, load_daily
from research.rebuild import iso, source_identity
from spotquant.model import DAY
from spotquant.state import State
from spotquant.types import Blocked, number


def combined(snapshots, now_ms, valuation_price=None):
    if len(snapshots) != 2 or {row.get('market') for row in snapshots} != {'spot', 'perpetual'}:
        raise ValueError('provide exactly one spot and one perpetual account snapshot')
    if valuation_price is None and len(snapshots) == 2 and all(
            key in row for row in snapshots for key in
            (('cash_usdt',) if row.get('market') == 'spot' else ('wallet_usdt', 'entry_price_usdt'))):
        valuation_price = next(row['btc_price_usdt'] for row in snapshots if row.get('market') == 'perpetual')
    if valuation_price is not None:
        price = number(valuation_price, positive=True)
        snapshots = [dict(row) for row in snapshots]
        for row in snapshots:
            qty = number(row['btc_position'])
            if row['market'] == 'spot':
                equity = number(row['cash_usdt'], nonnegative=True) + qty * price
            else:
                equity = number(row['wallet_usdt']) + qty * (price - number(row['entry_price_usdt'], nonnegative=True))
            row.update(btc_price_usdt=str(price), equity_usdt=str(equity))
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
            'accounts': [dict(dict(zip(('environment', 'uid', 'market'), identity)),
                         equity_usdt=str(account_equity), btc_position=str(qty),
                         **{key: row[key] for key in ('available_usdt', 'cash_usdt',
                             'native_stop_quantity_btc', 'btc_without_native_stop',
                             'native_full_position_protected', 'possible_entry_remainders',
                             'liquidation_price_usdt', 'liquidation_buffer_fraction') if key in row})
                         for identity, account_equity, qty, row in zip(identities, equities, quantities, snapshots)],
            'snapshot_times_ms': stamps, 'btc_price_usdt': str(price),
            'inputs_verified_remotely': False, 'write_attempted': False,
            'limitations': 'Supplied snapshots; equity includes PnL, not added collateral. Linear stress omits liquidation, funding and exit costs.',
            'new_risk_authorized': False}


def account_days(records, report):
    """Count actual collection dates for this account pair in these saved reports."""
    today = datetime.fromisoformat(report['recorded_date_utc']).date()
    records = [row for row in records if row.get('collection') == 'native_read_only_exporters'
               and row.get('configured_accounts') == report['configured_accounts']
               and row.get('recorded_date_utc', '9999') <= today.isoformat()]
    attempted = {datetime.fromisoformat(row['recorded_date_utc']).date() for row in records}
    valid = {datetime.fromisoformat(row['recorded_date_utc']).date() for row in records
             if row.get('status') == 'read_only' and row.get('account_observed') is True}
    span = (today - min(attempted)).days + 1
    window = {today - timedelta(days=i) for i in range(min(span, 30))}
    return {'calendar_days_since_first_attempt': span, 'days_with_valid_account_observation': len(valid),
            'missing_or_failed_days_last_30': len(window - valid),
            'thirty_day_observation_complete': span >= 30 and window <= valid,
            'native_execution_proven': False, 'profitability_proven': False}


def collect(spot_config, perp_config, perp_repo, output):
    """Collect both native read-only exports concurrently, with no order flags."""
    if output.exists():
        raise ValueError('refusing to overwrite an account observation')
    output.parent.mkdir(parents=True, exist_ok=True)
    scope = []
    for market, path in (('spot', spot_config), ('perpetual', perp_config)):
        config = json.loads(path.read_bytes())
        scope.append({'market': market, 'account_uid': config.get('account_uid'),
                      'environment': config.get('environment', 'live')})
    report = {'status': 'unknown', 'write_attempted': False, 'new_risk_authorized': False,
              'collection': 'native_read_only_exporters', 'account_observed': False,
              'recorded_date_utc': iso(int(time.time() * 1000))[:10], 'configured_accounts': scope,
              'native_execution_verified': False}
    with tempfile.TemporaryDirectory(prefix='btc-read-only-') as temporary:
        files = [Path(temporary) / 'spot.json', Path(temporary) / 'perpetual.json']
        commands = [([sys.executable, '-m', 'spotquant', 'snapshot', '--config', str(spot_config.resolve()),
                      '--out', str(files[0])], Path(__file__).resolve().parents[1]),
                    ([sys.executable, '-m', 'coinquant', 'snapshot', '--config', str(perp_config.resolve()),
                      '--out', str(files[1])], perp_repo)]
        jobs = []
        try:
            for command, root in commands:
                jobs.append(subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            for job in jobs:
                stdout, _stderr = job.communicate(timeout=45)
                if job.returncode:
                    try:
                        payload = json.loads(stdout)
                    except (ValueError, UnicodeError):
                        payload = {'reason': 'exporter returned no valid JSON'}
                    raise ValueError('read-only account collection failed: ' + payload.get('reason', 'unknown'))
            blobs = [path.read_bytes() for path in files]
            report.update(combined([json.loads(blob) for blob in blobs], int(time.time() * 1000)))
            report.update(account_observed=True,
                          input_sha256=[hashlib.sha256(blob).hexdigest() for blob in blobs],
                          snapshots=[json.loads(blob) for blob in blobs])
        except (OSError, ValueError, KeyError, TypeError, Blocked, subprocess.TimeoutExpired) as exc:
            report.update(status='unknown', reason='account collection timed out' if isinstance(exc, subprocess.TimeoutExpired)
                          else str(exc), account_observed=False)
        finally:
            for job in jobs:
                if job.poll() is None:
                    job.kill()
                job.communicate()
    prior = []
    for path in output.parent.glob('*.json'):
        try:
            prior.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    report['operations'] = account_days([*prior, report], report)
    with output.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    return report


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
    summary.add_argument('--valuation-price', help='Revalue both fresh exports at one explicit BTC price')
    collected = sub.add_parser('collect', help='Concurrently collect fresh native read-only exports')
    collected.add_argument('--spot-config', type=Path, required=True)
    collected.add_argument('--perp-config', type=Path, required=True)
    collected.add_argument('--perp-repo', type=Path, default=Path('/workspace/coinquant'))
    collected.add_argument('--out', type=Path, required=True)
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
        elif args.command == 'collect':
            report = collect(args.spot_config, args.perp_config, args.perp_repo, args.out)
        else:
            blobs = [path.read_bytes() for path in args.snapshots]
            report = combined([json.loads(blob) for blob in blobs], int(time.time() * 1000), args.valuation_price)
            report['input_sha256'] = [hashlib.sha256(blob).hexdigest() for blob in blobs]
    except (OSError, ValueError, KeyError, TypeError, Blocked) as exc:
        report = {'status': 'unknown', 'reason': str(exc), 'write_attempted': False,
                  'new_risk_authorized': False}
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if report['status'] == 'unknown' else 0


if __name__ == '__main__':
    raise SystemExit(main())
