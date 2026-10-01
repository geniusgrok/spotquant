"""Registered decisions and descriptive BTC exposure attribution, no new search."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
import math
from pathlib import Path
import statistics

from research.complete_spot import CANDIDATES, SCENARIOS, PriorFX
from research.market import load_daily
from research.rebuild import START_MS, END_MS, source_identity
from spotquant.model import DAY


def select_spot(results):
    decisions, eligible = {}, []
    for candidate in CANDIDATES:
        rows = [results.get(candidate + '-' + s, {}) for s in SCENARIOS]
        reference = [results.get('default-' + s, {}) for s in SCENARIOS]
        complete = all(r.get('complete') and r.get('audit', {}).get('passed') for r in rows)
        comparable = complete and all(r.get('complete') and r.get('audit', {}).get('passed') for r in reference)
        matched = comparable and all(D(r['mdd']) <= D(b['mdd']) + D('1e-9') and r['cagr'] >= b['cagr'] - .01 for r, b in zip(rows, reference))
        improvement = comparable and (rows[0]['cagr'] >= reference[0]['cagr'] + .01 or
            (D(rows[0]['mdd']) <= D(reference[0]['mdd']) - D('.01') and rows[0]['cagr'] >= reference[0]['cagr'] - .03))
        accepted = candidate != 'default' and matched and improvement
        decisions[candidate] = {'complete_audited': complete, 'matched_constraints': matched,
                               'improvement': improvement, 'eligible': accepted,
                               'worst_cagr': min(r['cagr'] for r in rows) if complete else None}
        if accepted:
            eligible.append(candidate)
    selected = max(eligible, key=lambda c: (decisions[c]['worst_cagr'], -CANDIDATES.index(c))) if eligible else 'default'
    return {'selected_research_candidate': selected, 'decisions': decisions,
            'production_promoted': False, 'native_execution_verified': False}


def regression(account, market):
    if len(account) != len(market) or len(account) < 3:
        raise ValueError('matched daily returns required')
    x, y = statistics.mean(market), statistics.mean(account)
    denominator = sum((v - x) ** 2 for v in market)
    if denominator <= 0:
        raise ValueError('market variance is zero')
    beta = sum((a - y) * (b - x) for a, b in zip(account, market)) / denominator
    intercept = y - beta * x
    errors = [a - intercept - beta * b for a, b in zip(account, market)]
    # Newey-West covariance of the intercept with seven fixed daily lags.
    n, xx = len(market), sum(v * v for v in market)
    determinant = n * xx - sum(market) ** 2
    influence = [e * (xx - sum(market) * v) / determinant for e, v in zip(errors, market)]
    variance = sum(v * v for v in influence)
    for lag in range(1, min(7, n - 1) + 1):
        variance += 2 * (1 - lag / 8) * sum(influence[i] * influence[i - lag] for i in range(lag, n))
    variance = max(0, variance) * n / (n - 2)
    return {'beta_btc': beta, 'intercept_daily': intercept,
            'residual_arithmetic_annualized': 365.25 * intercept,
            'intercept_hac7_t_descriptive': intercept / math.sqrt(variance) if variance else None,
            'residual_volatility_annualized': statistics.stdev(errors) * math.sqrt(365.25),
            'days': n, 'prospective_alpha_proven': False}


def daily_metrics(values, initial):
    if not values or any(v <= 0 for v in values):
        raise ValueError('positive account curve required')
    peak, mdd, underwater, longest = initial, 0, 0, 0
    returns, previous = [], initial
    for value in values:
        returns.append(value / previous - 1)
        previous = value
        peak = max(peak, value)
        mdd = max(mdd, 1 - value / peak)
        underwater = underwater + 1 if value < peak else 0
        longest = max(longest, underwater)
    tail = sorted(returns)[:max(1, math.ceil(len(returns) * .05))]
    return {'daily_mdd': mdd, 'longest_daily_underwater_days': longest,
            'daily_es5_loss': -statistics.mean(tail), 'worst_day': min(returns),
            'daily_volatility_annualized': statistics.stdev(returns) * math.sqrt(365.25),
            'cagr': (values[-1] / initial) ** (365.25 / len(values)) - 1,
            'final_cny': values[-1]}, returns


def canonical(row, market, fx, initial, kind):
    """Only flat wallet can carry over missing dates; held BTC needs daily marks."""
    raw = row['daily']
    raw = list(raw.values()) if isinstance(raw, dict) else raw
    time_key = 'timestamp_ms' if kind == 'spot' else 'stamp_ms'
    qty_key = 'btc' if kind == 'spot' else 'quantity_btc'
    cash_key = 'cash_usdt' if kind == 'spot' else 'wallet_usdt'
    raw = sorted(raw, key=lambda r: r[time_key])
    previous = {time_key: START_MS, qty_key: '0', cash_key: str(D(str(initial)) / fx(START_MS) * D('.999'))}
    index, curve = 0, []
    for day, _, _, _, close, _ in market:
        if not START_MS <= day < END_MS:
            continue
        target = day + DAY
        while index < len(raw) and raw[index][time_key] <= target:
            previous = raw[index]
            index += 1
        quantity = D(previous[qty_key])
        if quantity and previous[time_key] != target:
            raise ValueError('held position lacks closing daily mark at ' + str(target))
        if quantity:
            usdt = D(previous['equity_usdt'])
        else:
            usdt = D(previous[cash_key])
        cny = usdt * fx(target) * D('.999')
        # Exposure is a mark sensitivity at the daily close, not average intraday leverage.
        curve.append({'day_ms': day, 'equity_cny': float(cny), 'equity_usdt': float(usdt),
                      'net_btc': float(quantity), 'price_usdt': float(close),
                      'gross_exposure_over_equity': float(abs(quantity) * close / usdt) if usdt else None,
                      'mark_timestamp_ms': previous[time_key]})
    if len(curve) != (END_MS - START_MS) // DAY:
        raise ValueError('full daily coverage required')
    if abs(curve[-1]['equity_cny'] - float(row['final_cny'])) > max(.01, float(row['final_cny']) * 1e-8):
        raise ValueError('daily terminal equity differs from account')
    return curve


def attribution(row, curve, market_returns, initial, fx):
    metrics, returns = daily_metrics([r['equity_cny'] for r in curve], initial)
    initial_usdt = float(D(str(initial)) / fx(START_MS) * D('.999'))
    _, usd_returns = daily_metrics([r['equity_usdt'] for r in curve], initial_usdt)
    years = {}
    for item, value in zip(curve, returns):
        year = datetime.fromtimestamp(item['day_ms'] / 1000, timezone.utc).year
        years[year] = (years.get(year, 1) * (1 + value))
    down = [i for i, r in enumerate(market_returns) if r < 0]
    return {'metrics': metrics, 'usdt_btc_regression': regression(usd_returns, market_returns),
            'cny_btc_regression': regression(returns, market_returns),
            'down_market_beta': regression([usd_returns[i] for i in down], [market_returns[i] for i in down])['beta_btc'],
            'calendar_returns': {str(y): v - 1 for y, v in years.items()},
            'mean_closing_gross_exposure_over_equity': statistics.mean(r['gross_exposure_over_equity'] for r in curve),
            'days_with_closing_btc_position': sum(bool(r['net_btc']) for r in curve),
            'fees_usdt': row.get('fees', row.get('audit', {}).get('fees_usdt')),
            'funding_paid_usdt': row.get('funding', '0'),
            'continuous_mdd_from_account': float(row['mdd']), 'native_execution_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spot', type=Path, required=True)
    parser.add_argument('--perp', type=Path, required=True)
    parser.add_argument('--spot-budgets', type=Path, required=True)
    parser.add_argument('--perp-budgets', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error('never overwrite an assessment')
    # Budget schema is validated after actual budget replays complete.
    inputs = {name: json.loads(getattr(args, name).read_text()) for name in ('spot', 'perp', 'spot_budgets', 'perp_budgets')}
    bars = load_daily(Path('/tmp/spotquant-market/klines'), END_MS, require_through=END_MS)
    fx = PriorFX(Path('../starquant/data/usdcny_frankfurter.json'))
    active = [b for b in bars if START_MS <= b[0] < END_MS]
    previous_price = active[0][1]
    btc_returns = []
    for row in active:
        btc_returns.append(float(row[4] / previous_price - 1))
        previous_price = row[4]
    report = {'analysis_source': source_identity(), 'inputs': {n: hashlib.sha256(getattr(args, n).read_bytes()).hexdigest() for n in inputs},
              'input_sources': {'spot': inputs['spot']['source'], 'perp': inputs['perp']['inputs']['source']},
              'spot_selection': select_spot(inputs['spot']['results']), 'attribution': {},
              'qualification': 'NOT_QUALIFIED', 'native_execution_verified': False,
              'limitations': ['All history already studied; regression intercept is descriptive and not proof of alpha.',
                              'Closing exposure is not maximum intraday leverage; joint MDD uses daily curves.',
                              'Separate BTC accounts do not provide asset diversification; no added capital or transfers.']}
    spot = inputs['spot']['results']['default-base']
    perp = inputs['perp']['results']['incumbent']['base']
    for name, row in (('spot', spot), ('perp', perp)):
        if not row.get('complete') or not row['audit']['passed']:
            raise ValueError('complete audited baseline required')
        curve = canonical(row, bars, fx, 10000, name)
        report['attribution'][name] = attribution(row, curve, btc_returns, 10000, fx)
    # Filled by fixed-account replay integration, deliberately no curve scaling.
    report['joint_fixed_capital'] = joint(inputs, bars, fx)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')


def joint(inputs, bars, fx):
    rows = []
    for spot_budget in (0, 2500, 5000, 7500, 10000):
        perp_budget = 10000 - spot_budget
        curves = []
        for kind, budget in (('spot', spot_budget), ('perp', perp_budget)):
            if not budget:
                continue
            if budget == 10000:
                row = inputs[kind]['results']['default-base'] if kind == 'spot' else inputs[kind]['results']['incumbent']['base']
            else:
                row = inputs[kind + '_budgets']['results'][str(budget)]
            if not row.get('complete') or not row['audit']['passed'] or int(D(str(row.get('initial_cny', 10000)))) != budget:
                raise ValueError('complete audited actual budget required: ' + kind + '/' + str(budget))
            curves.append(canonical(row, bars, fx, budget, kind))
        combined = [sum(curve[i]['equity_cny'] for curve in curves) for i in range(len(curves[0]))]
        metrics, _ = daily_metrics(combined, 10000)
        rows.append({'spot_initial_cny': spot_budget, 'perp_initial_cny': perp_budget,
                     'metrics': metrics, 'continuous_joint_mdd_verified': False,
                     'fixed_accounts_no_transfers': True,
                     'maximum_closing_gross_exposure_over_equity': max(
                         sum(abs(curve[i]['net_btc']) * curve[i]['price_usdt'] for curve in curves) /
                         sum(curve[i]['equity_usdt'] for curve in curves) for i in range(len(combined))),
                     'daily_equity_cny': combined})
    return rows


if __name__ == '__main__':
    main()
