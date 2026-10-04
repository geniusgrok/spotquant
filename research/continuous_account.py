"""Entrant-only Spot flow sizing, actual cold wallets and fixed lower-size control."""
import argparse
from bisect import bisect_right
from decimal import Decimal as D
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

from research import complete_spot as meter, flow_risk as flow
from research.edge_features import FeatureBook
from research.edge_spot import FEATURE_SHA256
from research.market import load_daily
from research.rebuild import source_identity, timestamp
from spotquant import crowding

SPEC = Path(__file__).with_name('continuous-account-spec.json')


def adjusted_factor(base, name, spot, coin, now, uniform):
    feature = flow.flow_at(spot, coin, now)
    scale = D('.75') if name == 'candidate' and feature['veto'] else uniform if name == 'uniform' else D(1)
    return base*scale, feature


def measure(name, bars, starts, fx, features, spot, coin, uniform):
    journal, original = [], crowding.evaluate
    def evaluate(source, view, now):
        base, diagnostic = original(source, view, now)
        factor, feature = adjusted_factor(base, name, spot, coin, now, uniform)
        diagnostic.update(continuous_rule=name, original_factor=str(base), flow=flow.serial(feature))
        journal.append(dict(time_ms=now, before=str(base), after=str(factor), veto=feature['veto']))
        return factor, diagnostic
    with patch.object(crowding, 'evaluate', evaluate):
        row = meter.measure('crowding-interaction', 'base', bars, starts, fx, features, canonical=True)
    stamps = [e['time_ms'] for e in journal]
    actual = 0
    for fill in row['fills']:
        if fill['buyer'] and stamps:
            index = bisect_right(stamps, fill['time'])-1
            if index >= 0 and D(journal[index]['before']) > D(journal[index]['after']):
                actual += 1
    row.update(candidate='flow-soft75' if name == 'candidate' else 'uniform-trained-budget',
               window_account_finished=row.pop('complete'), complete=False, cagr=None,
               meaning='Independent cold historical screen account, not original economic qualification',
               treatment_filled_buy_cohorts=actual, flow_factor_journal=journal,
               research_identity=dict(execution='shared_finite_session_with_explicit_research_crowding_factor_hook',
                                      spec_sha256=flow.sha(SPEC.read_bytes()), original=row['research_identity']))
    return row


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('spot-root', 'coin-root', 'fx', 'features', 'schedule', 'baseline-summary', 'out'):
        p.add_argument('--'+name, type=Path, required=True)
    args = p.parse_args(argv)
    spec, source = json.loads(SPEC.read_text()), source_identity()
    if source['dirty'] or args.out.exists():
        raise ValueError('freeze source; preserve old outputs')
    previous = json.loads(args.baseline_summary.read_text())
    old = previous['source']['git_head']
    changed = subprocess.check_output(['git', 'diff', '--name-only', old, source['git_head'], '--',
        'spotquant', 'research/complete_spot.py', 'research/session_account.py', 'research/restore_check.py',
        'research/market.py', 'research/edge_features.py', 'research/adoption_spot.py'], text=True)
    if changed:
        raise ValueError('accepted baseline economic program changed')
    primary = json.loads(args.schedule.read_text())['primary']
    schedule_sha = flow.sha(json.dumps(primary['starts_ms'], separators=(',', ':')).encode())
    if schedule_sha != 'f8fb73bebf142ddcc3ed4a3e6b12b4dd7abed1e27bcd8a4ff1c93aec4fe0b32a':
        raise ValueError('original schedule changed')
    fx = meter.PriorFX(args.fx)
    if previous['fx_sha256'] != flow.sha(args.fx.read_bytes()) or previous['feature_sha256'] != FEATURE_SHA256:
        raise ValueError('baseline monetary/feature dependencies changed')
    bars = load_daily(args.spot_root/'klines', flow.END, require_through=flow.END)
    inputs = []
    spot = flow.market(args.spot_root, flow.DAY, inputs)
    coin = flow.aggregate(flow.market(args.coin_root, flow.FOUR, inputs), flow.FOUR)
    features = FeatureBook(args.features, FEATURE_SHA256)
    uniform = D(spec['uniform_control']['scale'])
    args.out.mkdir(parents=True)
    old_window = meter.START_MS, meter.END_MS
    rows, began, failures = [], time.monotonic(), []
    try:
        for index, dates in enumerate(spec['windows']):
            if time.monotonic()-began > 1800:
                failures.append('RESOURCE_BUDGET_EXHAUSTED'); break
            meter.START_MS, meter.END_MS = map(timestamp, dates)
            starts = [t for t in primary['starts_ms'] if meter.START_MS <= t < meter.END_MS]
            pair = {}
            for name in ('baseline', 'candidate', 'uniform'):
                saved = next((r['baseline'] for r in previous['rows'] if r['window'] == dates), None) if name == 'baseline' else None
                if saved:
                    path = args.baseline_summary.parent/saved['raw_file']
                    if flow.sha(path.read_bytes()) != saved['raw_sha256']:
                        raise ValueError('baseline account changed')
                    with gzip.open(path, 'rt') as handle:
                        row = json.load(handle)
                    producer = previous['source']
                    reused = True
                else:
                    row = (meter.measure('crowding-interaction', 'base', bars, starts, fx, features, canonical=True)
                           if name == 'baseline' else measure(name, bars, starts, fx, features, spot, coin, uniform))
                    if name == 'baseline':
                        row.update(window_account_finished=row.pop('complete'), complete=False, cagr=None)
                    producer, reused = source, False
                    path = args.out/f'window-{index}-{name}.json.gz'
                    with gzip.open(path, 'wt') as handle:
                        json.dump(row, handle, separators=(',', ':'))
                if [s['start_ms'] for s in row['sessions']] != starts:
                    raise ValueError('account session subset changed')
                finished = (row['window_account_finished'] and row['audit']['passed']
                            and not row['execution_unresolved_sessions'] and not row['pending_intents'])
                pair[name] = dict(final_cny=row['final_cny'], mdd=row['mdd'], finished=finished,
                                  audit=row['audit'], sessions=len(starts), fills=len(row['fills']),
                                  treatment_events=row.get('treatment_filled_buy_cohorts', 0),
                                  producer=producer, reused=reused, raw_path=str(path), raw_sha256=flow.sha(path.read_bytes()))
                print(json.dumps(dict(window=dates, account=name, **pair[name])), flush=True)
            base, candidate, control = pair['baseline'], pair['candidate'], pair['uniform']
            wealth, draw = float(candidate['final_cny'])/float(base['final_cny']), float(candidate['mdd'])
            passes = ((wealth >= 1 and draw <= float(base['mdd'])+.01)
                      or (draw <= float(base['mdd'])*.8 and wealth >= .95))
            rows.append(dict(window=dates, **pair, baseline_tradeoff_pass=passes,
                             logwealth_over_uniform=math.log(float(candidate['final_cny'])/float(control['final_cny'])),
                             uniform_risk_pass=draw <= float(control['mdd'])+.01))
            if not all(v['finished'] for v in pair.values()):
                failures.append('ACCOUNT_OR_EXECUTION_GATE_FAILED'); break
    finally:
        meter.START_MS, meter.END_MS = old_window
    checks = dict(windows=len(rows)==len(spec['windows']), operating=not failures,
                  treatments=sum(r['candidate']['treatment_events'] for r in rows)>0,
                  baseline_tradeoff=all(r['baseline_tradeoff_pass'] for r in rows),
                  uniform_wealth=sum(r['logwealth_over_uniform'] for r in rows)>0,
                  uniform_risk=all(r['uniform_risk_pass'] for r in rows))
    result = dict(source=source, spec_sha256=flow.sha(SPEC.read_bytes()), rows=rows, gates=checks,
                  decision='FINAL_COMPARISON_ENTRANT' if all(checks.values()) else 'SCREEN_REJECTED',
                  failures=failures, wall_seconds=time.monotonic()-began, runtime_promoted=False,
                  new_accounts=sum(not v['reused'] for r in rows for k,v in r.items() if k in ('baseline','candidate','uniform')),
                  new_sessions=sum(v['sessions'] for r in rows for k,v in r.items() if k in ('baseline','candidate','uniform') and not v['reused']),
                  original_795_replays=0)
    (args.out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'}), flush=True)


if __name__ == '__main__':
    main()
