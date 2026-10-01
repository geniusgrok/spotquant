#!/usr/bin/env python3
"""Independent read-only chronological audit of the proposed grouped-dust branch.

Inputs: complete_spot full/progress JSON or portfolio_spot budget JSON.
Output: one JSON proof object; no exchange calls or source/state mutation.
"""
import argparse
from decimal import Decimal as D, ROUND_DOWN
import hashlib
import json
from pathlib import Path

STEP = D('0.00001')
MIN_NOTIONAL = D('5')
POSITION_TOLERANCE = D('1e-24')
VERSION = 'isolated-grouped-dust-cumulative-v4-readback-guard'


def floor(value):
    return (value / STEP).to_integral_value(rounding=ROUND_DOWN) * STEP


def inspect_account(name, row):
    owners = {}
    for identity, payload, status, result in row['allocations']:
        payload, result = json.loads(payload), json.loads(result)
        if 'orderId' in result:
            order_id = int(result['orderId'])
            if order_id in owners:
                raise ValueError('duplicate native ownership identity')
            owners[order_id] = (payload, result)
    quantities = {30: D(0), 40: D(0), 50: D(0)}
    counters = {30: {}, 40: {}, 50: {}}
    seen, hits, counter_failures, pending_guards = set(), [], [], []
    sells = 0
    for trade in sorted(row['fills'], key=lambda r: (r['time'], r['id'])):
        if trade['id'] in seen:
            raise ValueError('duplicate fill identity')
        seen.add(trade['id'])
        order_id = int(trade['order_id'])
        owner, native = owners[order_id]
        group = owner['sleeves']
        weights = {int(k): D(v) for k, v in owner['weights'].items()}
        if set(weights) != set(group) or any(w <= 0 for w in weights.values()):
            raise ValueError('invalid owner weights')
        total = sum(weights.values(), D(0))
        gross = D(trade['qty'])
        base_fee = D(trade['commission']) if trade['commission_asset'] == 'BTC' else D(0)
        delta = gross - base_fee if trade['buyer'] else gross + base_fee
        applied, exact_group_close, start_matched = None, False, None
        if not trade['buyer']:
            sells += 1
            prior_values = [counters[w][order_id] for w in group if order_id in counters[w]]
            if prior_values:
                applied = prior_values[0]
                if any(v != applied for v in prior_values):
                    raise ValueError('inconsistent surviving applied counters')
            else:
                start_matched = all(abs(quantities[w] - weights[w]) <= POSITION_TOLERANCE for w in group)
                applied = D(0) if start_matched else None
                if not start_matched:
                    counter_failures.append({'order_id': order_id, 'fill_id': trade['id'], 'time_ms': trade['time']})
            if applied is not None:
                applied += gross
            intended = D(owner['order']['quantity'])
            rounded_group = sum((floor(v) for v in weights.values()), D(0))
            exact_group_close = (
                owner['order'].get('side') == 'SELL'
                and applied is not None and abs(applied - intended) <= POSITION_TOLERANCE
                and intended == rounded_group
            )
        given = D(0)
        for index, window in enumerate(group):
            amount = delta - given if index == len(group) - 1 else delta * weights[window] / total
            given += amount
            if trade['buyer']:
                quantities[window] += amount
                continue
            if amount > quantities[window] + STEP:
                raise ValueError('sell exceeds independently reconstructed ownership')
            remaining = max(D(0), quantities[window] - amount)
            quantities[window] = remaining
            counters[window][order_id] = applied
            if (exact_group_close and STEP <= remaining < STEP * len(group)
                    and remaining * D(trade['price']) < MIN_NOTIONAL):
                evidence = {
                    'fill_id': trade['id'], 'time_ms': trade['time'], 'order_id': order_id,
                    'sleeve': window, 'group': group, 'remaining_btc': str(remaining),
                    'remaining_value_usdt': str(remaining * D(trade['price'])),
                    'order_type': owner['order'].get('type'), 'intended_quantity': str(intended),
                    'sum_floor_weights': str(rounded_group), 'cumulative_applied_gross': str(applied),
                    'native_status': native.get('status'), 'native_executed_quantity': native.get('executedQty'),
                }
                if native.get('status') == 'FILLED' and native.get('executedQty') is not None and D(native['executedQty']) == intended:
                    hits.append(evidence)
                else:
                    pending_guards.append(evidence)
            if remaining == 0:
                counters[window] = {}
    discrepancy = sum(quantities.values(), D(0)) - D(row['btc'])
    if abs(discrepancy) > D('1e-8'):
        raise ValueError('reconstructed ownership differs from venue BTC')
    reported_positions = row['positions'] or {}
    for window, amount in quantities.items():
        stored = reported_positions.get(str(window))
        if abs(amount - D(stored['qty'] if stored else '0')) > D('1e-8'):
            raise ValueError('reconstructed ownership differs from recorded sleeve')
    complete_gate = bool(row.get('complete') and row.get('audit', {}).get('passed')
                         and not row.get('execution_unresolved_sessions')
                         and not row.get('pending_intents') and not row.get('policy_pending'))
    return {'account': name, 'account_result_sha256': hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(), 'fills_checked': len(seen), 'sell_fills_checked': sells,
            'native_owners_checked': len(owners), 'sessions': len(row['sessions']),
            'original_complete_audit_execution_gates': complete_gate,
            'extended_branch_hit_count': len(hits), 'hits': hits,
            'pending_readback_guard_count': len(pending_guards), 'pending_readback_guards': pending_guards,
            'additional_shape_count': len(hits) + len(pending_guards),
            'counter_initialization_failure_count': len(counter_failures),
            'counter_initialization_failures': counter_failures,
            'final_owned_btc': str(sum(quantities.values(), D(0))),
            'final_ownership_minus_venue_btc': str(discrepancy),
            'zero_hit_reuse_candidate': complete_gate and not hits and not pending_guards and not counter_failures,
            'caveat': 'Final implemented predicate and all other source changes need separate equivalence review.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', type=Path, nargs='+')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--implementation-root', type=Path)
    args = parser.parse_args()
    proof = {'verifier_version': VERSION,
             'verifier_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'predicate': 'Additional shape: STEP<=remaining<STEP*group, value<5, full rounded-group SELL, abs(cumulative gross applied-intended)<=1e-24. FILLED+native executed=intended classifies dust; otherwise pending-readback Unknown guard.',
             'input_bundles': []}
    if args.implementation_root:
        proof['implementation_reference'] = {name: hashlib.sha256((args.implementation_root / name).read_bytes()).hexdigest() for name in ('spotquant/execution.py', 'spotquant/session.py', 'spotquant/preview.py', 'spotquant/follow.py', 'research/complete_spot.py')}
    for path in args.inputs:
        blob = path.read_bytes()
        data = json.loads(blob)
        source = data.get('source', data.get('inputs', {}).get('source'))
        if not source or source.get('dirty'):
            raise ValueError('input lacks a clean recorded source')
        result = {'path': str(path.resolve()), 'sha256': hashlib.sha256(blob).hexdigest(),
                  'source': source, 'accounts': [inspect_account(k, r) for k, r in data['results'].items()]}
        proof['input_bundles'].append(result)
    body = json.dumps(proof, indent=2, allow_nan=False) + '\n'
    if args.out:
        with args.out.open('x') as stream:
            stream.write(body)
    else:
        print(body, end='')

if __name__ == '__main__':
    main()
