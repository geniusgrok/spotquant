"""P6 fixed BTC baselines. No added capital or exchange access."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from research.account import Book, ENTRY_SLIP, EXIT_SLIP, FEE, INITIAL_CNY, YEAR_MS
from research.fx import BASIS, DatedFX
from research.market import file_digest, load_daily
from research.rebuild import END, END_MS, ROOT, START, START_MS, iso, run_sleeves, source_identity
from spotquant.model import DAY


def underwater_days(curve, initial):
    """Longest daily-close underwater run; an unrecovered run ends at last close."""
    peak, run, longest = D(initial), 0, 0
    for _day, value in curve:
        if D(value) >= peak:
            peak, run = D(value), 0
        else:
            run += 1
            longest = max(longest, run)
    return longest


def passive(bars, fx, profile, *, start_ms=START_MS, end_ms=END_MS,
            fee=FEE, entry_slip=ENTRY_SLIP, exit_slip=EXIT_SLIP):
    if profile not in ('cash', 'buy-hold', 'monthly-12'):
        raise ValueError('unknown fixed baseline')
    book = Book(fx, start_ms, fee=fee, entry_slip=entry_slip, exit_slip=exit_slip)
    initial = book.usdt
    scheduled = {int(datetime(2020, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
                 for month in range(1, 13)}
    daily, positions, fills = [], [], []
    for stamp, open_, high, low, close, _volume in bars:
        if not start_ms <= stamp < end_ms:
            continue
        spend = D(0)
        if profile == 'buy-hold' and stamp == start_ms:
            spend = book.usdt
        elif profile == 'monthly-12' and stamp in scheduled:
            spend = book.usdt if stamp == max(scheduled) else min(book.usdt, initial / 12)
        if spend:
            price = open_ * (1 + book.entry_slip)
            commission = spend * book.fee
            qty = (spend - commission) / price
            book.usdt -= spend
            book.btc += qty
            book.fees += commission
            fills.append({'time': iso(stamp), 'side': 'BUY', 'quantity': str(qty),
                          'price': str(price), 'spent_usdt': str(spend), 'fee_usdt': str(commission)})
        book.mark(book.equity_usdt(open_), stamp, adverse=True)
        book.mark(book.equity_usdt(high), stamp, adverse=False)
        book.mark(book.equity_usdt(low), stamp, adverse=True)
        equity = book.equity_usdt(close)
        cny = book._cny(equity, stamp + DAY - 1)
        book.mark_cny(cny, stamp + DAY - 1, adverse=True)
        daily.append([iso(stamp)[:10], str(cny)])
        positions.append({'date': iso(stamp)[:10], 'cash_usdt': str(book.usdt),
                          'btc': str(book.btc), 'equity_usdt': str(equity),
                          'btc_fraction': str(book.btc * close / equity)})
    if len(daily) != (end_ms - start_ms) // DAY:
        raise ValueError('baseline window is incomplete')
    final = D(daily[-1][1])
    return {'profile': profile, 'final_cny': str(final), 'final_usdt': str(equity),
            'cost_net_cagr': float(final / INITIAL_CNY) ** (1 / float(D(end_ms - start_ms) / YEAR_MS)) - 1,
            'continuous_mdd': str(book.mdd), 'mdd_at': iso(book.mdd_at),
            'fees_usdt': str(book.fees), 'orders': len(fills), 'fills': fills,
            'daily_close_cny': daily, 'daily_positions': positions,
            'longest_daily_underwater_days': underwater_days(daily, book._cny(initial, start_ms)),
            'mean_daily_btc_fraction': str(sum((D(row['btc_fraction']) for row in positions), D(0)) / len(positions)),
            'terminal_sale': False}


def decision(results):
    dominators = []
    for name in ('cash', 'buy-hold', 'monthly-12'):
        comparisons = [(row, results['P4'][scenario]) for scenario, row in results[name].items()]
        if all(row['cost_net_cagr'] >= base['cost_net_cagr'] - 1e-9
               and D(row['continuous_mdd']) <= D(base['continuous_mdd']) + D('1e-9')
               for row, base in comparisons) and any(
                   row['cost_net_cagr'] > base['cost_net_cagr'] + 1e-9
                   or D(row['continuous_mdd']) < D(base['continuous_mdd']) - D('1e-9')
                   for row, base in comparisons):
            dominators.append(name)
    return {'dominators': dominators, 'pause_P4_rule_expansion': bool(dominators),
            'production_default': 'P4', 'native_qualification': 'NOT_QUALIFIED'}


def run(market, out):
    if out.exists():
        raise ValueError('choose a new output directory; prior measurements are immutable')
    source = source_identity()
    if source['dirty']:
        raise ValueError('commit source before measuring')
    bars = load_daily(market, END_MS, require_through=END_MS)
    identity = file_digest(market)
    # Unlike the legacy loader's optional checksum, official P6 inputs require it.
    for path in (market / '1d').rglob('BTCUSDT-1d-*.zip'):
        if not Path(str(path) + '.CHECKSUM').is_file():
            raise ValueError(f'missing official checksum: {path.name}')
    fx = DatedFX()
    results = {name: {} for name in ('P4', 'cash', 'buy-hold', 'monthly-12')}
    scenarios = {'base': {}, 'fee150': {'fee': D('0.0015')},
                 'slip2': {'entry_slip': D('0.001'), 'exit_slip': D('0.001'), 'stop_slip': D('0.002')}}
    payloads = {}
    for scenario, kwargs in scenarios.items():
        p4 = run_sleeves(bars, fx, f'P6-P4-{scenario}', **kwargs)
        p4['longest_daily_underwater_days'] = underwater_days(
            p4['daily_close_cny'], Book(fx, START_MS).peak_cny)
        payloads[f'P4-{scenario}'] = p4
        for name in results:
            row = p4 if name == 'P4' else passive(
                bars, fx, name, **{key: value for key, value in kwargs.items() if key != 'stop_slip'})
            row.update(source=source, market_sha256=identity, start=START, end=END, end_exclusive=True,
                       initial_cny='10000', additional_capital='0', fx=BASIS, symbol='BTCUSDT',
                       market='spot', scenario=scenario, out_of_sample=False, native_qualification='NOT_QUALIFIED')
            row['targets_met'] = row['cost_net_cagr'] >= 1 and D(row['continuous_mdd']) <= D('.30')
            row['economic_qualification'] = 'MET' if row['targets_met'] else 'NOT_MET'
            payloads[f'{name}-{scenario}'] = row
            results[name][scenario] = {key: row[key] for key in (
                'final_cny', 'cost_net_cagr', 'continuous_mdd', 'fees_usdt', 'longest_daily_underwater_days', 'targets_met')}
    old = json.loads((ROOT / 'evidence/sleeves-20260929/P4.json').read_text())
    reproduced = all(abs(D(str(payloads['P4-base'][key])) - D(str(old[key]))) < D('1e-9')
                     for key in ('final_cny', 'continuous_mdd', 'cost_net_cagr'))
    if not reproduced:
        raise ValueError('P4 baseline failed to reproduce; no measurement published')
    report = {'source': source, 'market_sha256': identity, 'baseline_reproduced': reproduced,
              'results': results, 'decision': decision(results), 'out_of_sample': False,
              'execution_model': 'daily bars; high-before-low drawdown envelope; no native execution'}
    out.mkdir(parents=True)
    for name, row in payloads.items():
        (out / f'{name}.json').write_text(json.dumps(row, indent=2, allow_nan=False) + '\n')
    (out / 'suite.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps(report, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market', type=Path, default=Path('/tmp/spotquant-market/klines'))
    parser.add_argument('--out', type=Path, default=ROOT / 'evidence/baselines-20261001')
    args = parser.parse_args(argv)
    run(args.market, args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
