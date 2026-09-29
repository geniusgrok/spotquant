"""Forward ledger for the frozen P4 rules. Not a backtest, not a selection.

From 2026-09-20 00:00 UTC the frozen rules run on BTCUSDT in a USDT ledger. The
account starts as cash, and a regime that is already bullish waits for a fresh
cross. Costs are the meter's costs; USDT is not converted, so the fee, slip, and
stop rules are the only frictions. The SHA-256 of the rule constants is pinned in
``research/spec.json``. Each run recomputes the whole ledger from the official
daily files and refuses to write when the days it already recorded would change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal as D
from pathlib import Path

from research.account import (
    ENTRY_SLIP, EXIT_SLIP, FEE, STOP_SLIP, simulate_sleeves,
)
from research.market import file_digest, load_daily
from research.rebuild import ROOT, iso, source_identity
from spotquant.model import (
    ADVERSE, CAP_BOUNCE, CAP_DEPTH, CAP_DROP, CAP_HAND, CAP_WINDOW, CONFIRM, CRASH, DAY, EXTEND,
    FRESH, HIGH_WINDOW, SLEEVES, TRAIL,
)

START_MS = 1789862400000  # 2026-09-20T00:00:00Z
INITIAL_USDT = D('10000')
OUT = ROOT / 'evidence' / 'forward' / 'forward.json'
SPEC = ROOT / 'research' / 'spec.json'


def rules() -> dict:
    return {
        'sleeves': list(SLEEVES),
        'confirm': CONFIRM,
        'fresh': FRESH,
        'crash': format(CRASH, 'f'),
        'high_window': HIGH_WINDOW,
        'trail': format(TRAIL, 'f'),
        'extend': format(EXTEND, 'f'),
        'cap_drop': format(CAP_DROP, 'f'),
        'cap_bounce': format(CAP_BOUNCE, 'f'),
        'cap_depth': format(CAP_DEPTH, 'f'),
        'cap_hand': format(CAP_HAND, 'f'),
        'cap_window': CAP_WINDOW,
        'adverse_stop': format(ADVERSE, 'f'),
        'fee': format(FEE, 'f'),
        'entry_slip': format(ENTRY_SLIP, 'f'),
        'exit_slip': format(EXIT_SLIP, 'f'),
        'stop_slip': format(STOP_SLIP, 'f'),
        'vol_scaling': None,
    }


def rules_sha256() -> str:
    return hashlib.sha256(json.dumps(rules(), sort_keys=True).encode()).hexdigest()


def ledger(bars) -> dict:
    end_ms = bars[-1][0] + DAY
    result = simulate_sleeves(
        bars, lambda _now: D(1), start_ms=START_MS, end_ms=end_ms, windows=SLEEVES,
        model_kwargs={
            'extend': EXTEND, 'cap_drop': CAP_DROP, 'cap_bounce': CAP_BOUNCE,
            'cap_depth': CAP_DEPTH, 'cap_hand': CAP_HAND, 'adverse_stop': ADVERSE,
        },
        conversion=D(0), cold_start=True, initial_cny=INITIAL_USDT,
    )
    days = [[iso(day)[:10], format(value, 'f')] for day, value in result['daily_cny']]
    return {
        'start': iso(START_MS),
        'through': iso(bars[-1][0])[:10],
        'initial_usdt': format(INITIAL_USDT, 'f'),
        'days': days,
        'final_usdt': format(result['final_usdt'], 'f'),
        'return': float(result['final_usdt'] / INITIAL_USDT - 1),
        'continuous_mdd': format(result['mdd'], 'f'),
        'fees_usdt': format(result['fees'], 'f'),
        'trades': result['trades'],
        'position_by_sleeve': {str(key): format(value, 'f') for key, value in result['positions'].items()},
    }


def build(market: Path, extra: list[Path]) -> dict:
    spec = json.loads(SPEC.read_text(encoding='utf-8'))
    pinned = spec.get('forward', {}).get('rules_sha256')
    if pinned != rules_sha256():
        raise SystemExit('the rule constants differ from the hash pinned in research/spec.json')
    bars = load_daily(market, START_MS + 3650 * DAY, 'BTCUSDT', extra)
    if bars[-1][0] < START_MS:
        raise SystemExit('no daily file on or after the ledger start')
    out = ledger(bars)
    out.update({
        'trial': 'P4-forward',
        'symbol': 'BTCUSDT',
        'market': 'spot',
        'rules': rules(),
        'rules_sha256': rules_sha256(),
        'cold_start': 'all cash; a regime already bullish at the start waits for a fresh cross',
        'qualification': 'NOT_QUALIFIED',
        'note': 'A ledger of a few days is not evidence of an edge. It is a record that starts before the result is known.',
        'source': source_identity(),
        'market_sha256': file_digest(market, 'BTCUSDT', extra),
    })
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description='Forward ledger for the frozen P4 rules')
    parser.add_argument('--market', default='/tmp/spotquant-market/klines')
    parser.add_argument('--extra', action='append', default=['/tmp/spotquant-market/forward'])
    parser.add_argument('--out', default=str(OUT))
    args = parser.parse_args(argv)
    out = build(Path(args.market), [Path(item) for item in args.extra])
    path = Path(args.out)
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        if out['days'][:len(previous['days'])] != previous['days']:
            raise SystemExit('the recorded days would change; the ledger is append-only')
    if out['source']['dirty'] and ROOT / 'evidence' in path.resolve().parents:
        raise SystemExit('evidence under evidence/ must come from a clean committed tree')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: out[key] for key in (
        'through', 'final_usdt', 'return', 'continuous_mdd', 'trades')}, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
