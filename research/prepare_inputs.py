"""Rebuild registered lagged features from verified public archives, no credentials."""
import argparse
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from research.market import load_daily, file_digest
from research.rebuild import END_MS
from spotquant.model import DAY


def build(futures_input, perp_repo, perp_market, spot_market):
    # Separate interpreter avoids the two repositories' same `research` namespace.
    program = """import json,sys
from research.session_market import load_base
m=load_base(sys.argv[1])
print(json.dumps({'funding': [[t,str(v)] for t,v in m.funding],
'daily_futures_close': {str(t//86400000*86400000):str(v[3]) for t,v in sorted(m.h4.items()) if t%86400000==72000000},
'market_identity':m.identity},sort_keys=True))
"""
    actual = json.loads(subprocess.run([sys.executable, '-c', program, str(perp_market)],
                                      cwd=perp_repo, capture_output=True, text=True, check=True).stdout)
    original = json.loads(Path(futures_input).read_text())
    if actual['market_identity']['funding_uncovered']:
        raise ValueError('official funding coverage is incomplete')
    if original['daily_futures_close'] != actual['daily_futures_close']:
        raise ValueError('registered futures closes differ from verified archives')
    if any(row not in actual['funding'] for row in original['funding']):
        raise ValueError('registered settled funding differs from verified archives')
    old_vision = {r['path']: r for r in original['market_identity']['vision']}
    actual_vision = {r['path']: r for r in actual['market_identity']['vision']}
    if any(actual_vision.get(name) != value for name, value in old_vision.items()):
        raise ValueError('registered futures archive changed')
    bars = load_daily(spot_market, END_MS, require_through=END_MS)
    closes = original['daily_futures_close']
    basis = [[t + DAY + 60000, str(D(closes[str(t)]) / c - 1)]
             for t, o, h, l, c, q in bars if str(t) in closes]
    september = Path(perp_market) / 'funding/BTCUSDT-fundingRate-2026-09.zip'
    return {'basis': basis, 'funding': actual['funding'], 'source': {
        'basis_available_lag_ms': 60000,
        'basis_model': 'previous UTC daily futures trade close / spot close; not instantaneous executable basis',
        'funding_available_lag_ms': 28800000,
        'funding_model': 'settled observed historical rates, not predicted future rates',
        'futures_artifact_sha256': hashlib.sha256(Path(futures_input).read_bytes()).hexdigest(),
        'futures_market_identity': actual['market_identity'],
        'september_funding_archive_sha256': hashlib.sha256(september.read_bytes()).hexdigest(),
        'spot_daily_sha256': file_digest(spot_market)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--futures-input', type=Path, required=True)
    parser.add_argument('--perp-repo', type=Path, default=Path('../coinquant'))
    parser.add_argument('--perp-market', type=Path, default=Path('/tmp/coinquant-market'))
    parser.add_argument('--spot-market', type=Path, default=Path('/tmp/spotquant-market/klines'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error('choose a new immutable output')
    report = build(args.futures_input, args.perp_repo, args.perp_market, args.spot_market)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        stream.write(json.dumps(report, sort_keys=True) + '\n')
    print(json.dumps({'sha256': hashlib.sha256(args.out.read_bytes()).hexdigest(),
                      'funding': len(report['funding']), 'basis': len(report['basis'])}))


if __name__ == '__main__':
    main()
