"""One preregistered real-session screen; quarter accounts are never spliced."""
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import time
import subprocess

from research import complete_spot as complete, structure_spot as candidate
from research.edge_features import FeatureBook
from research.edge_spot import FEATURE_SHA256
from research.market import load_daily
from research.rebuild import source_identity, timestamp


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('market', 'fx', 'features', 'schedule', 'out'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--resume-receipt', type=Path)
    args = p.parse_args(argv)
    source = source_identity()
    if source['dirty']:
        raise ValueError('freeze sources before financial screening')
    spec = json.loads(candidate.SPEC.read_text())
    primary = json.loads(args.schedule.read_text())['primary']
    schedule_sha = hashlib.sha256(json.dumps(primary['starts_ms'], separators=(',', ':')).encode()).hexdigest()
    if schedule_sha != 'f8fb73bebf142ddcc3ed4a3e6b12b4dd7abed1e27bcd8a4ff1c93aec4fe0b32a':
        raise ValueError('original schedule changed')
    fx = complete.PriorFX(args.fx)
    bars = load_daily(args.market, complete.END_MS, require_through=complete.END_MS)
    features = FeatureBook(args.features, FEATURE_SHA256)
    started = time.monotonic()
    rows, rejected = [], []
    resume = json.loads(args.resume_receipt.read_text()) if args.resume_receipt else None
    if resume:
        # Only output serialization changed. Preserve each old producer identity.
        subprocess.run(['git', 'merge-base', '--is-ancestor', resume['producer_head'], source['git_head']], check=True)
        changed = subprocess.check_output(['git', 'diff', '--name-only', resume['producer_head'], source['git_head'],
            '--', 'spotquant', 'research', ':(exclude)research/structure_screen.py'], text=True)
        if changed or resume['spec_sha256'] != hashlib.sha256(candidate.SPEC.read_bytes()).hexdigest():
            raise ValueError('resume economic dependencies changed')
        if (resume['schedule_sha256'] != schedule_sha or resume['feature_sha256'] != features.sha256
                or resume['fx_sha256'] != hashlib.sha256(args.fx.read_bytes()).hexdigest()):
            raise ValueError('resume inputs changed')
    args.out.mkdir(parents=True, exist_ok=True)
    original = complete.START_MS, complete.END_MS
    try:
        for index, window in enumerate(spec['windows']):
            if time.monotonic()-started >= spec['screen']['max_wall_seconds']:
                rejected.append('RESOURCE_BUDGET_EXHAUSTED'); break
            complete.START_MS, complete.END_MS = map(timestamp, window)
            starts = [s for s in primary['starts_ms'] if complete.START_MS <= s < complete.END_MS]
            if not starts:
                raise ValueError('empty registered session window')
            pair = {}
            for name in ('baseline', 'candidate'):
                reused = resume and str(index)+'/'+name in resume['accounts']
                if reused:
                    saved = resume['accounts'][str(index)+'/'+name]
                    path = args.resume_receipt.parent/saved['file']
                    if hashlib.sha256(path.read_bytes()).hexdigest() != saved['sha256']:
                        raise ValueError('resume account bytes changed')
                    with gzip.open(path, 'rt') as handle:
                        row = json.load(handle)
                    if [r['start_ms'] for r in row['sessions']] != starts:
                        raise ValueError('resume session subset changed')
                else:
                    row = (complete.measure('crowding-interaction', 'base', bars, starts, fx, features, canonical=True)
                           if name == 'baseline' else candidate.measure(bars, starts, fx, features))
                # The legacy meter's completion flag here is only for this cold window.
                row.update(window_account_finished=row['window_account_finished'] if reused else row.pop('complete'), complete=False, cagr=None,
                           meaning='finite real-session screening account, not original full-window qualification')
                row = candidate.alpha.serial(row)
                path = args.out/f'window-{index}-{name}.json.gz'
                if not reused:
                    with gzip.open(path, 'wt') as handle:
                        json.dump(row, handle, separators=(',', ':'))
                else:
                    path = args.resume_receipt.parent/saved['file']
                core_events = 0
                if name == 'candidate':
                    owners = candidate.alpha.execution.allocation_owners(
                        (json.loads(r[1]), json.loads(r[3])) for r in row['allocations'])
                    core_events = sum(owners.get(str(f['order_id']), {}).get('sleeves') == [200] for f in row['fills'])
                pair[name] = {'final_cny': row['final_cny'], 'mdd': row['mdd'],
                    'finished': row['window_account_finished'], 'audit': row['audit'],
                    'sessions': len(row['sessions']), 'fills': len(row['fills']), 'component_fill_events': core_events,
                    'execution_unresolved': row['execution_unresolved_sessions'],
                    'raw_file': path.name, 'raw_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                print(json.dumps({'window': window, 'account': name, **pair[name]}), flush=True)
            row = {'window': window, **pair,
                'paired_log_wealth_gain': math.log(float(pair['candidate']['final_cny'])/float(pair['baseline']['final_cny'])),
                'mdd_increase': float(pair['candidate']['mdd'])-float(pair['baseline']['mdd'])}
            rows.append(row)
            if not all(v['finished'] and v['audit']['passed'] and not v['execution_unresolved'] for v in pair.values()):
                rejected.append('ACCOUNT_OR_EXECUTION_GATE_FAILED')
            if row['mdd_increase'] > spec['screen']['maximum_each_window_mdd_increase']:
                rejected.append('WINDOW_RISK_INCREASE_EXCEEDED')
            if rejected:
                break
    finally:
        complete.START_MS, complete.END_MS = original
    gain = sum(r['paired_log_wealth_gain'] for r in rows)
    events = sum(r['candidate']['component_fill_events'] for r in rows)
    if gain < spec['screen']['minimum_sum_paired_log_wealth_gain']:
        rejected.append('INSUFFICIENT_PAIRED_WEALTH_GAIN')
    if events < spec['screen']['minimum_component_fill_events']:
        rejected.append('NO_COMPONENT_FILL_EVENTS')
    result = {'source': source, 'spec_sha256': hashlib.sha256(candidate.SPEC.read_bytes()).hexdigest(),
        'schedule_sha256': schedule_sha, 'fx_sha256': hashlib.sha256(args.fx.read_bytes()).hexdigest(), 'feature_sha256': features.sha256,
        'rows': rows, 'sum_paired_log_wealth_gain': gain, 'component_fill_events': events,
        'decision': 'SCREEN_REJECTED' if rejected else 'FULL_MEASUREMENT_ENTRANT',
        'resume_receipt': resume,
        'reasons': rejected, 'wall_seconds': time.monotonic()-started,
        'historical_screen_only': True, 'production_promoted': False, 'native_qualified': False}
    (args.out/'summary.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'rows'}), flush=True)


if __name__ == '__main__':
    main()
