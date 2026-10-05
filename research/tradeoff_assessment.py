"""Describe benefits and costs of accepted, independently financed finite wallets.

No sessions run here. Conditions must come from the producer/specification, not
be inferred from similar-looking returns. No weighted score or promotion gate is
fitted to the results; original gates stay separate from this reconsideration.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import statistics

DAY = 86400000
CONDITIONS = (
    'market', 'valuation_currency', 'reporting_currency', 'initial_state',
    'market_data', 'fx', 'cost_model', 'execution_model', 'clock',
)
# Beta has no universal preferred direction and is deliberately absent.
PARETO_AXES = {
    'return_cny': 1, 'mdd_path_proxy': -1, 'daily_es5_loss_usdt': -1,
    'daily_volatility_annual_usdt': -1, 'longest_underwater_days_observed_usdt': -1,
    'fees_usdt': -1, 'funding_paid_usdt': -1,
}


def _number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite account value')
    return result


def _stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _equal_amount(a, b):
    return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal('0.00000001')


def load_wallet(path, *, expected_sha256, kind, conditions, original_gate=None):
    """Read only the accepted small wallet output; bind its exact bytes."""
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError('accepted wallet SHA256 changed: ' + str(path))
    return wallet_view(json.loads(raw), kind=kind, conditions=conditions,
                       source={'path': str(path), 'sha256': digest, 'accepted': True},
                       original_gate=original_gate)


def wallet_view(row, *, kind, source, conditions, original_gate=None):
    """Adapt an already accepted core_accounts wallet, with explicit conditions.

    In-memory callers own source verification; file callers should use
    load_wallet. An accepted ledger audit is a prerequisite, not recomputed here.
    initial_state must name cold cash, zero BTC and no pending intents. Strategies
    and risk budgets may differ; market, money, clocks and execution must match.
    """
    if kind not in ('spot', 'coin'):
        raise ValueError('unsupported wallet kind')
    missing = [key for key in CONDITIONS if key not in conditions or conditions[key] is None]
    if missing:
        raise ValueError('missing comparison conditions: ' + ', '.join(missing))
    if conditions['valuation_currency'] != 'USDT' or conditions['reporting_currency'] != 'CNY':
        raise ValueError('wallet fields require USDT valuation and CNY reporting')
    initial = conditions['initial_state']
    if (initial.get('kind') != 'cold_cash' or Decimal(str(initial.get('btc', 'NaN'))) != 0
            or initial.get('pending_intents') != []
            or not _equal_amount(initial.get('cash_usdt', 'NaN'), row['initial_usdt'])):
        raise ValueError('independent cold-cash initial state required')
    if not source.get('accepted') or not source.get('path') or len(source.get('sha256', '')) != 64:
        raise ValueError('accepted source binding required')
    if (row.get('complete_finite') is not True or row.get('audit', {}).get('passed') is not True
            or row.get('failure') or row.get('pending_intents') != []
            or row.get('known_path', kind == 'spot') is not True):
        raise ValueError('incomplete or unaudited wallet')
    sessions = row['sessions']
    starts = [s['start_ms'] for s in sessions]
    if (not starts or starts != sorted(set(starts)) or len(starts) != row['session_count']
            or len(starts) != row['registered_session_count']
            or any(s.get('pending_intents') or s.get('execution_unresolved') for s in sessions)):
        raise ValueError('incomplete or ambiguous session clocks')
    if any(word in str(error) for session in sessions for error in session.get('errors', [])
           for word in ('history must start', 'incompatible', 'risk identity mismatch')):
        raise ValueError('strategy integration failed despite completion flag')
    begin, end = map(_stamp, row['window'])
    if begin >= end or any(t < begin or t >= end for t in starts):
        raise ValueError('session outside account window')
    raw_daily = row['daily']
    raw_daily = raw_daily.values() if isinstance(raw_daily, dict) else raw_daily
    daily = {}
    opening = None
    for item in raw_daily:
        t = item.get('timestamp_ms', item.get('stamp_ms'))
        if not isinstance(t, int) or t % DAY or not begin <= t <= end:
            raise ValueError('daily ledger needs actual UTC midnight points inside window')
        if t in daily:
            raise ValueError('duplicate daily ledger timestamp')
        values = {currency: _number(item['equity_' + currency.lower()]) for currency in ('USDT', 'CNY')}
        if any(v <= 0 for v in values.values()):
            raise ValueError('nonpositive daily equity; insolvency needs explicit review')
        daily[t] = values
        if t == begin:
            opening = item
    # Never fill missing marks or intersect away an adverse/missing account day.
    if sorted(daily) != list(range(begin, end + DAY, DAY)):
        raise ValueError('full boundary-to-boundary daily ledger required')
    if not _equal_amount(daily[begin]['USDT'], row['initial_usdt']):
        raise ValueError('initial ledger differs from financed cash')
    if _number(row['initial_cny']) <= 0:
        raise ValueError('positive independently financed capital required')
    opening_qty = opening.get('quantity_btc', opening.get('btc'))
    opening_cash = opening.get('wallet_usdt', opening.get('cash_usdt'))
    if (opening_qty is not None and Decimal(str(opening_qty)) != 0
            or opening_cash is not None and not _equal_amount(opening_cash, row['initial_usdt'])):
        raise ValueError('opening ledger contradicts cold cash initial state')
    for currency in ('USDT', 'CNY'):
        if not _equal_amount(daily[end][currency], row['final_' + currency.lower()]):
            raise ValueError('terminal ledger differs from final account value')
    if not _equal_amount(Decimal(str(row['final_cny'])) / Decimal(str(row['initial_cny'])) - 1,
                         row['return_cny']):
        raise ValueError('reported return differs from financed capital')
    fees = row.get('fees', row.get('audit', {}).get('fees_usdt'))
    return {
        'account_id': row['case'], 'kind': kind, 'source': deepcopy(source),
        'conditions': deepcopy(conditions), 'window_ms': [begin, end],
        'session_starts_ms': starts, 'initial_cny': str(row['initial_cny']),
        'initial_usdt': str(row['initial_usdt']), 'daily': daily,
        'return_cny': _number(row['return_cny']), 'final_cny': _number(row['final_cny']),
        'mdd_path_proxy': _number(row['mdd']), 'price_model': row['price_model'],
        'fees_usdt': _number(fees) if fees is not None else None,
        'funding_paid_usdt': _number(row.get('funding', 0)) if kind == 'spot' or 'funding' in row else None,
        'fill_count': len(row.get('fills', row.get('trades', []))),
        'bounded_minutes': deepcopy(row.get('bounded_minutes', [])),
        'hindsight_bounded': row.get('hindsight_bounded', False),
        'original_gate': deepcopy(original_gate), 'producer_identity': deepcopy(row['identity']),
    }


def _comparable(baseline, candidate):
    if (baseline['account_id'] == candidate['account_id']
            or baseline['source']['path'] == candidate['source']['path']
            or baseline['source']['sha256'] == candidate['source']['sha256']):
        raise ValueError('distinct independently financed wallet sources required')
    for key in ('kind', 'conditions', 'window_ms', 'session_starts_ms', 'price_model'):
        if baseline[key] != candidate[key]:
            raise ValueError('incomparable ' + key)
    for key in ('initial_cny', 'initial_usdt'):
        if Decimal(baseline[key]) != Decimal(candidate[key]):
            raise ValueError('incomparable ' + key + '; never scale a curve')
    if baseline['daily'].keys() != candidate['daily'].keys():
        raise ValueError('incomparable daily clocks')
    first = min(baseline['daily'])
    if baseline['daily'][first] != candidate['daily'][first]:
        raise ValueError('incomparable initial ledger')
    for t, left in baseline['daily'].items():
        right = candidate['daily'][t]
        if not math.isclose(left['CNY'] / left['USDT'], right['CNY'] / right['USDT'], rel_tol=1e-12):
            raise ValueError('incomparable daily FX valuation')


def _beta(rows):
    if len(rows) < 3:
        return None
    account_mean = statistics.mean(a for a, b in rows)
    market_mean = statistics.mean(b for a, b in rows)
    variance = sum((b - market_mean) ** 2 for a, b in rows)
    return sum((a - account_mean) * (b - market_mean) for a, b in rows) / variance if variance else None


def _daily_metrics(points, initial):
    values = [v for t, v in points]
    returns = [b / a - 1 for a, b in zip(values, values[1:])]
    peak, peak_at = initial, points[0][0]
    mdd = longest = 0
    worst_peak = worst_trough = None
    for t, value in points:
        if value >= peak:
            peak, peak_at = value, t
        else:
            longest = max(longest, (t - peak_at) // DAY)
        drawdown = 1 - value / peak
        if drawdown > mdd:
            mdd, worst_peak, worst_trough = drawdown, (peak_at, peak), t
    recovered_at = next((t for t, v in points if worst_trough is not None
                         and t > worst_trough and v >= worst_peak[1]), None)
    tail = sorted(returns)[:max(1, math.ceil(len(returns) * .05))]
    return {
        'daily_mdd': mdd, 'worst_day_return': min(returns) if returns else None,
        'daily_es5_loss': -statistics.mean(tail) if tail else None,
        'tail_days': len(tail), 'daily_returns': len(returns),
        'daily_volatility_annual': statistics.stdev(returns) * math.sqrt(365.25) if len(returns) > 1 else None,
        'longest_underwater_days_observed': longest,
        'terminal_underwater_days_observed': (points[-1][0] - peak_at) // DAY if values[-1] < peak else 0,
        'worst_drawdown_recovery': {
            'peak_ms': worst_peak[0] if worst_peak else None, 'trough_ms': worst_trough,
            'recovered_ms': recovered_at, 'right_censored': worst_trough is not None and recovered_at is None,
            'peak_to_recovery_days': (recovered_at - worst_peak[0]) // DAY if recovered_at is not None else None,
            'trough_to_recovery_days': (recovered_at - worst_trough) // DAY if recovered_at is not None else None,
        },
    }, returns


def wallet_metrics(view, benchmark):
    points = sorted(view['daily'].items())
    diagnostics = {}
    for currency in ('USDT', 'CNY'):
        metric, returns = _daily_metrics([(t, v[currency]) for t, v in points],
                                         _number(view['initial_' + currency.lower()]))
        diagnostics[currency] = metric
        if currency == 'USDT':
            usdt_returns = returns
    pairs = []
    for i, ((a, _), (b, _)) in enumerate(zip(points, points[1:])):
        if a in benchmark and b in benchmark:
            before, after = _number(benchmark[a]), _number(benchmark[b])
            if before <= 0 or after <= 0:
                raise ValueError('nonpositive BTCUSDT benchmark')
            pairs.append((usdt_returns[i], after / before - 1))
    result = {key: view[key] for key in ('return_cny', 'final_cny', 'mdd_path_proxy',
              'fees_usdt', 'funding_paid_usdt', 'fill_count')}
    result.update(daily_by_currency=diagnostics,
                  daily_es5_loss_usdt=diagnostics['USDT']['daily_es5_loss'],
                  daily_volatility_annual_usdt=diagnostics['USDT']['daily_volatility_annual'],
                  longest_underwater_days_observed_usdt=diagnostics['USDT']['longest_underwater_days_observed'],
                  beta_btc_usdt=_beta(pairs), beta_up_days=_beta([(a, b) for a, b in pairs if b > 0]),
                  beta_down_days=_beta([(a, b) for a, b in pairs if b < 0]),
                  benchmark_matched_days=len(pairs), benchmark_missing_days=len(usdt_returns) - len(pairs),
                  benchmark_up_days=sum(b > 0 for a, b in pairs), benchmark_down_days=sum(b < 0 for a, b in pairs))
    return result


def _difference(baseline, candidate):
    delta = {key: candidate[key] - baseline[key] for key in baseline
             if isinstance(baseline[key], (int, float)) and isinstance(candidate[key], (int, float))}
    available = {key: direction for key, direction in PARETO_AXES.items()
                 if baseline[key] is not None and candidate[key] is not None}
    moves = [direction * (candidate[key] - baseline[key]) for key, direction in available.items()]
    relation = ('candidate_dominates_on_observed_axes' if all(v >= 0 for v in moves) and any(v > 0 for v in moves)
                else 'baseline_dominates_on_observed_axes' if all(v <= 0 for v in moves) and any(v < 0 for v in moves)
                else 'equal_on_observed_axes' if all(v == 0 for v in moves) else 'tradeoff_no_dominance')
    return {'candidate_minus_baseline': delta, 'pareto': {
        'relation': relation, 'axes': available, 'missing_axes': sorted(set(PARETO_AXES) - set(available)),
        'meaning': 'Descriptive partial order only; correlated axes are not votes or an adoption score. '
                   'Observed underwater duration is censored at the shared window end, not proven recovery speed.'}}


def assess_comparison(baseline, candidate, benchmark, *, purpose, accepted_costs, risk_control=None):
    """Return facts and gaps; adoption always requires an explicit net-benefit judgment."""
    if not purpose or not accepted_costs:
        raise ValueError('record intended purpose and acceptable tradeoffs before assessment')
    _comparable(baseline, candidate)
    metrics = {'baseline': wallet_metrics(baseline, benchmark), 'candidate': wallet_metrics(candidate, benchmark)}
    sources = {name: deepcopy(view['source']) for name, view in (('baseline', baseline), ('candidate', candidate))}
    gaps = ['Finite reused historical windows do not establish prospective alpha or cross-regime stability.',
            'Path MDD is the recorded price-model proxy; daily tail/recovery observations are not continuous/native risk.',
            'No independent fee/slippage stress wallet supplied; observed costs are not a stressed execution path.',
            'Native execution and incremental maintenance burden require separate evidence.']
    control = None
    if risk_control is None:
        gaps.append('No independently financed simple risk-budget control; lower beta/volatility alone does not establish added skill.')
    else:
        _comparable(baseline, risk_control)
        _comparable(candidate, risk_control)
        metrics['risk_control'] = wallet_metrics(risk_control, benchmark)
        sources['risk_control'] = deepcopy(risk_control['source'])
        control = {'baseline_to_control': _difference(metrics['baseline'], metrics['risk_control']),
                   'control_to_candidate': _difference(metrics['risk_control'], metrics['candidate']),
                   'interpretation': 'Actual independent control wallet; risk need not match exactly. Beta is descriptive, not a target fitted to outcomes.'}
    if any(m['benchmark_missing_days'] for m in metrics.values()):
        gaps.append('BTCUSDT beta uses only explicitly matched daily intervals; incomplete benchmark coverage is disclosed.')
    if any(m['beta_down_days'] is None or m['beta_up_days'] is None for m in metrics.values()):
        gaps.append('At least one directional beta is unidentified or has fewer than three daily observations.')
    if any(v['hindsight_bounded'] or v['bounded_minutes'] for v in (baseline, candidate)):
        gaps.append('Original wallet reports bounded/hindsight-bounded path intervals; preserved, not upgraded to exact risk.')
    return {
        'assessment_type': 'post_result_user_authorized_benefit_harm_reconsideration',
        'status': 'EXPLICIT_NET_BENEFIT_JUDGMENT_REQUIRED', 'adopted': False,
        'purpose': purpose, 'accepted_costs': accepted_costs,
        'window_ms': baseline['window_ms'], 'conditions': deepcopy(baseline['conditions']),
        'sources': sources, 'metrics': metrics, 'comparison': _difference(metrics['baseline'], metrics['candidate']),
        'simple_risk_control': control, 'evidence_gaps': gaps,
        'original_gates': {'baseline': deepcopy(baseline['original_gate']), 'candidate': deepcopy(candidate['original_gate'])},
        'interpretation': 'Lower return or a losing segment is a cost, never an automatic rejection. '
                          'Judge magnitude, relevance and uncertainty; no weighted score or count of winning metrics. '
                          'CNY total return includes original FX conversion costs; daily returns use actual adjacent ledger values. '
                          'USDT beta is against BTCUSDT at identical UTC boundaries and is not validated alpha.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True,
                        help='JSON: baseline/candidate[/risk_control] load_wallet arguments, benchmark timestamp map, purpose, accepted_costs')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError('preserve existing assessment')
    manifest = json.loads(args.manifest.read_text())
    views = {name: load_wallet(**manifest[name]) for name in ('baseline', 'candidate', 'risk_control') if name in manifest}
    benchmark = {int(t): value for t, value in manifest['benchmark'].items()}
    report = assess_comparison(**views, benchmark=benchmark, purpose=manifest['purpose'], accepted_costs=manifest['accepted_costs'])
    report['manifest_sha256'] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
