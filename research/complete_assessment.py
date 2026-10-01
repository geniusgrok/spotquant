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
from research.market import load_daily, file_digest
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
    standard_error = math.sqrt(variance)
    return {'beta_btc': beta, 'intercept_daily': intercept,
            'residual_arithmetic_annualized': 365.25 * intercept,
            'intercept_hac7_t_descriptive': intercept / standard_error if variance else None,
            'intercept_hac7_standard_error_daily': standard_error,
            'residual_arithmetic_annualized_normal95_descriptive': [
                365.25 * (intercept - 1.96 * standard_error),
                365.25 * (intercept + 1.96 * standard_error)],
            'uncertainty_limit': 'Descriptive fixed seven-lag normal approximation; no selection adjustment or prospective claim.',
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


def attribution(row, curve, market_returns, cny_market_returns, initial, fx):
    metrics, returns = daily_metrics([r['equity_cny'] for r in curve], initial)
    initial_usdt = float(D(str(initial)) / fx(START_MS) * D('.999'))
    _, usd_returns = daily_metrics([r['equity_usdt'] for r in curve], initial_usdt)
    years = {}
    for item, value in zip(curve, returns):
        year = datetime.fromtimestamp(item['day_ms'] / 1000, timezone.utc).year
        years[year] = (years.get(year, 1) * (1 + value))
    down = [i for i, r in enumerate(market_returns) if r < 0]
    up = [i for i, r in enumerate(market_returns) if r > 0]
    capture = {name: {'days': len(indices),
                     'mean_account_usdt_return': statistics.mean(usd_returns[i] for i in indices),
                     'mean_btc_usdt_return': statistics.mean(market_returns[i] for i in indices),
                     'arithmetic_capture_ratio': sum(usd_returns[i] for i in indices) /
                                                sum(market_returns[i] for i in indices)}
               for name, indices in (('up', up), ('down', down)) if indices}
    return {'metrics': metrics, 'usdt_btc_regression': regression(usd_returns, market_returns),
            'cny_btc_regression': regression(returns, cny_market_returns),
            'down_market_beta': regression([usd_returns[i] for i in down], [market_returns[i] for i in down])['beta_btc'],
            'calendar_returns': {str(y): v - 1 for y, v in years.items()},
            'mean_closing_gross_exposure_over_equity': statistics.mean(r['gross_exposure_over_equity'] for r in curve),
            'mean_closing_signed_exposure_over_equity': statistics.mean(
                (1 if r['net_btc'] >= 0 else -1) * r['gross_exposure_over_equity'] for r in curve),
            'btc_up_down_day_capture': capture,
            'days_with_closing_btc_position': sum(bool(r['net_btc']) for r in curve),
            'days_with_closing_btc_notional_at_least_usdt5': sum(abs(r['net_btc']) * r['price_usdt'] >= 5 for r in curve),
            'days_with_closing_short_position': sum(r['net_btc'] < 0 for r in curve),
            'fees_usdt': row.get('fees', row.get('audit', {}).get('fees_usdt')),
            'funding_paid_usdt': row.get('funding', '0'),
            'fill_count': len(row.get('fills', row.get('trades', []))),
            'continuous_mdd_from_account': float(row['mdd']), 'native_execution_verified': False}


def passive_controls(bars, fx):
    """Economic benchmarks at fixed monthly opens, not actual-session accounts."""
    active = [b for b in bars if START_MS <= b[0] < END_MS]
    initial_cash = D(10000) / fx(START_MS) * D('.999')
    report = {}
    for method in ('cash', 'buy_hold', 'btc25_cash75', 'btc50_cash50', 'btc75_cash25',
                   'twelve_month_dca', 'volatility_control40'):
        cash, btc, curve, purchases = initial_cash, D(0), [], 0
        history = [b[4] for b in bars if b[0] < START_MS]
        for day, open_, high, low, close, volume in active:
            date = datetime.fromtimestamp(day / 1000, timezone.utc)
            fraction = {'buy_hold': D(1), 'btc25_cash75': D('.25'),
                        'btc50_cash50': D('.5'), 'btc75_cash25': D('.75')}.get(method)
            buy = (fraction is not None and day == START_MS) or (
                method == 'twelve_month_dca' and date.year == 2020 and date.day == 1)
            if buy:
                spent = initial_cash * fraction if fraction is not None else min(cash, initial_cash / 12)
                # Same spot fee, entry slippage and final mark convention.
                btc += spent * D('.999') / (open_ * D('1.0005'))
                cash -= spent
                purchases += 1
            if method == 'volatility_control40':
                if len(history) < 21:
                    raise ValueError('volatility benchmark requires prior completed warmup')
                returns = [history[i] / history[i - 1] - 1 for i in range(len(history) - 20, len(history))]
                rms = (sum(r * r for r in returns) / 20).sqrt()
                weight = min(D(1), D('.4') / (rms * D('365.25').sqrt())) if rms else D(1)
                target = (cash + btc * open_) * weight
                difference = target - btc * open_
                if difference > 0:
                    spent = min(cash, difference)
                    btc += spent * D('.999') / (open_ * D('1.0005'))
                    cash -= spent
                    purchases += 1
                elif difference < 0:
                    sold = min(btc, -difference / open_)
                    btc -= sold
                    cash += sold * open_ * D('.9995') * D('.999')
            curve.append(float((cash + btc * close) * fx(day + DAY) * D('.999')))
            history.append(close)
        metrics, _ = daily_metrics(curve, 10000)
        report[method] = {'metrics': metrics, 'purchases': purchases, 'initial_cny': 10000,
                          'additional_capital_cny': 0, 'market': 'spot',
                          'method': 'Daily-open economic benchmark with fractional BTC, not finite-session/Lifecycle fills.',
                          'fees_each_buy': '.001', 'entry_slippage': '.0005',
                          'terminal_valuation': 'mark-to-market, no assumed terminal sale',
                          'continuous_mdd_verified': False, 'native_execution_verified': False}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spot', type=Path, required=True)
    parser.add_argument('--perp', type=Path, required=True)
    parser.add_argument('--spot-budgets', type=Path, required=True)
    parser.add_argument('--perp-budgets', type=Path, required=True)
    parser.add_argument('--spot-selected-budgets', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error('never overwrite an assessment')
    # Budget schema is validated after actual budget replays complete.
    inputs = {name: json.loads(getattr(args, name).read_text()) for name in ('spot', 'perp', 'spot_budgets', 'perp_budgets')}
    if args.spot_selected_budgets:
        inputs['spot_selected_budgets'] = json.loads(args.spot_selected_budgets.read_text())
    verify_joint_inputs(inputs)
    market_root = Path('/tmp/spotquant-market/klines')
    fx_path = Path('../starquant/data/usdcny_frankfurter.json')
    if hashlib.sha256(fx_path.read_bytes()).hexdigest() != inputs['spot']['fx_sha256']:
        raise ValueError('analysis FX bytes differ from frozen account input')
    market_digest = file_digest(market_root)
    if market_digest != inputs['spot']['market_sha256'] or market_digest != inputs['spot_budgets']['inputs']['market_sha256']:
        raise ValueError('analysis spot archive bytes differ from frozen account input')
    bars = load_daily(market_root, END_MS, require_through=END_MS)
    fx = PriorFX(fx_path)
    active = [b for b in bars if START_MS <= b[0] < END_MS]
    previous_price = active[0][1]
    btc_returns, cny_btc_returns = [], []
    previous_cny_price = previous_price * fx(START_MS)
    for row in active:
        btc_returns.append(float(row[4] / previous_price - 1))
        cny_price = row[4] * fx(row[0] + DAY)
        cny_btc_returns.append(float(cny_price / previous_cny_price - 1))
        previous_cny_price = cny_price
        previous_price = row[4]
    report = {'analysis_source': source_identity(), 'inputs': {n: hashlib.sha256(getattr(args, n).read_bytes()).hexdigest() for n in inputs},
              'input_sources': {'spot': inputs['spot']['source'], 'perp': inputs['perp']['inputs']['source']},
              'spot_selection': select_spot(inputs['spot']['results']), 'attribution': {},
              'passive_controls': passive_controls(bars, fx),
              'qualification': 'NOT_QUALIFIED', 'native_execution_verified': False,
              'limitations': ['All history already studied; regression intercept is descriptive and not proof of alpha.',
                              'Closing exposure is not maximum intraday leverage; joint MDD uses daily curves.',
                              'Nonzero BTC-balance day counts include retained dust; material-notional days are not strategy signal counts.',
                              'Separate BTC accounts do not provide asset diversification; no added capital or transfers.']}
    report['input_sources']['spot_account_sources'] = inputs['spot'].get('account_sources', {})
    for name in ('spot_budgets', 'perp_budgets', 'spot_selected_budgets'):
        if name in inputs:
            report['input_sources'][name] = inputs[name]['inputs']['source']
    if 'derivation' in inputs['perp']:
        report['perp_terminal_derivation'] = inputs['perp']['derivation']
        report['input_sources']['perp_derivation_source'] = inputs['perp']['inputs']['derivation_source']
    report['calendar_return_periods'] = {str(year): {
        'start_utc': datetime.fromtimestamp(min(b[0] for b in active if datetime.fromtimestamp(b[0] / 1000, timezone.utc).year == year) / 1000, timezone.utc).date().isoformat(),
        'end_exclusive_utc': datetime.fromtimestamp((max(b[0] for b in active if datetime.fromtimestamp(b[0] / 1000, timezone.utc).year == year) + DAY) / 1000, timezone.utc).date().isoformat(),
        'complete_calendar_year': year != 2026}
        for year in range(2020, 2027)}
    spot = inputs['spot']['results']['default-base']
    perp = inputs['perp']['results']['incumbent']['base']
    for name, row in (('spot', spot), ('perp', perp)):
        if not row.get('complete') or not row['audit']['passed']:
            raise ValueError('complete audited baseline required')
        curve = canonical(row, bars, fx, 10000, name)
        report['attribution'][name] = attribution(row, curve, btc_returns, cny_btc_returns, 10000, fx)
    report['candidate_attribution'] = {}
    for key, row in inputs['spot']['results'].items():
        if row.get('complete') and row['audit']['passed']:
            curve = canonical(row, bars, fx, 10000, 'spot')
            report['candidate_attribution']['spot/' + key] = attribution(row, curve, btc_returns, cny_btc_returns, 10000, fx)
    for candidate, scenarios in inputs['perp']['results'].items():
        for scenario, row in scenarios.items():
            if row.get('complete') and row['audit']['passed']:
                curve = canonical(row, bars, fx, 10000, 'perp')
                report['candidate_attribution']['perp/' + candidate + '/' + scenario] = attribution(row, curve, btc_returns, cny_btc_returns, 10000, fx)
    spot_curve, perp_curve = canonical(spot, bars, fx, 10000, 'spot'), canonical(perp, bars, fx, 10000, 'perp')
    _, spot_returns = daily_metrics([r['equity_cny'] for r in spot_curve], 10000)
    _, perp_returns = daily_metrics([r['equity_cny'] for r in perp_curve], 10000)
    report['daily_return_correlation_spot_perp'] = statistics.correlation(spot_returns, perp_returns)
    # Filled by fixed-account replay integration, deliberately no curve scaling.
    report['joint_fixed_capital'] = joint(inputs, bars, fx, btc_returns, cny_btc_returns)
    selected_spot = report['spot_selection']['selected_research_candidate']
    selected_perp = inputs['perp']['selection']['selected_research_candidate']
    report['perp_selection'] = inputs['perp']['selection']
    if selected_spot != 'default' or selected_perp != 'incumbent':
        spot_budgets, perp_budgets = verify_selected_budgets(inputs, selected_spot, selected_perp)
        report['joint_selected_fixed_capital'] = joint(inputs, bars, fx, btc_returns, cny_btc_returns,
            spot_candidate=selected_spot, perp_candidate=selected_perp,
            spot_budget_rows=spot_budgets, perp_budget_rows=perp_budgets)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')


def joint(inputs, bars, fx, btc_returns, cny_btc_returns, *, spot_candidate='default', perp_candidate='incumbent',
          spot_budget_rows=None, perp_budget_rows=None):
    rows = []
    for spot_budget in (0, 2500, 5000, 7500, 10000):
        perp_budget = 10000 - spot_budget
        curves = []
        for kind, budget in (('spot', spot_budget), ('perp', perp_budget)):
            if not budget:
                continue
            if budget == 10000:
                row = inputs[kind]['results'][spot_candidate + '-base'] if kind == 'spot' else inputs[kind]['results'][perp_candidate]['base']
            else:
                budget_rows = spot_budget_rows if kind == 'spot' else perp_budget_rows
                row = (budget_rows if budget_rows is not None else inputs[kind + '_budgets']['results'])[str(budget)]
            expected = spot_candidate if kind == 'spot' else perp_candidate
            if row.get('candidate') != expected:
                raise ValueError('joint account candidate differs: ' + kind + '/' + str(budget))
            if not row.get('complete') or not row['audit']['passed'] or int(D(str(row.get('initial_cny', 10000)))) != budget:
                raise ValueError('complete audited actual budget required: ' + kind + '/' + str(budget))
            curves.append(canonical(row, bars, fx, budget, kind))
        combined = [sum(curve[i]['equity_cny'] for curve in curves) for i in range(len(curves[0]))]
        metrics, combined_cny_returns = daily_metrics(combined, 10000)
        combined_usdt = [sum(curve[i]['equity_usdt'] for curve in curves) for i in range(len(curves[0]))]
        initial_usdt = float(D(10000) / fx(START_MS) * D('.999'))
        _, combined_usdt_returns = daily_metrics(combined_usdt, initial_usdt)
        rows.append({'spot_initial_cny': spot_budget, 'perp_initial_cny': perp_budget,
                     'spot_candidate': spot_candidate, 'perp_candidate': perp_candidate,
                     'metrics': metrics,
                     'usdt_btc_regression': regression(combined_usdt_returns, btc_returns),
                     'cny_btc_regression': regression(combined_cny_returns, cny_btc_returns),
                     'continuous_joint_mdd_verified': False,
                     'fixed_accounts_no_transfers': True,
                     'maximum_closing_gross_exposure_over_equity': max(
                         sum(abs(curve[i]['net_btc']) * curve[i]['price_usdt'] for curve in curves) /
                         sum(curve[i]['equity_usdt'] for curve in curves) for i in range(len(combined))),
                     'daily_equity_cny': combined})
    return rows


def verify_selected_budgets(inputs, spot_candidate, perp_candidate):
    reference = [r['start_ms'] for r in inputs['perp']['results']['incumbent']['base']['sessions']]
    spot_bundle = inputs['spot_budgets'] if spot_candidate == 'default' else inputs.get('spot_selected_budgets')
    if spot_bundle is None:
        raise ValueError('selected Spot candidate requires its actual three budget accounts')
    for key in ('fx_sha256', 'crowding_sha256', 'market_sha256', 'schedule_sha256'):
        if spot_bundle['inputs'].get(key) != inputs['spot_budgets']['inputs'].get(key):
            raise ValueError('selected Spot budget input differs: ' + key)
    perp_bundle = inputs['perp_budgets']
    if perp_candidate == 'incumbent':
        perp_rows = perp_bundle['results']
    elif perp_bundle.get('selected_candidate') == perp_candidate and 'selected_results' in perp_bundle:
        perp_rows = perp_bundle['selected_results']
    else:
        raise ValueError('selected perpetual candidate requires its actual three budget accounts')
    for kind, candidate, rows in (('spot', spot_candidate, spot_bundle['results']), ('perp', perp_candidate, perp_rows)):
        for budget in ('2500', '5000', '7500'):
            row = rows[budget]
            if row.get('candidate') != candidate or D(str(row['initial_cny'])) != D(budget):
                raise ValueError('selected budget candidate or capital differs: ' + kind + '/' + budget)
            if [r['start_ms'] for r in row['sessions']] != reference:
                raise ValueError('selected budget session schedule differs: ' + kind + '/' + budget)
    return spot_bundle['results'], perp_rows


def verify_joint_inputs(inputs):
    metadata = [inputs['spot'], inputs['perp']['inputs'], inputs['spot_budgets']['inputs'], inputs['perp_budgets']['inputs']]
    for key in ('fx_sha256', 'crowding_sha256'):
        if any(not item.get(key) for item in metadata) or len({item[key] for item in metadata}) != 1:
            raise ValueError('joint account ' + key + ' differs')
    full_perp, budget_perp = inputs['perp']['inputs'], inputs['perp_budgets']['inputs']
    for key in ('market_identity', 'protocol_sha256', 'schedule_sha256'):
        if not full_perp.get(key) or full_perp[key] != budget_perp.get(key):
            raise ValueError('perpetual budget input differs: ' + key)
    reference = [r['start_ms'] for r in inputs['perp']['results']['incumbent']['base']['sessions']]
    if len(reference) != 795 or reference != [r['start_ms'] for r in inputs['spot']['results']['default-base']['sessions']]:
        raise ValueError('complete baseline sessions differ')
    for kind in ('spot', 'perp'):
        for budget in ('2500', '5000', '7500'):
            row = inputs[kind + '_budgets']['results'][budget]
            expected = 'default' if kind == 'spot' else 'incumbent'
            if row.get('candidate') != expected:
                raise ValueError('baseline joint budget candidate differs: ' + kind + '/' + budget)
            if [r['start_ms'] for r in row['sessions']] != reference:
                raise ValueError('budget sessions differ: ' + kind + '/' + budget)


if __name__ == '__main__':
    main()
