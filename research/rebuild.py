"""Economic meter for the spot account.

The default trial is P3. It uses the ``spotquant.model`` constants: SMA 40,
two confirmed closes, a fresh cross, the 252-day crash filter, a 28% stop,
a 60% blow-off, an 8% then 6% crash reversal under the 400-day high, and a
4% close under the entry fill that does not apply during repair. ``--grid``
sweeps SMA window and trail under those other constants and writes
``hold-grid.json``. It does not replace ``frontier.json`` or the P1 and P2
files. Naming the trial P2 overwrites that earlier file. Partial runs are
not written into the evidence directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from research.account import (
    CONVERSION, ENTRY_SLIP, EXIT_SLIP, FEE, INITIAL_CNY, STOP_SLIP, simulate,
)
from research.fx import BASIS as FX_BASIS, DatedFX
from research.market import file_digest, load_daily
from spotquant.model import (
    ADVERSE, CAP_BOUNCE, CAP_DEPTH, CAP_DROP, CAP_HAND, CAP_WINDOW, CONFIRM, CRASH,
    EXTEND, FRESH, HIGH_WINDOW, SMA_WINDOW, TRAIL,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence' / 'rebuild-20260928'
START = '2020-01-01T00:00:00Z'
END = '2026-09-20T00:00:00Z'
START_MS = 1577836800000
END_MS = 1789862400000
# Registered before the reported measurement. Full-sample selection is disclosed.
SMA_GRID = (20, 30, 40, 50, 80, 100, 120, 150, 200)
TRAIL_GRID = ('0.10', '0.15', '0.20', '0.25', '0.30')


def timestamp(text: str) -> int:
    return int(datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() * 1000)


def iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def source_identity() -> dict:
    def git(*args):
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout

    files = sorted(git('ls-files', 'spotquant', 'research').split())
    digest = hashlib.sha256()
    for name in files:
        if name.endswith('.py'):
            digest.update(name.encode() + b'\0' + (ROOT / name).read_bytes() + b'\0')
    return {
        'git_head': git('rev-parse', 'HEAD').strip(),
        'dirty': bool(git('status', '--porcelain', '--', 'spotquant', 'research').strip()),
        'python_sources_sha256': digest.hexdigest(),
    }


def _public(result: dict, name: str, extra: dict) -> dict:
    trades = result['trades']
    wins = sum(1 for item in trades if D(item['exit']) > D(item['entry']))
    kinds = {}
    for item in trades:
        kinds[item['kind']] = kinds.get(item['kind'], 0) + 1
    out = {
        'trial': name,
        'symbol': 'BTCUSDT',
        'market': 'spot',
        'leverage': '0',
        'shorting': False,
        'start': START,
        'end': END,
        'end_exclusive': True,
        'initial_cny': format(INITIAL_CNY, 'f'),
        'additional_capital': '0',
        'final_usdt': format(result['final_usdt'], 'f'),
        'final_cny': format(result['final_cny'], 'f'),
        'cost_net_cagr': result['cagr'],
        'continuous_mdd': format(result['mdd'], 'f'),
        'mdd_at': iso(result['mdd_at']),
        'fees_usdt': format(result['fees'], 'f'),
        'trades': len(trades),
        'wins': wins,
        'exit_kinds': kinds,
        'position_btc': format(result['position_btc'], 'f'),
        'skipped_entries': result['skipped_entries'],
        'sma_window': result['sma_window'],
        'trail': result['trail'],
        'confirm': result['confirm'],
        'crash': result['crash'],
        'fresh': result['fresh'],
        'high_window': HIGH_WINDOW,
        'extend': result['extend'],
        'cap_drop': result['cap_drop'],
        'cap_bounce': result['cap_bounce'],
        'cap_depth': result['cap_depth'],
        'cap_hand': result['cap_hand'],
        'cap_window': result['cap_window'],
        'adverse_stop': result['adverse_stop'],
        'stop_order': 'STOP_LOSS stopPrice, amended as the daily high ratchets',
        'targets': {'cagr_minimum_inclusive': '1', 'mdd_maximum_inclusive': '0.30'},
        'targets_met': result['cagr'] >= 1 and result['mdd'] <= D('0.30'),
        'fee': extra.get('fee', format(FEE, 'f')),
        'entry_slip': extra.get('entry_slip', format(ENTRY_SLIP, 'f')),
        'exit_slip': extra.get('exit_slip', format(EXIT_SLIP, 'f')),
        'stop_slip': extra.get('stop_slip', format(STOP_SLIP, 'f')),
        'conversion': format(CONVERSION, 'f'),
        'fx': FX_BASIS,
        'qualification': 'NOT_QUALIFIED',
    }
    out['economic_qualification'] = 'MET' if out['targets_met'] else 'NOT_MET'
    out.update(extra)
    return out


def run_once(bars, fx, name: str, *, sma: int, trail: str, **kwargs) -> dict:
    result = simulate(
        bars, fx, start_ms=START_MS, end_ms=END_MS, sma_window=sma, trail=trail, **kwargs,
    )
    extra = {
        'fee': format(kwargs.get('fee', FEE), 'f'),
        'entry_slip': format(kwargs.get('entry_slip', ENTRY_SLIP), 'f'),
        'exit_slip': format(kwargs.get('exit_slip', EXIT_SLIP), 'f'),
        'stop_slip': format(kwargs.get('stop_slip', STOP_SLIP), 'f'),
    }
    public = _public(result, name, extra)
    public['daily_close_cny'] = [[iso(day)[:10], format(value, 'f')] for day, value in result['daily_cny']]
    public['trade_list'] = result['trades']
    return public


def skip_set(bars, ratio: D = D('0.2')) -> set[int]:
    """Deterministic 20% of in-window daily opens. Not chosen from prices."""
    chosen = set()
    for open_ms, *_rest in bars:
        if open_ms < START_MS or open_ms >= END_MS:
            continue
        digest = hashlib.sha256(f'spotquant-skip-20260928|{open_ms}'.encode()).digest()
        if D(int.from_bytes(digest[:8], 'big')) / D(2**64) < ratio:
            chosen.add(open_ms)
    return chosen


def block_set(bars) -> set[int]:
    """One seeded 21-day window of daily opens, derived without looking at prices."""
    opens = [open_ms for open_ms, *_rest in bars if START_MS <= open_ms < END_MS]
    digest = hashlib.sha256(b'spotquant-block-20260928').digest()
    index = int(D(int.from_bytes(digest[:8], 'big')) / D(2**64) * len(opens))
    first = opens[index]
    return {item for item in opens if first <= item < first + 21 * 86_400_000}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Spot account rebuild on daily Binance klines')
    parser.add_argument('name', nargs='?', default='P3')
    parser.add_argument('--market', default='/tmp/spotquant-market/klines')
    parser.add_argument('--sma', type=int, default=SMA_WINDOW)
    parser.add_argument('--trail', default=format(TRAIL, 'f'))
    parser.add_argument('--fee', default=format(FEE, 'f'))
    parser.add_argument('--entry-slip', default=format(ENTRY_SLIP, 'f'))
    parser.add_argument('--exit-slip', default=format(EXIT_SLIP, 'f'))
    parser.add_argument('--stop-slip', default=format(STOP_SLIP, 'f'))
    parser.add_argument('--sequence', default='primary', choices=('primary', 'skip', 'block'))
    parser.add_argument('--grid', action='store_true')
    parser.add_argument('--out', default=None)
    args = parser.parse_args(argv)
    bars = load_daily(Path(args.market), END_MS + 86_400_000)
    fx = DatedFX()
    identity = source_identity()
    market_hash = file_digest(Path(args.market))
    out_dir = Path(args.out) if args.out else OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {
        'fee': D(args.fee),
        'entry_slip': D(args.entry_slip),
        'exit_slip': D(args.exit_slip),
        'stop_slip': D(args.stop_slip),
    }
    if args.sequence == 'skip':
        kwargs['skip_entries'] = skip_set(bars)
    elif args.sequence == 'block':
        kwargs['skip_entries'] = block_set(bars)
    if args.grid:
        rows = []
        for sma in SMA_GRID:
            for trail in TRAIL_GRID:
                public = run_once(bars, fx, f'sma{sma}-t{trail}', sma=sma, trail=trail, **kwargs)
                public.pop('daily_close_cny', None)
                public.pop('trade_list', None)
                rows.append({key: public[key] for key in (
                    'trial', 'sma_window', 'trail', 'confirm', 'crash', 'fresh',
                    'final_cny', 'cost_net_cagr', 'continuous_mdd',
                    'mdd_at', 'trades', 'wins', 'targets_met', 'fees_usdt')})
                print(public['trial'], round(public['cost_net_cagr'] * 100, 2),
                      round(float(public['continuous_mdd']) * 100, 2), public['final_cny'], flush=True)

        def rank(row):
            return (D(row['final_cny']), -D(row['continuous_mdd']))

        best = max(rows, key=rank)
        feasible = [row for row in rows if row['targets_met']]
        payload = {
            'source': identity,
            'market_sha256': market_hash,
            'rule': ('diagnostic SMA x trail sweep under the current confirm, fresh-cross, '
                     'crash-filter, blow-off, crash-reversal, and adverse-close constants. '
                     'Not the P1 frontier. best_final is the highest terminal CNY.'),
            'best_final': best['trial'],
            'best_feasible': None if not feasible else max(feasible, key=rank)['trial'],
            'default': {
                'sma_window': SMA_WINDOW,
                'trail': format(TRAIL, 'f'),
                'confirm': CONFIRM,
                'crash': format(CRASH, 'f'),
                'fresh': FRESH,
                'extend': format(EXTEND, 'f'),
                'cap_drop': format(CAP_DROP, 'f'),
                'cap_bounce': format(CAP_BOUNCE, 'f'),
                'cap_depth': format(CAP_DEPTH, 'f'),
                'cap_hand': format(CAP_HAND, 'f'),
                'cap_window': CAP_WINDOW,
                'adverse_stop': format(ADVERSE, 'f'),
            },
            'rows': rows,
        }
        (out_dir / 'hold-grid.json').write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({key: payload[key] for key in ('best_final', 'best_feasible', 'default')}))
        return 0
    public = run_once(bars, fx, args.name, sma=args.sma, trail=args.trail, **kwargs)
    public['source'] = identity
    public['market_sha256'] = market_hash
    (out_dir / f'{args.name}.json').write_text(json.dumps(public, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: public[key] for key in (
        'trial', 'final_cny', 'cost_net_cagr', 'continuous_mdd', 'trades', 'targets_met',
        'economic_qualification')}, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
