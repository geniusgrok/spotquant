"""Canonical shared ATR replay. Historical equivalence is a separate exact-source proof."""
import argparse
import hashlib
import json
from pathlib import Path

from research import complete_spot
from research.alpha_spot import CUTOFF, SPEC, calibration
from research.market import file_digest, load_daily
from research.rebuild import END_MS, source_identity
from spotquant.session import RULE


def digest(data):
    return hashlib.sha256(data).hexdigest()


def risk_identity(path=None):
    """Validate the registered profile and bind its exact file and profile bytes."""
    profile = calibration(path, 'atr-stop')
    return dict(candidate='atr-stop', components=['atr-stop'], spec_sha256=digest(SPEC.read_bytes()),
                risk_scale=profile['scale'], core_mode=None, core_fraction='0',
                rule=RULE, cutoff_ms=CUTOFF, scale=profile['scale'],
                calibration_sha256=profile['sha256'], profile=profile,
                profile_sha256=digest(json.dumps(profile, sort_keys=True).encode()))


def measure(scenario, bars, starts, fx, *, limit=None, calibration_path=None):
    if scenario not in complete_spot.SCENARIOS:
        raise ValueError('unregistered scenario')
    return complete_spot.measure('atr-stop', scenario, bars, starts, fx, {}, limit=limit,
                               canonical=True, calibration_path=calibration_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--scenario', choices=complete_spot.SCENARIOS, required=True)
    parser.add_argument('--risk-calibration', type=Path)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args(argv)
    source = source_identity()
    if source['dirty'] or args.out.exists() or (args.limit is not None and (
            args.limit < 1 or not args.out.resolve().is_relative_to('/tmp'))):
        parser.error('commit source; choose new output; positive partial accounts only in /tmp')
    identity = risk_identity(args.risk_calibration)
    market_root = Path('/tmp/spotquant-market/klines')
    bars = load_daily(market_root, END_MS, require_through=END_MS)
    schedule_path = Path('../coinquant/research/session_schedule.json').resolve()
    starts = json.loads(schedule_path.read_text())['primary']['starts_ms']
    fx_path = Path('../starquant/data/usdcny_frankfurter.json').resolve()
    row = measure(args.scenario, bars, starts, complete_spot.PriorFX(fx_path),
                  limit=args.limit, calibration_path=args.risk_calibration)
    report = dict(format=1, source=source, adoption=dict(execution='canonical_shared_session', **identity),
                  spec_sha256=digest(SPEC.read_bytes()),
                  protocol_sha256=digest(SPEC.with_name('alpha-beta-PROTOCOL.md').read_bytes()),
                  market_sha256=file_digest(market_root), schedule_sha256=digest(schedule_path.read_bytes()),
                  fx_sha256=digest(fx_path.read_bytes()), risk_calibration_sha256=identity['calibration_sha256'],
                  results={'atr-stop-' + args.scenario: row}, native_execution_verified=False,
                  limitations=['conditional proposal; adoption requires exact five-account bridge',
                               'daily OHLC proxy, native unverified, actual account-days zero'])
    # The calibration must not change while this account is running.
    if risk_identity(args.risk_calibration) != identity:
        raise ValueError('diagnostic calibration changed during replay')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        json.dump(report, stream, separators=(',', ':'), allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
