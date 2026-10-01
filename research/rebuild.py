"""Economic meter for the spot account.

The default trial is P4: three SMA sleeves (30, 40, and 50) on one USDT pool,
using the ``spotquant.model`` constants. ``--book single --sma 40`` runs the
earlier single-sleeve P3 book under the same constants; on this window it prints
the P3 trades and the P3 account. ``--suite`` writes the whole P4 evidence set
(base, stresses, the 4% inclusion check, the volatility-scaling test, the P3
reproduction, single-sleeve neighbors of SMA 40, the ETHUSDT check with frozen
rules, and the plateau scan).
``--grid`` sweeps SMA window and trail for one sleeve and writes
``hold-grid.json``. Nothing here replaces ``rebuild-20260928/frontier.json`` or
its P1, P2, and P3 files; writing into that directory is refused. Partial runs
are not written into the evidence directory.
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
    CONVERSION, ENTRY_SLIP, EXIT_SLIP, FEE, INITIAL_CNY, STOP_SLIP, VOL_TARGET, VOL_WINDOW,
    simulate, simulate_sleeves,
)
from research.fx import BASIS as FX_BASIS, DatedFX
from research.market import file_digest, load_daily
from spotquant.model import (
    ADVERSE, CAP_BOUNCE, CAP_DEPTH, CAP_DROP, CAP_HAND, CAP_WINDOW, CONFIRM, CRASH,
    EXTEND, FRESH, HIGH_WINDOW, SLEEVES, SMA_WINDOW, TRAIL,
)

ROOT = Path(__file__).resolve().parents[1]
OLD_OUT = ROOT / 'evidence' / 'rebuild-20260928'
OUT = ROOT / 'evidence' / 'sleeves-20260929'
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
        'pending_stop_exit': bool(result.get('pending_stop_exit')),
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
        'stop_order': 'STOP_LOSS stopPrice from the prior peak; a completed bar tightens it for the next day',
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


def outage_set(bars) -> set[int]:
    """21 daily opens from 2020-03-01. That month holds the recorded P3 drawdown.

    The set is a fixed calendar, not a search for a large effect.
    """
    start = 1583020800000
    return {open_ms for open_ms, *_rest in bars if start <= open_ms < start + 21 * 86_400_000}


def block_set(bars) -> set[int]:
    """One seeded 21-day window of daily opens, derived without looking at prices."""
    opens = [open_ms for open_ms, *_rest in bars if START_MS <= open_ms < END_MS]
    digest = hashlib.sha256(b'spotquant-block-20260928').digest()
    index = int(D(int.from_bytes(digest[:8], 'big')) / D(2**64) * len(opens))
    first = opens[index]
    return {item for item in opens if first <= item < first + 21 * 86_400_000}


P3_CONSTANTS = {
    'extend': D('0.60'), 'cap_drop': D('0.08'), 'cap_bounce': D('0.06'),
    'cap_depth': D('0.50'), 'cap_hand': D('0.20'),
}
PLATEAU_RANGES = {
    'extend': (D('0.40'), D('0.90')),
    'cap_drop': (D('0.005'), D('0.40')),
    'cap_bounce': (D('0.005'), D('0.30')),
    'cap_depth': (D('0.30'), D('0.90')),
    'cap_hand': (D('0.005'), D('0.50')),
}
PLATEAU_STEP = D('0.005')
ETH_MARKET = '/tmp/spotquant-market/klines-eth'


def model_constants(adverse=None) -> dict:
    return {
        'extend': EXTEND, 'cap_drop': CAP_DROP, 'cap_bounce': CAP_BOUNCE, 'cap_depth': CAP_DEPTH,
        'cap_hand': CAP_HAND, 'adverse_stop': ADVERSE if adverse is None else D(adverse),
    }


def _win_counts(trades, fee) -> tuple[int, int]:
    """Gross is exit price above entry price. Net charges the fee on both sides."""
    threshold = D(1) / (D(1) - D(fee)) ** 2
    gross = sum(1 for item in trades if D(item['exit']) > D(item['entry']))
    net = sum(1 for item in trades if D(item['exit']) / D(item['entry']) > threshold)
    return gross, net


def _public_sleeves(result: dict, name: str, symbol: str, windows, constants: dict, extra: dict) -> dict:
    trades = result['trades']
    gross_wins, wins = _win_counts(trades, extra.get('fee', FEE))
    kinds = {}
    per_sleeve = {}
    for item in trades:
        kinds[item['kind']] = kinds.get(item['kind'], 0) + 1
        per_sleeve[str(item['sleeve'])] = per_sleeve.get(str(item['sleeve']), 0) + 1
    out = {
        'trial': name,
        'symbol': symbol,
        'market': 'spot',
        'leverage': '0',
        'shorting': False,
        'book': 'sleeves' if len(windows) > 1 else 'single',
        'sleeves': list(windows),
        'pool_rule': ('one USDT pool; an armed sleeve buys pool / sleeves holding nothing after '
                      "that open's exits; each sleeve exits and stops on its own coins"),
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
        'gross_wins': gross_wins,
        'wins_are': 'net of the fee on both sides',
        'exit_kinds': kinds,
        'trades_per_sleeve': per_sleeve,
        'position_base': format(result['position_btc'], 'f'),
        'position_by_sleeve': {str(key): format(value, 'f') for key, value in result['positions'].items()},
        'skipped_entries': result['skipped_entries'],
        'pending_stop_exit': bool(result.get('pending_stop_exit')),
        'confirm': CONFIRM,
        'crash': format(CRASH, 'f'),
        'fresh': FRESH,
        'high_window': HIGH_WINDOW,
        'trail': format(TRAIL, 'f'),
        'cap_window': CAP_WINDOW,
        'stop_order': 'STOP_LOSS stopPrice from the prior peak; a completed bar tightens it for the next day',
        'targets': {'cagr_minimum_inclusive': '1', 'mdd_maximum_inclusive': '0.30'},
        'targets_met': result['cagr'] >= 1 and result['mdd'] <= D('0.30'),
        'conversion': format(CONVERSION, 'f'),
        'fx': FX_BASIS,
        'qualification': 'NOT_QUALIFIED',
    }
    for key, value in constants.items():
        out[key] = format(value, 'f')
    out['economic_qualification'] = 'MET' if out['targets_met'] else 'NOT_MET'
    out.update(extra)
    return out


def run_sleeves(bars, fx, name: str, *, symbol='BTCUSDT', windows=SLEEVES, adverse=None,
                vol_target=None, series=True, rules=None, **kwargs) -> dict:
    constants = model_constants(adverse)
    constants.update(rules or {})
    result = simulate_sleeves(
        bars, fx, start_ms=START_MS, end_ms=END_MS, windows=windows,
        model_kwargs=dict(constants), vol_target=vol_target, **kwargs,
    )
    extra = {
        'fee': format(kwargs.get('fee', FEE), 'f'),
        'entry_slip': format(kwargs.get('entry_slip', ENTRY_SLIP), 'f'),
        'exit_slip': format(kwargs.get('exit_slip', EXIT_SLIP), 'f'),
        'stop_slip': format(kwargs.get('stop_slip', STOP_SLIP), 'f'),
        'vol_target': None if vol_target is None else format(D(vol_target), 'f'),
        'vol_window': VOL_WINDOW if vol_target is not None else None,
    }
    public = _public_sleeves(result, name, symbol, windows, constants, extra)
    if series:
        public['daily_close_cny'] = [[iso(day)[:10], format(value, 'f')] for day, value in result['daily_cny']]
    public['trade_list'] = result['trades']
    return public


def _keys(result: dict):
    return [(t['entry_ms'], t['exit_ms'], t['entry'], t['exit'], t['kind']) for t in result['trades']]


def plateau_scan(bars, fx) -> dict:
    """Protocol step 3. Single SMA 40, one constant at a time, others at their P3 values."""
    def trades(**override):
        kwargs = dict(P3_CONSTANTS)
        kwargs.update(override)
        return _keys(simulate_sleeves(
            bars, fx, start_ms=START_MS, end_ms=END_MS, windows=(40,), model_kwargs=kwargs))

    base = trades()
    rows = {}
    for name, (low, high) in PLATEAU_RANGES.items():
        center = P3_CONSTANTS[name]
        lower = center
        while lower - PLATEAU_STEP >= low and trades(**{name: lower - PLATEAU_STEP}) == base:
            lower -= PLATEAU_STEP
        upper = center
        while upper + PLATEAU_STEP <= high and trades(**{name: upper + PLATEAU_STEP}) == base:
            upper += PLATEAU_STEP
        middle = ((lower + upper) / 2).quantize(D('0.01'), rounding='ROUND_HALF_EVEN')
        adopted = middle if trades(**{name: middle}) == base else center
        rows[name] = {
            'p3_value': format(center, 'f'),
            'plateau_low': format(lower, 'f'),
            'plateau_high': format(upper, 'f'),
            'scan_low': format(low, 'f'),
            'scan_high': format(high, 'f'),
            'midpoint_rounded': format(middle, 'f'),
            'adopted': format(adopted, 'f'),
        }
    joint = {name: D(rows[name]['adopted']) for name in rows}
    return {
        'rule': ('single SMA 40 book, one constant at a time in steps of 0.005 outward from its P3 '
                 'value while the trades stay identical; adopted = midpoint rounded to 0.01 if that '
                 'value prints the identical trades, else the P3 value'),
        'constants': rows,
        'joint_prints_p3_trades': trades(**joint) == base,
        'model_constants_match': all(
            D(rows[name]['adopted']) == model_constants()[name] for name in rows),
    }


def run_suite(bars, eth_bars, fx, out_dir: Path, identity: dict, hashes: dict, sequences: dict) -> dict:
    def save(public: dict, name: str, *, keep_series=False):
        if not keep_series:
            public.pop('daily_close_cny', None)
        public['source'] = identity
        public['market_sha256'] = hashes[public['symbol']]
        (out_dir / f'{name}.json').write_text(json.dumps(public, indent=2) + '\n', encoding='utf-8')
        return public

    def line(public):
        return {key: public[key] for key in (
            'trial', 'final_cny', 'cost_net_cagr', 'continuous_mdd', 'mdd_at', 'trades', 'targets_met')}

    summary = {}
    base = save(run_sleeves(bars, fx, 'P4', series=True), 'P4', keep_series=True)
    summary['P4'] = line(base)
    stresses = {
        'P4-fee': {'fee': D('0.0015')},
        'P4-slip': {'exit_slip': D('0.001'), 'stop_slip': D('0.002')},
        'P4-skip': {'skip_entries': sequences['skip']},
        'P4-block': {'skip_entries': sequences['block']},
        'P4-outage': {'frozen': sequences['outage']},
    }
    for name, kwargs in stresses.items():
        public = run_sleeves(bars, fx, name, series=False, **kwargs)
        if name == 'P4-block':
            public['note'] = (
                'Skips new buys only. skipped_entries of 0 means the seeded 21 days contained '
                'no armed open. It is not a 21-day loss of the position or the stop.'
            )
        if name == 'P4-outage':
            public['note'] = (
                '21 days from 2020-03-01 freeze signal exits and stop tightening. '
                'The resting stop from before the window still trades. '
                'The date is the month of the recorded P3 drawdown, not a searched window.'
            )
        summary[name] = line(save(public, name))
    variants = {}
    for label, distance in (('P4-adv000', '0'), ('P4-adv035', '0.035'), ('P4-adv040', '0.04'), ('P4-adv045', '0.045')):
        public = save(run_sleeves(bars, fx, label, adverse=distance, series=False), label)
        variants[label] = public
        summary[label] = line(public)
    off = variants['P4-adv000']
    def drawdown(public):
        return D(public['continuous_mdd']).quantize(D('0.000000001'))

    include = all(
        drawdown(variants[label]) <= drawdown(off)
        and variants[label]['cost_net_cagr'] >= off['cost_net_cagr'] - 0.01
        for label in ('P4-adv035', 'P4-adv040', 'P4-adv045')
    )
    scaled = save(run_sleeves(bars, fx, 'P4-vol070', vol_target=VOL_TARGET, series=False), 'P4-vol070')
    summary['P4-vol070'] = line(scaled)
    adopt_vol = (
        drawdown(scaled) <= drawdown(base) - D('0.01')
        and scaled['cost_net_cagr'] >= base['cost_net_cagr'] - 0.03
    )
    reproduction = save(run_sleeves(
        bars, fx, 'P3-reproduction', windows=(40,), adverse=ADVERSE, series=False), 'P3-reproduction')
    summary['P3-reproduction'] = line(reproduction)
    for label, book in (('ETH-P4', SLEEVES), ('ETH-P3', (40,))):
        public = save(run_sleeves(eth_bars, fx, label, symbol='ETHUSDT', windows=book, series=False), label)
        summary[label] = line(public)
    neighbors = {}
    for window in (30, 35, 40, 45, 50):
        public = run_sleeves(bars, fx, f'single-{window}', windows=(window,), series=False)
        neighbors[str(window)] = line(public)
    (out_dir / 'neighbors.json').write_text(json.dumps({
        'rule': 'one sleeve at a time under the P4 constants: how much the P3 result depends on SMA 40',
        'source': identity,
        'results': neighbors,
    }, indent=2) + '\n', encoding='utf-8')
    plateau = plateau_scan(bars, fx)
    plateau['source'] = identity
    (out_dir / 'plateau.json').write_text(json.dumps(plateau, indent=2) + '\n', encoding='utf-8')
    decisions = {
        'adverse_close_included': include,
        'adverse_close_default': str(ADVERSE),
        'vol_scaling_adopted': adopt_vol,
        'thresholds_match_plateau_centers': plateau['model_constants_match'],
        'joint_thresholds_print_p3_trades': plateau['joint_prints_p3_trades'],
        'reproduction_matches_p3': (
            reproduction['final_cny'] == '1113885.265822092834607674624'
            and reproduction['trades'] == 37),
    }
    decisions['consistent_with_defaults'] = bool(
        decisions['adverse_close_included'] == (ADVERSE > 0) and not adopt_vol
        and decisions['thresholds_match_plateau_centers'] and decisions['reproduction_matches_p3'])
    payload = {'source': identity, 'decisions': decisions, 'results': summary}
    (out_dir / 'suite.json').write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description='Spot account rebuild on daily Binance klines')
    parser.add_argument('name', nargs='?', default='P4')
    parser.add_argument('--market', default='/tmp/spotquant-market/klines')
    parser.add_argument('--eth-market', default=ETH_MARKET)
    parser.add_argument('--symbol', default='BTCUSDT')
    parser.add_argument('--book', default='sleeves', choices=('sleeves', 'single'))
    parser.add_argument('--windows', default=','.join(str(item) for item in SLEEVES))
    parser.add_argument('--sma', type=int, default=SMA_WINDOW)
    parser.add_argument('--trail', default=format(TRAIL, 'f'))
    parser.add_argument('--adverse', default=None)
    parser.add_argument('--vol-target', default=None)
    parser.add_argument('--fee', default=format(FEE, 'f'))
    parser.add_argument('--entry-slip', default=format(ENTRY_SLIP, 'f'))
    parser.add_argument('--exit-slip', default=format(EXIT_SLIP, 'f'))
    parser.add_argument('--stop-slip', default=format(STOP_SLIP, 'f'))
    parser.add_argument('--sequence', default='primary', choices=('primary', 'skip', 'block'))
    parser.add_argument('--grid', action='store_true')
    parser.add_argument('--suite', action='store_true')
    parser.add_argument('--simplify', action='store_true', help='registered P5 rule deletions, BTC only')
    parser.add_argument('--out', default=None)
    args = parser.parse_args(argv)
    out_dir = Path(args.out) if args.out else (
        ROOT / 'evidence' / 'simplify-20261001' if args.simplify else OUT)
    if out_dir.resolve() == OLD_OUT.resolve():
        raise SystemExit('refusing to write into the P1, P2, and P3 evidence directory')
    bars = load_daily(Path(args.market), END_MS + 86_400_000, args.symbol, require_through=END_MS)
    fx = DatedFX()
    identity = source_identity()
    market_hash = file_digest(Path(args.market), args.symbol)
    if identity['dirty'] and ROOT / 'evidence' in out_dir.resolve().parents:
        raise SystemExit('evidence under evidence/ must come from a clean committed tree')
    out_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {
        'fee': D(args.fee),
        'entry_slip': D(args.entry_slip),
        'exit_slip': D(args.exit_slip),
        'stop_slip': D(args.stop_slip),
    }
    if args.simplify:
        if args.symbol != 'BTCUSDT' or args.suite or args.grid or args.book != 'sleeves':
            raise SystemExit('P5 uses only the frozen BTC sleeves account')
        if (args.windows != ','.join(str(item) for item in SLEEVES)
                or args.sequence != 'primary' or args.adverse is not None or args.vol_target is not None
                or D(args.fee) != FEE or D(args.entry_slip) != ENTRY_SLIP
                or D(args.exit_slip) != EXIT_SLIP or D(args.stop_slip) != STOP_SLIP):
            raise SystemExit('P5 uses the registered constants and scenarios; custom knobs are refused')
        from research.simplify import run_simplification
        payload = run_simplification(bars, fx, out_dir, identity, market_hash)
        print(json.dumps(payload['decision'], indent=2))
        return 0 if payload['baseline_reproduced'] else 1
    if args.suite:
        eth_bars = load_daily(Path(args.eth_market), END_MS + 86_400_000, 'ETHUSDT', require_through=END_MS)
        payload = run_suite(
            bars, eth_bars, fx, out_dir, identity,
            {'BTCUSDT': market_hash, 'ETHUSDT': file_digest(Path(args.eth_market), 'ETHUSDT')},
            {'skip': skip_set(bars), 'block': block_set(bars), 'outage': outage_set(bars)},
        )
        print(json.dumps(payload['decisions'], indent=2))
        for name, item in payload['results'].items():
            print(name, round(item['cost_net_cagr'] * 100, 2), round(float(item['continuous_mdd']) * 100, 2),
                  item['final_cny'], item['trades'])
        return 0 if payload['decisions']['consistent_with_defaults'] else 1
    if args.sequence == 'skip':
        kwargs['skip_entries'] = skip_set(bars)
    elif args.sequence == 'block':
        kwargs['skip_entries'] = block_set(bars)
    if args.grid:
        return grid(bars, fx, kwargs, identity, market_hash, out_dir)
    if args.book == 'sleeves':
        windows = tuple(int(item) for item in args.windows.split(','))
        public = run_sleeves(
            bars, fx, args.name, symbol=args.symbol, windows=windows, adverse=args.adverse,
            vol_target=None if args.vol_target is None else D(args.vol_target), **kwargs)
    else:
        public = run_once(bars, fx, args.name, sma=args.sma, trail=args.trail, **kwargs)
    public['source'] = identity
    public['market_sha256'] = market_hash
    (out_dir / f'{args.name}.json').write_text(json.dumps(public, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: public[key] for key in (
        'trial', 'final_cny', 'cost_net_cagr', 'continuous_mdd', 'trades', 'targets_met',
        'economic_qualification')}, default=str))
    return 0


def grid(bars, fx, kwargs, identity, market_hash, out_dir: Path) -> int:
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
        'rule': ('diagnostic SMA x trail sweep for one sleeve under the current confirm, fresh-cross, '
                 'crash-filter, blow-off, crash-reversal, and adverse-close constants. '
                 'Not the P1 frontier. best_final is the highest terminal CNY.'),
        'best_final': best['trial'],
        'best_feasible': None if not feasible else max(feasible, key=rank)['trial'],
        'default': {
            'sleeves': list(SLEEVES),
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


if __name__ == '__main__':
    raise SystemExit(main())
