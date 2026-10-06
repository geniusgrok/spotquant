"""One fresh full wallet on the frozen current Spotquant main runtime.

Private tooling only. No network, archived strategies, runtime hooks or backups.
"""
from __future__ import annotations

import argparse
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO = Path('/workspace/spotquant')
HEAD = 'b81db17c31a4b1fed8d8fc3d63c9954053a66613'
START = 1577836800000
END = 1789862400000
sys.path.insert(0, str(REPO))

from historical_venue import HistoricalVenue, audit
from feature_book import FeatureBook
from prior_fx import PriorFX
from spotquant import execution, follow, model, preview, session
from spotquant.config import Config
from spotquant.state import State
from spotquant.types import serial


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_input(path, expected):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('registered small input changed: ' + str(path))
    return json.loads(raw)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registration', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--state-dir', type=Path, required=True)
    args = p.parse_args()
    # The root creates this explicit launch registration after costs/inputs freeze.
    reg = json.loads(args.registration.read_text())
    if reg['runtime_head'] != HEAD or reg['window_ms'] != [START, END]:
        raise ValueError('registered source/window mismatch')
    if reg['initial_cny'] != '10000' or reg['schedule_count'] != 795:
        raise ValueError('registered fresh capital/session count mismatch')
    if not 1 <= reg['budget_seconds'] <= 1800:
        raise ValueError('unregistered wall budget')
    if reg['state_dir'] != str(args.state_dir.resolve()):
        raise ValueError('registered fixed synthetic state directory differs')
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip() != HEAD:
        raise ValueError('current main source no longer matches frozen head')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=REPO, text=True).strip():
        raise ValueError('current main must be clean at launch')
    if not (session.decision is preview.decision and session.State is State
            and session.Model is model.Model and follow.Model is model.Model
            and execution._protection is preview._protection):
        raise ValueError('refuse installed strategy/execution hooks')
    for path, expected in reg['tooling_sha256'].items():
        if digest(Path(__file__).parent / path) != expected:
            raise ValueError('private producer changed: ' + path)
    if args.state_dir.exists() or args.out.exists():
        raise ValueError('fresh run cannot reuse or overwrite a previous account/output')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    data = read_input(reg['daily_path'], reg['daily_sha256'])
    table = data['bars']
    bars = [(int(t), *(D(row[k]) for k in ('open', 'high', 'low', 'close', 'quote_volume')))
            for t, row in sorted(table.items(), key=lambda item: int(item[0]))
            if model.ORIGIN <= int(t) < END]
    if (len(bars) != 2819 or bars[0][0] != model.ORIGIN or bars[-1][0] != END - model.DAY
            or any(b[0] - a[0] != model.DAY for a, b in zip(bars, bars[1:]))):
        raise ValueError('daily bar coverage is not the fixed contiguous 2019 origin')
    schedule = read_input(reg['schedule_path'], reg['schedule_sha256'])
    starts = [s for s in schedule['primary']['starts_ms'] if START <= s < END]
    if len(starts) != 795 or starts != sorted(set(starts)):
        raise ValueError('fixed full schedule changed')
    read_input(reg['fx_path'], reg['fx_sha256'])
    fx = PriorFX(reg['fx_path'])
    features = FeatureBook(reg['features_path'], reg['features_sha256'])
    wallet = D('10000') / fx(START) * D('.999')
    venue = HistoricalVenue(bars, START, wallet, fx, uid='1')
    venue.crowding_features = lambda: features
    config = Config('1', str(args.state_dir), 300, 5, 'demo', '5000000')
    reports, failure = [], None
    with Path(str(args.out) + '.sessions.jsonl').open('x') as journal:
        for index, start in enumerate(starts):
            if time.monotonic() - started >= reg['budget_seconds']:
                failure = dict(phase='budget', completed_sessions=len(reports),
                               reason='registered wall budget exhausted; no automatic cold rerun')
                break
            if shutil.disk_usage(args.state_dir.parent).free < 100_000_000:
                failure = dict(phase='storage', completed_sessions=len(reports),
                               reason='private workspace free space below 100 MB')
                break
            try:
                venue.advance(start)
                report = session.run(config, venue, execute=True,
                                     monotonic=venue.monotonic, wait=venue.wait)
                errors = report['errors']
                unresolved = bool(report['pending_intents']) or (
                    report['status'] != 'demo_execution'
                    and not (errors and all('session deadline' in e['reason'] for e in errors)))
                row = dict(start_ms=start, ended_ms=venue.now_ms,
                           status=report['status'], cycles=report['cycles'], errors=errors,
                           pending_intents=report['pending_intents'],
                           execution_unresolved=unresolved,
                           elapsed_seconds=report['elapsed_seconds'])
                reports.append(row)
                journal.write(json.dumps(serial(row), separators=(',', ':')) + '\n')
                journal.flush()
                unexpected = [e for e in errors if 'session deadline' not in e['reason']]
                if unresolved or unexpected:
                    failure = dict(phase='session', start_ms=start, report=row)
                    break
                if index % 50 == 0 or index == len(starts) - 1:
                    print(json.dumps(dict(session_count=len(reports), target=len(starts),
                                          start_ms=start, wall_seconds=time.monotonic()-started)), flush=True)
            except Exception as exc:
                failure = dict(phase='session', start_ms=start,
                               error_type=type(exc).__name__, reason=str(exc))
                break
    if failure is None:
        try:
            venue.advance(END)
        except Exception as exc:
            failure = dict(phase='terminal', error_type=type(exc).__name__, reason=str(exc))
    with State(args.state_dir, config.scope) as state:
        pending = state.pending()
        positions = state.get('positions')
        allocations = list(state.db.execute('SELECT id,payload,status,result FROM intents ORDER BY updated'))
        incompatible = [key for key in ('lifecycle_identity', 'alpha_identity', 'edge_identity', 'adoption_risk')
                        if state.get(key) is not None]
    money = audit(venue)  # Once, at completion; no economic acceptance threshold.
    final_usdt = venue.cash + venue.btc * venue.price
    terminal_ms = END if failure is None else venue.now_ms
    final_cny = final_usdt * fx(terminal_ms) * D('.999')
    complete = (failure is None and not pending and not incompatible
                and len(reports) == 795 and money['passed'])
    identity = dict(runtime_head=HEAD, package_path=str(Path(session.__file__).resolve()),
                    registration_sha256=digest(args.registration), tooling_sha256=reg['tooling_sha256'],
                    daily_sha256=reg['daily_sha256'], features_sha256=features.sha256,
                    schedule_sha256=reg['schedule_sha256'], fx_sha256=reg['fx_sha256'],
                    strategy_hooks_installed=False, legacy_strategy_identities=incompatible)
    raw = serial(dict(format='spotquant-current-main-full-wallet-v1', identity=identity,
                      complete=complete, failure=failure, initial_cny='10000', initial_usdt=wallet,
                      final_cny=final_cny, final_usdt=final_usdt, cash_usdt=venue.cash, btc=venue.btc,
                      window_ms=[START, END], actual_terminal_ms=terminal_ms,
                      return_cny=final_cny / 10000 - 1,
                      cagr=(float(final_cny / 10000) ** (1 / ((END - START) / (365.25 * model.DAY))) - 1)
                           if complete else None,
                      mdd=venue.mdd, audit=money, fills=venue.fills, daily=venue.daily,
                      sessions=reports, client_events=venue.client_events, allocations=allocations,
                      positions=positions, pending_intents=pending, session_count=len(reports),
                      registered_session_count=795,
                      session_error_count=sum(bool(r['errors']) for r in reports),
                      execution_unresolved_sessions=sum(r['execution_unresolved'] for r in reports),
                      wall_seconds=time.monotonic() - started, native_execution_verified=False,
                      price_model='daily OHLC linear high-before-low proxy, not observed intraday tape or native fills',
                      costs=dict(fee='.001', market_slip='.0005', stop_slip='.001',
                                 fx_conversion_each_end='.001'),
                      actual_account_days=0, prospective_alpha_proven=False))
    with gzip.open(args.out, 'xt', encoding='utf-8', compresslevel=1) as stream:
        json.dump(raw, stream, separators=(',', ':'), allow_nan=False)
        stream.write('\n')
    brief = {k: raw[k] for k in ('format', 'identity', 'complete', 'failure', 'initial_cny',
                                'final_cny', 'final_usdt', 'return_cny', 'cagr', 'mdd', 'audit',
                                'session_count', 'registered_session_count', 'session_error_count',
                                'execution_unresolved_sessions', 'wall_seconds',
                                'native_execution_verified', 'price_model', 'costs')}
    with Path(str(args.out) + '.summary.json').open('x') as stream:
        json.dump(brief, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(brief), flush=True)
    if not complete:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
