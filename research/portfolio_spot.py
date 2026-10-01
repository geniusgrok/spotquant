"""Three actual fixed-budget accounts; no scaling of an existing equity curve."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

from research.complete_spot import CANDIDATES, PriorFX, measure
from research.market import load_daily, file_digest
from research.rebuild import END_MS, source_identity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--crowding', type=Path, default=Path('/tmp/btc-complete-inputs/crowding-complete.json'))
    parser.add_argument('--candidate', choices=CANDIDATES, default='default')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1)
    args = parser.parse_args(argv)
    source = source_identity()
    if source['dirty'] or args.out.exists():
        parser.error('commit source and choose a new output')
    root = Path('/tmp/spotquant-market/klines')
    bars = load_daily(root, END_MS, require_through=END_MS)
    schedule = Path('../coinquant/research/session_schedule.json')
    starts = json.loads(schedule.read_text())['primary']['starts_ms']
    fx = Path('../starquant/data/usdcny_frankfurter.json')
    features = json.loads(args.crowding.read_text())
    results = {}
    if args.workers == 1:
        for budget in (2500, 5000, 7500):
            results[str(budget)] = measure(args.candidate, 'base', bars, starts, PriorFX(fx), features, initial_cny=D(budget))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(measure, args.candidate, 'base', bars, starts, PriorFX(fx), features,
                                   initial_cny=D(budget)): budget for budget in (2500, 5000, 7500)}
            for future in as_completed(pending):
                results[str(pending[future])] = future.result()
    results = {str(budget): results[str(budget)] for budget in (2500, 5000, 7500)}
    report = {'inputs': {'source': source, 'market_sha256': file_digest(root),
                         'schedule_sha256': hashlib.sha256(schedule.read_bytes()).hexdigest(),
                         'fx_sha256': hashlib.sha256(fx.read_bytes()).hexdigest(),
                         'crowding_sha256': hashlib.sha256(args.crowding.read_bytes()).hexdigest()},
              'candidate': args.candidate, 'results': results, 'native_execution_verified': False,
              'method': 'Independent actual 795-session accounts at each initial budget, no curve scaling or transfers.'}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        json.dump(report, stream, separators=(',', ':'), allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
