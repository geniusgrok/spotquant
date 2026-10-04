"""Reuse accepted accounts for missing-input and execution attribution; no replay."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
import statistics

from research.complete_spot import PriorFX
from research.complete_assessment import daily_metrics, regression
from research.edge_features import FeatureBook
from research.market import load_daily
from research.rebuild import START_MS, END_MS, source_identity
from research.upgrade_spot import screen
from spotquant.model import DAY
from spotquant.types import serial

RAW = Path('/workspace/scratch/btc-alpha-beta-edge-20261003/recovery1/unscaled')
INPUTS = {
    'spot-crowding-interaction-base.json.gz': '4c937ecf80f60d957486a752562c8ab8dfee4c06fa2e5b38b135cfb24ec38872',
    'perp-registered-16.json.gz': 'a071749cb6498a0e9612241bca23b57f8de9bae002b93fe189b7e1a3777a416d',
}
ACCEPTED_REPORT_SHA256 = '16ee432bd45c9783f9f38beb19aa7fef21ca3cc6305bee29469a371d388b2625'


def accepted(name):
    raw = (RAW/name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != INPUTS[name]:
        raise ValueError('accepted account bytes changed')
    return json.loads(gzip.decompress(raw))


def missing_attribution(row):
    causes, blocks, half, unchanged = Counter(), [], 0, 0
    for event in row['opportunity_ledger']:
        for item in event.get('diagnostics', []):
            if item.get('mechanism') != 'crowding-interaction':
                continue
            inputs, momentum = item['inputs'], item['momentum']
            # Original accepted journals predate the explicit scale field.
            # Reconstruct the registered conjunction from its original causal inputs.
            missing = (any(v.get('value') is None for v in inputs) or
                       not momentum['causal_completed'] or momentum['prior_close'] is None)
            values = {v['name']: v.get('value') for v in inputs}
            half_condition = (not missing and D(values['funding']) > D('.0003') and
                              D(values['basis']) > D('.01') and
                              D(momentum['current_close']) <= D(momentum['prior_close']))
            factor = D(0) if missing else D('.5') if half_condition else D(1)
            if factor == 0:
                reasons = [str(v.get('cause')) for v in item['inputs'] if v.get('cause')]
                causes.update(reasons)
                blocks.append(dict(decision_ms=event['decision_ms'],
                    completed_bar_ms=event['completed_bar_ms'], causes=reasons))
            elif factor == D('.5'):
                half += 1
            else:
                unchanged += 1
    return dict(blocked_proposals=len(blocks), unique_blocked_completed_days=len({v['completed_bar_ms'] for v in blocks}),
                causes=causes, actual_halving_proposals=half, unchanged_proposals=unchanged,
                blocked=blocks,
                conclusion='Observed gain comes from missing-input exclusions; halving has no historical treatment sample. No fill or profitability counterfactual is invented.')


def execution_attribution(row):
    journal = row['opportunity_ledger']
    orders, fills = {}, defaultdict(lambda: D(0))
    unlinked, regimes = 0, Counter()
    constraints = Counter()
    for item in journal:
        event = item['event']
        if event in ('entry_sizing', 'topup_sizing'):
            constraints[event+':'+str(item['constraint'])] += 1
        if event == 'decision' and D(item['quantity_before']) != 0:
            owner = item.get('opportunity')
            regimes['primary' if owner and owner > 0 else 'macro' if owner and owner < 0 else 'unknown'] += 1
        if event == 'write_attempt' and item['method'] == 'POST':
            p = item['payload']
            if p.get('side') == 'BUY' and p.get('timeInForce') == 'IOC':
                if item['identity'] in orders and orders[item['identity']] != p:
                    raise ValueError('changed IOC identity')
                orders[item['identity']] = p
        if event == 'fill' and item['trade']['side'] == 'BUY':
            key = item.get('client_order_id')
            if key:
                fills[key] += D(item['trade']['qty'])
            else:
                unlinked += 1
    outcomes = []
    for key, order in orders.items():
        requested, filled = D(order['quantity']), fills[key]
        if filled > requested:
            raise ValueError('fills exceed IOC request')
        outcomes.append(dict(client_id=key, requested_btc=requested, filled_btc=filled,
                             fill_fraction=filled/requested if requested else None))
    return dict(entry_and_topup_constraints=constraints, owned_decision_regimes=regimes,
        ioc_order_count=len(outcomes), zero_fill_ioc_orders=sum(v['filled_btc'] == 0 for v in outcomes),
        partial_fill_ioc_orders=sum(0 < v['filled_btc'] < v['requested_btc'] for v in outcomes),
        unlinked_buy_fill_rows=unlinked, ioc_outcomes=outcomes,
        joint_budget=dict(status='ALREADY_COVERED', concurrent_component_positions=0,
            source='coinquant/campaign.py: Campaign.active, select_macro and position_campaign',
            conclusion='One campaign owns the single net position; macro ownership has priority, primary ownership clears macro geometry. An overlap-specific extra budget has no treatment opportunity.'),
        execution_change=dict(status='NO_JUSTIFIED_FIX',
            conclusion='Existing IOC limits, committed targets and stop-budget constraints are visible. Incomplete fills are evidence of an execution tradeoff, not a proven defect. Preserve current execution until a new policy shows net benefit.'))


def proxy_metrics(row, bars, fx):
    price = {b[0]: float(b[4]) for b in bars}
    daily = [(int(day), float(value)/float(fx(day+DAY-1))/.999) for day, value in row['daily_cny']]
    initial = float(D(10000)/fx(START_MS)*D('.999'))
    values = [v for _, v in daily]
    metrics, returns = daily_metrics(values, initial)
    metrics['final_usdt'] = metrics.pop('final_cny')
    metrics['usdt_cagr'] = metrics.pop('cagr')
    previous = price[START_MS-DAY]
    market = []
    for day, _ in daily:
        market.append(price[day]/previous-1)
        previous = price[day]
    metrics.update(regression(returns, market))
    for label, sign in [('up_capture', 1), ('down_capture', -1)]:
        indices = [i for i, v in enumerate(market) if sign*v > 0]
        denominator = sum(market[i] for i in indices)
        metrics[label] = sum(returns[i] for i in indices)/denominator if denominator else None
    metrics.update(cny_cagr=row['cagr'], continuous_ohlc_proxy_mdd=row['mdd'],
                   closed_sleeve_trades=len(row['trades']))
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--features', type=Path, required=True)
    parser.add_argument('--accepted-attribution', type=Path,
                        help='Accepted final report: attribute all crowding rows only, without any screening/replay')
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error('exclusive output required')
    if args.accepted_attribution:
        raw_report = args.accepted_attribution.read_bytes()
        if hashlib.sha256(raw_report).hexdigest() != ACCEPTED_REPORT_SHA256:
            raise ValueError('unregistered accepted final report')
        report = json.loads(raw_report)
        if report['status'] != 'complete_reviewed':
            raise ValueError('accepted final financial report required')
        rows = {}
        for key, item in report['accounts'].items():
            if item['kind'] != 'spot' or item['candidate'] != 'crowding-interaction':
                continue
            raw = Path(item['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != item['raw_sha256'] or item['status'] != 'complete':
                raise ValueError('accepted source row changed')
            row = next(iter(json.loads(gzip.decompress(raw))['results'].values()))
            rows[key] = dict(path=item['path'], raw_sha256=item['raw_sha256'],
                             attribution=missing_attribution(row))
        aggregate = Counter()
        for item in rows.values():
            aggregate.update(item['attribution']['causes'])
        result = dict(source=source_identity(), accounts=rows,
            accepted_report=str(args.accepted_attribution),
            accepted_report_sha256=ACCEPTED_REPORT_SHA256,
            totals={name:sum(v['attribution'][name] for v in rows.values()) for name in
                    ('blocked_proposals','actual_halving_proposals','unchanged_proposals')},
            causes=dict(aggregate),
            unique_blocked_completed_days=len({r['completed_bar_ms'] for v in rows.values()
                for r in v['attribution']['blocked']}), financial_replay=False,
            limitation='Counts span different real accounts and repeated poll proposals, not independent trades.')
        with args.out.open('x') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
        print(json.dumps({k:result[k] for k in ('totals','causes','unique_blocked_completed_days')}))
        return
    spot = next(iter(accepted('spot-crowding-interaction-base.json.gz')['results'].values()))
    missing = missing_attribution(spot)
    del spot
    perp = accepted('perp-registered-16.json.gz')['results']['incumbent']['base']
    execution = execution_attribution(perp)
    del perp
    bars = load_daily(Path('/tmp/spotquant-market/klines'), END_MS, require_through=END_MS)
    fx = PriorFX(Path('../starquant/data/usdcny_frankfurter.json'))
    features = FeatureBook(args.features)
    rows = {}
    for name in ('baseline', 'trend-reentry', 'slow-participation'):
        row = screen(name, bars, fx, features)
        metrics = proxy_metrics(row, bars, fx)
        rows[name] = metrics
    base = rows['baseline']
    for name in ('trend-reentry', 'slow-participation'):
        r = rows[name]
        r['survives_screen'] = bool(r['cny_cagr'] >= base['cny_cagr']+.01 and
            D(r['continuous_ohlc_proxy_mdd']) <= D(base['continuous_ohlc_proxy_mdd'])+D('.02'))
    result = dict(format='btc-upgrade-readonly-and-spot-screen-v1', source=source_identity(),
        accepted_inputs=INPUTS, missing_input_attribution=missing, execution_attribution=execution,
        spot_screen=rows, full_replays_run=0, native_cases=0, prospective_alpha_proven=False,
        limitations=['Daily screening trades at proxy daily opens, without finite-session latency, partial fills or native order rules.',
                    'Screening metrics are not current-runtime financial results. Capture uses arithmetic conditional daily sums.',
                    'Known history remains contaminated. Source ownership exclusivity is established by current production call paths.'])
    with args.out.open('x') as stream:
        json.dump(serial(result), stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({'output':str(args.out), 'spot_screen':serial(rows),
                      'missing_causes':dict(missing['causes']), 'joint_budget':'ALREADY_COVERED'}))


if __name__ == '__main__':
    main()
