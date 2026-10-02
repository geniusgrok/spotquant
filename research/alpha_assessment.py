"""Strict, descriptive assessment of the registered BTC alpha/beta accounts.

No execution, curve scaling, parameter search, or public-data download occurs here.
"""
import argparse
import bisect
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal as D
import csv
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import tarfile
import tempfile
import time

from research.complete_assessment import canonical, attribution, regression, daily_metrics, passive_controls
from research.complete_spot import PriorFX
from research.market import load_daily, file_digest
from research.rebuild import START_MS, END_MS, source_identity
from spotquant.model import DAY

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / 'research/alpha_beta_spec.json'
PROTOCOL_PATH = ROOT / 'research/alpha-beta-PROTOCOL.md'
SPEC = json.loads(SPEC_PATH.read_text())
CUTOFF = 1640995200000
APPROVED_BASELINES = {
    'spot': 'cf075b86ba5df590e078a7c626c53cb20937be955cfb6ad8d41eae19b78645e8',
    'perp': '15dd7bfc242d52bf692663179cd1e3867418f8b55a4cad0894d253a8b296f00a',
}
COIN_ORIGINAL_ACCOUNTS_SHA = '55aa8ba0d8e54d572907a5d57cc557face20a0ce6666692d526df37387585423'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    require(not isinstance(value, bool), 'boolean monetary value')
    result = float(value)
    require(math.isfinite(result), 'nonfinite value')
    return result


def hash_value(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def read_json(path):
    """Hash the original bytes; do not keep a second uncompressed byte buffer."""
    path = Path(path)
    digest = sha(path)
    opener = gzip.open if path.suffix == '.gz' else open
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    with opener(path, 'rt', encoding='utf-8') as stream:
        body = json.load(stream, object_pairs_hook=pairs,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    require(sha(path) == digest, 'input changed while reading')
    return body, digest


def write_new(path, body):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(body, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def verify_source(source, kind):
    require(isinstance(source, dict) and source.get('dirty') is False, 'clean measured source required')
    head = source.get('git_head', '')
    require(len(head) == 40 and all(c in '0123456789abcdef' for c in head), 'invalid source commit')
    repo = ROOT if kind == 'spot' else ROOT.parent / 'coinquant'
    archive = subprocess.run(['git', 'archive', head, 'research', kind.replace('perp', 'coin') + 'quant'],
                             cwd=repo, check=True, capture_output=True).stdout
    digest = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        for member in sorted(tree.getmembers(), key=lambda m: m.name):
            if member.isfile() and member.name.endswith('.py'):
                digest.update(member.name.encode() + b'\0' + tree.extractfile(member).read() + b'\0')
    require(digest.hexdigest() == source.get('python_sources_sha256'), 'source digest differs from committed tree')


def environment(schedule_path, fx_path, market_root):
    require(sha(SPEC_PATH) == '8228013f4ac41affb65162c1cabad8f51b5ef32b9607f62231a4776168a337d0', 'registered spec bytes changed')
    require(sha(PROTOCOL_PATH) == '4ae09ae0bc9224f8028ddcc1373dc1bbc7251a70be2945e5655f5e5fa12eb27e', 'registered protocol bytes changed')
    schedule, _ = read_json(schedule_path)
    starts = schedule['primary']['starts_ms']
    require(len(starts) == 795 and starts == sorted(set(starts)), 'registered 795 starts required')
    require(schedule['session_seconds'] == 300 and schedule['poll_seconds'] == 5, 'session economics differ')
    require(schedule['primary']['sha256'] == hashlib.sha256(json.dumps(starts, separators=(',', ':')).encode()).hexdigest() == 'f8fb73bebf142ddcc3ed4a3e6b12b4dd7abed1e27bcd8a4ff1c93aec4fe0b32a', 'schedule primary checksum differs')
    return {'starts': starts, 'schedule_sha256': sha(schedule_path),
            'primary_sha256': schedule['primary']['sha256'], 'fx_sha256': sha(fx_path),
            'market_sha256': file_digest(market_root), 'spec_sha256': sha(SPEC_PATH),
            'protocol_sha256': sha(PROTOCOL_PATH)}


def metadata(bundle, kind, env, *, capital='10000', offset=0):
    meta = bundle if kind == 'spot' else bundle['inputs']
    for key in ('spec_sha256', 'protocol_sha256', 'fx_sha256'):
        require(meta.get(key) == env[key], kind + ' input mismatch: ' + key)
    verify_source(meta['source'], kind)
    if kind == 'spot':
        require(bundle.get('format') == 1, 'Spot format')
        for key in ('schedule_sha256', 'market_sha256'):
            require(meta.get(key) == env[key], 'Spot input mismatch: ' + key)
    else:
        require(meta.get('measured_source') == meta['source'], 'conflicting Coin source identities')
        require(meta.get('schedule_sha256') == env['primary_sha256'] == meta.get('original_schedule_sha256'), 'Coin schedule mismatch')
        require(meta.get('actual_starts_sha256') == checksum([s + offset for s in env['starts']]), 'actual start hash differs')
        require(D(meta['initial_cny']) == D(capital) and meta['start_offset_ms'] == offset, 'capital/offset mismatch')
        require(meta.get('crowding_source') == 'not used by alpha/beta mechanisms' and
                meta.get('crowding_sha256') == checksum(None), 'undeclared crowding input')
        require(isinstance(meta.get('market_identity'), dict) and meta['market_identity'], 'missing Coin market identity')
        for name in ('loaded_minute_files', 'loaded_print_files'):
            require(isinstance(meta.get(name), dict) and all(hash_value(v) for v in meta[name].values()), 'missing market file hashes')
        conditions = bundle['conditions']
        require(D(conditions['conversion_each_way']) == D('.001') and
                conditions['terminal_funding_exclusive_ms'] == END_MS, 'Coin economic boundary differs')
    return {k: v for k, v in meta.items() if k != 'results'}


def equivalent_inputs(left, right, kind):
    keys = ['spec_sha256', 'protocol_sha256', 'fx_sha256', 'schedule_sha256']
    keys += ['market_sha256'] if kind == 'spot' else ['market_identity', 'original_meter_protocol_sha256', 'crowding_sha256']
    for key in keys:
        require(left.get(key) == right.get(key), 'account inputs differ: ' + key)
    require(left['source']['python_sources_sha256'] == right['source']['python_sources_sha256'], 'account source differs')
    if kind == 'perp':
        for key in ('loaded_minute_files', 'loaded_print_files'):
            a, b = left[key], right[key]
            require(all(a[n] == b[n] for n in a.keys() & b.keys()), 'consumed file bytes differ')


def rows(bundle, kind):
    if kind == 'spot':
        for key, row in bundle['results'].items():
            require(key == row['candidate'] + '-' + row['scenario'], 'Spot candidate/key mismatch')
            yield row['candidate'], row['scenario'], row
    else:
        require(set(bundle['inputs']['candidates']) == set(bundle['results']), 'Coin candidate manifest differs')
        for candidate, scenarios in bundle['results'].items():
            for scenario, row in scenarios.items():
                require(row.get('candidate', candidate) == candidate and row.get('scenario', scenario) == scenario, 'Coin row identity differs')
                yield candidate, scenario, row


def verify_spot_timing(row):
    """Check original dispatch semantics, not an elapsed-time tolerance.

    Reads check the stop predicate before their 200ms advance; writes do so
    before their 1000ms advance and record both timestamps. Integer-ms starts
    therefore permit an already dispatched read to finish at deadline+199.
    Any later finish must be the recorded receipt of a pre-deadline write.
    """
    sessions = row['sessions']
    starts = [session['start_ms'] for session in sessions]
    receipts = {start: [] for start in starts}
    prior_received = -1
    for event in row['client_events']:
        sent, received = event['sent_ms'], event['received_ms']
        require(type(sent) is int and type(received) is int and sent >= prior_received,
                'client trace timestamps/order invalid')
        index = bisect.bisect_right(starts, sent) - 1
        require(index >= 0 and sent < starts[index] + 300000, 'client dispatch outside session deadline')
        require(event['method'] in ('POST', 'DELETE') and received == sent + 1000,
                'client write latency differs from original meter')
        require(received <= sessions[index]['ended_ms'], 'client receipt after recorded session end')
        receipts[starts[index]].append(received)
        prior_received = received
    for session in sessions:
        start, end = session['start_ms'], session['ended_ms']
        require(type(start) is int and type(end) is int and end >= start + 300000,
                'session ended before original deadline')
        require(number(session['elapsed_seconds']) == max(0, end / 1000 - start / 1000),
                'elapsed clock differs from recorded integer-ms clock')
        require(session['archive_verified'] is True and hash_value(session['archive_backup_sha256']),
                'archive/session proof failed')
        require(session['status'] in ('offline_execution', 'demo_execution') or
                (session['errors'] and all('session deadline' in e['reason'] for e in session['errors'])),
                'session status is not original resolved/deadline-only path')
        if end >= start + 300200:
            require(end in receipts[start] and end < start + 301000,
                    'session overrun lacks an actual pre-deadline write receipt')


def row_validity(row, kind, starts, capital, scenario='base'):
    if kind == 'spot' and scenario == 'outage':
        starts = [v for v in starts if not 1583020800000 <= v < 1584835200000]
    require(isinstance(row.get('complete'), bool) and isinstance(row.get('audit', {}).get('passed'), bool), 'missing completion/audit schema')
    require(D(row['initial_cny']) == D(capital), 'row capital mismatch')
    actual = [s['start_ms'] for s in row['sessions']]
    require(actual == starts[:len(actual)], 'row starts mismatch')
    reasons = []
    if not row['complete']:
        reasons.append('measurement_incomplete')
    else:
        require(actual == starts, 'complete account lacks all starts')
        for session in row['sessions']:
            require(not session['execution_unresolved'], 'complete account has unresolved execution')
            if kind == 'perp':
                require(session['cleanup'] == 'verified', 'complete Coin account lacks verified cleanup')
        if kind == 'spot':
            verify_spot_timing(row)
    if not row['audit']['passed']:
        reasons.append('audit_failed')
    if kind == 'perp':
        if not row['known_path'] or row['failure'] or row['execution_unresolved']:
            reasons.append('unknown_or_failed_execution')
    elif row['execution_unresolved_sessions'] or row['policy_pending'] or row['pending_intents']:
        reasons.append('unsettled_execution')
    if not reasons:
        number(row['cagr'])
        require(0 <= number(row['mdd']) <= 1, 'invalid continuous MDD')
        require(number(row['final_cny']) > 0 and number(row['final_usdt']) > 0, 'invalid terminal money')
    return reasons


def safe_regression(account, market):
    if len(account) < 3 or len(set(market)) < 2:
        return {'identifiable': False, 'beta_btc': None, 'reason': 'insufficient observations or zero market variance'}
    return dict(regression(account, market), identifiable=True)


def risk_stats(account, market):
    return {'volatility': statistics.stdev(account) * math.sqrt(365.25) if len(account) > 1 else None,
            'beta': safe_regression(account, market)['beta_btc'], 'days': len(account)}


def returns_for(curve, initial_usdt):
    return daily_metrics([r['equity_usdt'] for r in curve], initial_usdt)[1]


def calibrate(curve, baseline, market_returns, initial_usdt):
    """Slice BEFORE any statistic; post-cutoff values cannot affect the scale."""
    count = sum(r['day_ms'] < CUTOFF for r in curve)
    require(count == sum(r['day_ms'] < CUTOFF for r in baseline) and count >= 3, 'training coverage differs')
    candidate = risk_stats(returns_for(curve[:count], initial_usdt), market_returns[:count])
    reference = risk_stats(returns_for(baseline[:count], initial_usdt), market_returns[:count])
    scales = [1.0]
    if candidate['volatility'] > 0:
        scales.append(reference['volatility'] / candidate['volatility'])
    if candidate['beta'] is not None and reference['beta'] is not None:
        if candidate['beta'] > reference['beta'] and candidate['beta'] > 0:
            scales.append(max(.01, reference['beta']) / max(.01, candidate['beta']))
    return str(min(scales)), {'candidate': candidate, 'baseline': reference}


def achieved_match(curve, baseline, market_returns, initial_usdt):
    start = next(i for i, row in enumerate(curve) if row['day_ms'] >= CUTOFF)
    candidate = risk_stats(returns_for(curve, initial_usdt)[start:], market_returns[start:])
    reference = risk_stats(returns_for(baseline, initial_usdt)[start:], market_returns[start:])
    identifiable = all(v['beta'] is not None and v['volatility'] is not None for v in (candidate, reference))
    matched = identifiable and candidate['volatility'] <= reference['volatility'] * 1.05 and candidate['beta'] <= reference['beta'] + .02
    gain = curve[-1]['equity_usdt'] / curve[start - 1]['equity_usdt'] - baseline[-1]['equity_usdt'] / baseline[start - 1]['equity_usdt']
    return {'achieved_match': bool(matched), 'candidate': candidate, 'baseline': reference,
            'validation_total_usdt_return_gain': gain,
            'classification': ('conditional_historical_matched_return_evidence' if gain > 0 else 'no_positive_matched_return_evidence')
                              if matched else 'risk_match_failed_or_unidentifiable',
            'prospective_alpha_proven': False}


def evidence_fingerprints(row, kind):
    """Separate money, fills, curves, ownership and operating equality.

    Only client-ID spelling and separately verified nondeterministic archive
    hashes normalize. Numeric order/trade IDs, every timestamp/deadline, all
    sizes/prices/cash and references between ownership records stay intact.
    The first dispatch defines each client ID's stable relational identity.
    """
    ids = {}
    if kind == 'spot':
        for event in row['client_events']:
            ids.setdefault(event['client_id'], 'synthetic-client-' + str(len(ids)))
        for identity, _, _, _ in row['allocations']:
            ids.setdefault(identity, 'synthetic-client-' + str(len(ids)))
    def normalize(value):
        if isinstance(value, dict):
            return {ids.get(k, k): normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return ids.get(value, value) if isinstance(value, str) else value
    financial = ('initial_cny', 'final_cny', 'final_usdt', 'cagr', 'mdd', 'audit') + (
        ('cash_usdt', 'btc') if kind == 'spot' else ('fees', 'funding', 'position', 'final_mark', 'mdd_close'))
    fill_fields = ('fills',) if kind == 'spot' else ('trades', 'funding_ledger')
    curve_fields = ('daily',) if kind == 'spot' else ('daily', 'daily_cny')
    ownership = ('positions', 'allocations', 'pending_intents') if kind == 'spot' else ('position',)
    operating = ('sessions', 'session_error_count', 'execution_unresolved_sessions', 'policy_pending',
                 'filters', 'client_events') if kind == 'spot' else (
                 'sessions', 'known_path', 'unknown_from', 'hindsight_bounded', 'bounded_minutes',
                 'mdd_envelope_at', 'mdd_close_at', 'funnel', 'failure', 'feature_coverage', 'execution_unresolved')
    groups = {}
    for name, fields in (('financial', financial), ('fills', fill_fields), ('daily', curve_fields),
                         ('ownership', ownership), ('operating', operating)):
        require(all(k in row for k in fields), 'missing baseline evidence fields: ' + name)
        body = {k: row[k] for k in fields}
        if 'allocations' in body:
            body['allocations'] = [[identity, json.loads(payload), status, json.loads(result)]
                                   for identity, payload, status, result in body['allocations']]
        if 'sessions' in body and kind == 'spot':
            require(all(s['archive_verified'] is True and hash_value(s['archive_backup_sha256'])
                        for s in body['sessions']), 'archive proof missing before normalization')
            body['sessions'] = [{k: v for k, v in session.items() if k != 'archive_backup_sha256'}
                                for session in body['sessions']]
        groups[name] = checksum(normalize(body))
    # Include all other original row fields, excluding explicit identity/journal
    # additions. This prevents a future producer field being silently omitted.
    covered = set(financial + fill_fields + curve_fields + ownership + operating)
    additions = {'candidate', 'scenario', 'original_row_sha256', 'opportunity_ledger',
                 'research_identity', 'risk_calibration', 'subpools'}
    groups['remaining_original_fields'] = checksum(normalize({k: v for k, v in row.items() if k not in covered | additions}))
    return groups


def monetary_fingerprint(row, kind):
    evidence = evidence_fingerprints(row, kind)
    return checksum({k: evidence[k] for k in ('financial', 'fills', 'daily')})


def diagnose(events, bars, kind):
    counts, reasons, exits, opportunities = Counter(), Counter(), Counter(), {}
    sizing, idle, ages, price_gaps = {}, [], [], []
    fills_by_opportunity, price_gap_links = {}, {}

    def quantities(into, identity, values):
        key = json.dumps(identity, sort_keys=True)
        record = into.setdefault(key, {'identity': identity, 'observations': 0, 'values': {}})
        record['observations'] += 1
        for name, raw in values.items():
            if raw is None:
                continue
            number(raw)
            value = D(str(raw))
            field = record['values'].setdefault(name, {'n': 0, 'sum': '0', 'min': str(value), 'max': str(value)})
            field.update(n=field['n'] + 1, sum=str(D(field['sum']) + value),
                         min=str(min(D(field['min']), value)), max=str(max(D(field['max']), value)))
    actionability = Counter()
    filled = Counter()
    closes = {b[0] + DAY: float(b[4]) for b in bars if START_MS <= b[0] < END_MS}
    for event in events:
        counts[event['event']] += 1
        if event['event'] == 'fill':
            fill = event.get('trade', event)
            filled['count'] += 1
            filled['quantity_btc'] += number(fill.get('qty', '0'))
            quantities(fills_by_opportunity,
                       {'opportunity': event.get('opportunity', event.get('signal_ms')),
                        'order_id': fill.get('order_id', fill.get('orderId')),
                        'side': fill.get('side', 'BUY' if fill.get('buyer') else 'SELL'),
                        'exit_type': event.get('exit_type')},
                       {k: fill.get(k) for k in ('qty', 'quote', 'price', 'commission')})
        if event.get('opportunity') is not None:
            actionability[str(event.get('reason', event.get('event')))] += 1
        for label in ('desired_orders', 'accepted_orders'):
            for order in event.get(label, []):
                quantities(sizing, {'event': label, 'opportunity': event.get('opportunity_id'),
                                    'side': order.get('side'), 'sleeves': order.get('sleeves'),
                                    'sleeve_opportunity_ids': [event.get('sleeves', {}).get(str(w), {}).get('opportunity', {}).get('id')
                                                               for w in order.get('sleeves', [])],
                                    'constraints': event.get('constraints', [])},
                           {k: order.get(k) for k in ('quoteOrderQty', 'quantity', 'price', 'stopPrice')})
        if event.get('reason') is not None:
            reasons[str(event['reason'])] += 1
        for constraint in event.get('constraints', []):
            reasons[str(constraint.get('reason', constraint))] += 1
        if event.get('constraint'):
            reasons[str(event['constraint'])] += 1
        if event.get('exit_type'):
            exits[json.dumps(event['exit_type'], sort_keys=True)] += 1
        if event.get('idle_cash_usdt') is not None:
            idle.append(number(event['idle_cash_usdt']))
        if 'desired_btc' in event:
            quantities(sizing, {'event': event['event'], 'opportunity': event.get('opportunity'),
                                'constraint': event.get('constraint')},
                       {k: event.get(k) for k in ('desired_btc', 'accepted_btc', 'entry_estimate',
                                                 'sizing_capital_usdt', 'allocated_margin_usdt')})
        if kind == 'perp' and event['event'] == 'decision' and event.get('trigger') and event.get('decision_mark'):
            trigger = event['trigger']
            price = trigger.get('close', trigger.get('decision_mark'))
            if price is not None and number(price) > 0:
                gap = number(event['decision_mark']) / number(price) - 1
                price_gaps.append(gap)
                quantities(price_gap_links, {'opportunity': event.get('opportunity'), 'reason': event.get('reason'),
                                             'constraint': event.get('constraint')},
                           {'trigger_price': price, 'decision_price': event['decision_mark'], 'gap_return': gap})
        for sleeve in (event.get('sleeves', {}).values() if isinstance(event.get('sleeves'), dict) else []):
            reasons[str(sleeve['reason'])] += 1
            opportunity = sleeve.get('opportunity', {})
            if opportunity.get('id'):
                opportunities.setdefault(str(opportunity['id']),
                    (opportunity['trigger_bar_ms'] + DAY, opportunity['trigger_close']))
                if opportunity.get('age_ms') is not None:
                    ages.append(number(opportunity['age_ms']))
                if opportunity.get('trigger_close') and event.get('decision_price'):
                    price_gaps.append(number(event['decision_price']) / number(opportunity['trigger_close']) - 1)
        if event['event'] == 'opportunity':
            trigger = event.get('trigger', {}) or event
            price = trigger.get('close', trigger.get('trigger_close', event.get('close', event.get('decision_mark'))))
            opportunities.setdefault(str(event.get('identity', event.get('opportunity', event.get('at_ms')))), (event['at_ms'], price))
        if event.get('age_ms') is not None:
            ages.append(number(event['age_ms']))
    horizons = {}
    for days in (5, 20):
        values, unavailable = [], 0
        for at, price in opportunities.values():
            if not START_MS <= at < END_MS:
                continue
            target = (at + days * DAY) // DAY * DAY
            if price is None or target not in closes or number(price) <= 0:
                unavailable += 1
            else:
                values.append(closes[target] / number(price) - 1)
        horizons[str(days)] = {'sample_size': len(values), 'unavailable_endpoints': unavailable,
                              'mean_close_return': statistics.mean(values) if values else None}
    def summary(values):
        return {'n': len(values), 'mean': statistics.mean(values) if values else None,
                'maximum': max(values) if values else None}
    return {'semantic_event_counts': dict(counts), 'reason_observations': dict(reasons),
            'exit_fill_counts': dict(exits), 'distinct_opportunities': len(opportunities),
            'pre_window_opportunities': sum(at < START_MS for at, _ in opportunities.values()),
            'in_window_opportunities': sum(START_MS <= at < END_MS for at, _ in opportunities.values()),
            'actionability': 'Opportunity creation alone is not actionability; reasons/constraints include cold-start and consumed gates.',
            'opportunity_age_ms': summary(ages), 'decision_vs_trigger_return': summary(price_gaps),
            'idle_cash_usdt': summary(idle), 'sizing_observations': list(sizing.values()), 'filled': dict(filled),
            'fill_size_observations': list(fills_by_opportunity.values()),
            'decision_price_gap_observations': list(price_gap_links.values()),
            'size_summary_note': 'Counts/min/max/sums of actual recorded observations; repeated desired/accepted observations are not fills.',
            'opportunity_gate_observations': dict(actionability),
            'post_event_completed_day_closes': horizons,
            'diagnostic_only': 'Spot public daily closes at floored horizon; unavailable tails excluded. Post-run price observations; not executable fills, missed realized profit, or decision inputs.'}


def financial(row, curve, bars, fx, market_returns, cny_returns, initial):
    usd_initial = float(D(str(initial)) / fx(START_MS) * D('.999'))
    cny_metrics, cny_r = daily_metrics([r['equity_cny'] for r in curve], initial)
    usd_metrics, usd_r = daily_metrics([r['equity_usdt'] for r in curve], usd_initial)
    usd_metrics['final_usdt'] = usd_metrics.pop('final_cny')
    require(abs(cny_metrics['cagr'] - number(row['cagr'])) <= 1e-8, 'account CAGR differs from actual curve')
    require(number(row['mdd']) + 1e-8 >= cny_metrics['daily_mdd'], 'continuous MDD below closing curve MDD')
    # The original helper is retained for all identifiable ordinary account paths.
    down = [i for i, v in enumerate(market_returns) if v < 0]
    if len(down) >= 3 and len({market_returns[i] for i in down}) > 1 and len(set(cny_returns)) > 1:
        result = attribution(row, curve, market_returns, cny_returns, initial, fx)
    else:
        result = {'metrics': cny_metrics, 'usdt_btc_regression': safe_regression(usd_r, market_returns),
                  'cny_btc_regression': safe_regression(cny_r, cny_returns),
                  'continuous_mdd_from_account': number(row['mdd']),
                  'fees_usdt': row.get('fees', row['audit'].get('fees_usdt')), 'funding_paid_usdt': row.get('funding', '0')}
    years = {}
    for point, ret in zip(curve, usd_r):
        year = str(datetime.fromtimestamp(point['day_ms'] / 1000, timezone.utc).year)
        years[year] = years.get(year, 0.0) + math.log1p(ret)
    total = sum(years.values())
    start = next(i for i, r in enumerate(curve) if r['day_ms'] >= CUTOFF)
    result.update(usdt_metrics=usd_metrics,
                  validation_2022_plus={'usdt': daily_metrics([r['equity_usdt'] for r in curve[start:]], curve[start - 1]['equity_usdt'])[0],
                                        'cny': daily_metrics([r['equity_cny'] for r in curve[start:]], curve[start - 1]['equity_cny'])[0],
                                        'regression': safe_regression(usd_r[start:], market_returns[start:])},
                  calendar_log_return=years,
                  calendar_log_return_share={y: v / total if total else None for y, v in years.items()},
                  calendar_2026='partial through 2026-09-19 UTC',
                  prior_research_contamination=True, prospective_alpha_proven=False)
    return result


def consume(path, kind, env, bars, fx, market_returns, cny_returns, *, expected=None,
            reference=None, calibration=None, calibration_sha=None, capital='10000', offset=0, _manifest_child=False):
    bundle, raw_hash = read_json(path)
    if bundle.get('format') == 'alpha-account-manifest-v1':
        require(not _manifest_child and bundle.get('kind') == kind and bundle.get('files'), 'invalid or nested account manifest')
        merged, source_manifests, first = {}, [], None
        for entry in bundle['files']:
            child_path = Path(path).resolve().parent / entry['path']
            require(hash_value(entry['sha256']) and sha(child_path) == entry['sha256'], 'manifest raw bytes mismatch')
            # One level only; keep every measured source and original artifact hash.
            child = consume(child_path, kind, env, bars, fx, market_returns, cny_returns,
                            reference=reference or first, calibration=calibration,
                            calibration_sha=calibration_sha, capital=capital, offset=offset, _manifest_child=True)
            require(not set(merged) & set(child['accounts']), 'duplicate manifest account')
            merged.update(child['accounts'])
            source_manifests.append({k: v for k, v in child.items() if k != 'accounts'})
            first = first or child
        if expected is not None:
            require(set(merged) == set(expected), 'missing/unexpected manifest matrix accounts')
        return {'raw_sha256': raw_hash, 'path': str(Path(path).resolve()), 'metadata': first['metadata'],
                'account_source_manifests': source_manifests, 'accounts': merged}
    meta = metadata(bundle, kind, env, capital=capital, offset=offset)
    if reference:
        equivalent_inputs(reference['metadata'], meta, kind)
    require(meta.get('risk_calibration_sha256') == calibration_sha, 'risk calibration raw hash differs')
    accounts = {}
    names = SPEC[kind + '_candidates']
    starts = [s + offset for s in env['starts']]
    for candidate, scenario, row in rows(bundle, kind):
        key = candidate + '/' + scenario
        require(key not in accounts, 'duplicate account')
        require(scenario in SPEC[kind + '_scenarios'], 'unregistered stress')
        if expected is not None:
            require(key in expected, 'unexpected account: ' + key)
        parts = [] if candidate == names[0] else candidate.split('+') if kind == 'perp' else row['research_identity']['components']
        require(parts == [p for p in names[1:] if p in parts] and len(parts) == len(set(parts)), 'unregistered components/order')
        require(sum(p.startswith('core-') for p in parts) <= 1, 'incompatible core modes')
        if candidate in names:
            require(parts == ([] if candidate == names[0] else [candidate]), 'candidate/components mismatch')
        else:
            require(len(parts) >= 2 and (kind != 'spot' or candidate == 'combo'), 'invalid combination')
        if calibration:
            require(candidate in calibration.get('profiles', {}), 'no legal calibration profile for candidate: ' + candidate)
        profile = calibration['profiles'][candidate] if calibration else None
        if kind == 'spot':
            identity = row['research_identity']
            require(identity['candidate'] == candidate and identity['spec_sha256'] == env['spec_sha256'] and
                    identity['calibration_sha256'] == calibration_sha, 'row research identity mismatch')
            require(D(identity['risk_scale']) == D(profile['scale'] if profile else '1'), 'row risk scale mismatch')
            core = next((p for p in parts if p.startswith('core-')), None)
            require(identity['core_mode'] == core and D(identity['core_fraction']) == D('.20' if core else '0'), 'core allocation mismatch')
            require(row['risk_calibration'] == (dict(profile, sha256=calibration_sha) if profile else {'scale': '1', 'sha256': None}), 'row calibration mismatch')
        else:
            require(meta['candidates'][candidate] == parts, 'Coin component mismatch')
            require(meta['risk_profiles'].get(candidate) == profile, 'Coin profile mismatch')
        rejection = row_validity(row, kind, starts, capital, scenario)
        item = {'candidate': candidate, 'scenario': scenario, 'components': parts, 'valid': not rejection,
                'rejections': rejection, 'cagr': number(row['cagr']) if not rejection else None,
                'mdd': number(row['mdd']) if not rejection else None,
                'target_status': ('MET' if not rejection and number(row['cagr']) >= (1 if kind == 'spot' else 1.5) and
                                  (number(row['mdd']) <= .3 if kind == 'spot' else number(row['mdd']) < .5) else 'NOT_MET'),
                'failure': row.get('failure'), 'raw_bundle_sha256': raw_hash,
                'causal': diagnose(row['opportunity_ledger'], bars, kind)}
        if not rejection:
            curve = canonical(row, bars, fx, float(capital), kind)
            evidence = evidence_fingerprints(row, kind)
            item.update(financial=financial(row, curve, bars, fx, market_returns, cny_returns, float(capital)),
                        monetary_sha256=checksum({k: evidence[k] for k in ('financial', 'fills', 'daily')}),
                        evidence_sha256=evidence)
            # Only base curves are needed for calibration/actual rerun matching.
            if scenario == 'base':
                item['curve'] = curve
        accounts[key] = item
        # Drop journals/allocations immediately; bundle traversal retains only an empty row.
        row.clear()
    if expected is not None:
        require(set(accounts) == set(expected), 'missing matrix accounts: ' + ','.join(sorted(set(expected) - accounts.keys())))
    return {'raw_sha256': raw_hash, 'path': str(Path(path).resolve()), 'metadata': meta, 'accounts': accounts}


def selection(accounts, kind):
    names, scenarios = SPEC[kind + '_candidates'], SPEC[kind + '_scenarios']
    baseline = [accounts[names[0] + '/' + s] for s in scenarios]
    decisions, eligible = {}, []
    extra = sorted({a['candidate'] for a in accounts.values()} - set(names))
    for candidate in names + extra:
        rows_ = [accounts.get(candidate + '/' + s) for s in scenarios]
        valid = all(r and r['valid'] for r in rows_ + baseline)
        matched = valid and all(r['cagr'] >= b['cagr'] - .01 and
                    (r['mdd'] <= b['mdd'] if kind == 'spot' else r['mdd'] < .5) for r, b in zip(rows_, baseline))
        improvement = valid and (rows_[0]['cagr'] >= baseline[0]['cagr'] + .01 or
                      (rows_[0]['mdd'] <= baseline[0]['mdd'] - .01 and rows_[0]['cagr'] >= baseline[0]['cagr'] - .01))
        accepted = candidate != names[0] and matched and improvement
        decisions[candidate] = {'eligible': bool(accepted), 'complete_known_audited': bool(valid),
                                'paired_unscaled_constraints': bool(matched), 'base_improvement': bool(improvement),
                                'worst_cagr': min(r['cagr'] for r in rows_) if valid else None,
                                'rejection_reasons': [] if accepted or candidate == names[0] else
                                [name for flag, name in ((valid, 'incomplete_unknown_or_failed_audit'), (matched, 'paired_stress_constraints'),
                                                         (improvement, 'insufficient_base_improvement')) if not flag]}
        if accepted:
            eligible.append(candidate)
    order = names + extra
    chosen = max(eligible, key=lambda n: (decisions[n]['worst_cagr'], -order.index(n))) if eligible else names[0]
    components = [n for n in names[1:] if n in eligible]
    if all(n in components for n in ('core-permanent', 'core-slow')):
        winner = max(('core-permanent', 'core-slow'), key=lambda n: (decisions[n]['worst_cagr'], n == 'core-slow'))
        components.remove('core-slow' if winner == 'core-permanent' else 'core-permanent')
    combo_name = 'combo' if kind == 'spot' else '+'.join(components)
    status = 'not_applicable' if not components else 'existing_singleton' if len(components) == 1 else (
             'measured' if combo_name in decisions else 'pending_actual_four_stresses')
    return {'selected_research_candidate': chosen, 'decisions': decisions, 'compatible_components': components,
            'combination_status': status, 'production_promoted': False, 'native_qualification': 'NOT_QUALIFIED'}


def calibration_document(bundles, market_returns, fx):
    profiles, diagnostics = {}, {}
    initial = float(D(10000) / fx(START_MS) * D('.999'))
    for kind, bundle in bundles.items():
        baseline_name = SPEC[kind + '_candidates'][0]
        baseline = bundle['accounts'][baseline_name + '/base']
        if not baseline['valid']:
            diagnostics[baseline_name] = {'unavailable': 'baseline incomplete/unknown/unaudited'}
            continue
        for candidate in SPEC[kind + '_candidates']:
            account = bundle['accounts'][candidate + '/base']
            if not account['valid']:
                continue
            scale, diag = calibrate(account['curve'], baseline['curve'], market_returns, initial)
            profiles[candidate] = {'scale': scale, 'effective_from_ms': CUTOFF, 'calibration_end_ms': CUTOFF,
                'training_end_day_exclusive': '2022-01-01', 'base_bundle_sha256': bundle['raw_sha256'], 'baseline_candidate': baseline_name}
            diagnostics[candidate] = diag
    return {'format': 1, 'cutoff_ms': CUTOFF, 'spec_sha256': sha(SPEC_PATH), 'profiles': profiles}, diagnostics


def risk_inventory(bundle, kind):
    """Inventory every registered direction before seeing any rerun outcome."""
    names = SPEC[kind + '_candidates']
    baseline = bundle['accounts'][names[0] + '/base']
    inventory, required = {}, set()
    for name in names[1:]:
        base = bundle['accounts'][name + '/base']
        if not base['valid']:
            status, reasons = 'not_applicable_due_to_invalid_unscaled_base', base['rejections']
        elif not baseline['valid']:
            status, reasons = 'pending_valid_unscaled_baseline_for_calibration', baseline['rejections']
        else:
            status, reasons = 'pending_actual_risk_account', []
            required.add(name + '/base')
        inventory[name] = {'status': status, 'rejections': reasons, 'achieved_match': False,
                           'unscaled_base_raw_sha256': base['raw_bundle_sha256']}
    if baseline['valid']:
        required.add(names[0] + '/base')
    control = {'status': 'pending_actual_unity_control' if baseline['valid'] else
                        'not_applicable_due_to_invalid_unscaled_baseline',
               'passed': False, 'rejections': baseline['rejections']}
    return inventory, required, control


def original_baseline(path, kind, bundle, env):
    """Pin immutable originals and compare complete financial/operating evidence."""
    old, digest = read_json(path)
    require(digest == APPROVED_BASELINES[kind], 'original baseline raw SHA is not the approved immutable reference')
    meta = old if kind == 'spot' else old['inputs']
    verify_source(meta['source'], kind)
    repo = ROOT if kind == 'spot' else ROOT.parent / 'coinquant'
    ancestry = subprocess.run(['git', 'merge-base', '--is-ancestor', meta['source']['git_head'], SPEC[kind + '_baseline_git']], cwd=repo)
    require(ancestry.returncode == 0, 'original baseline is not ancestor of registered baseline')
    if kind == 'perp':
        require(old['derivation']['execution_source'] == meta['source'] and
                old['derivation']['original_accounts_sha256'] == COIN_ORIGINAL_ACCOUNTS_SHA and
                old['derivation']['method'] == 'remove only exact END funding debit; no curve scaling or replay relabelling',
                'original derivation provenance differs')
        verify_source(meta['derivation_source'], kind)
    for key in ('fx_sha256', 'schedule_sha256'):
        require(meta[key] == bundle['metadata'][key], 'original baseline input mismatch')
    market_key = 'market_sha256' if kind == 'spot' else 'market_identity'
    require(meta[market_key] == bundle['metadata'][market_key], 'original baseline market mismatch')
    name = SPEC[kind + '_candidates'][0]
    checks, evidence_checks = {}, {}
    for scenario in SPEC[kind + '_scenarios']:
        row = old['results'][name + '-' + scenario] if kind == 'spot' else old['results'][name][scenario]
        require(not row_validity(row, kind, env['starts'], '10000', scenario), 'invalid original baseline account')
        if kind == 'perp':
            key = name + '/' + scenario
            require(key not in old['derivation']['changed_accounts'], 'incumbent baseline must be unchanged by terminal derivation')
            original = {k: v for k, v in row.items() if k not in ('candidate', 'scenario', 'initial_cny', 'original_row_sha256')}
            require(checksum(original) == row['original_row_sha256'] == old['derivation']['row_bindings_sha256'][key],
                    'Coin original row binding differs')
        incoming = bundle['accounts'][name + '/' + scenario]
        measured = incoming.get('evidence_sha256', {})
        expected = evidence_fingerprints(row, kind)
        evidence_checks[scenario] = {k: value == measured.get(k) for k, value in expected.items()}
        checks[scenario] = all(evidence_checks[scenario].values())
    return {'raw_sha256': digest, 'source': meta['source'], 'registered_baseline_git': SPEC[kind + '_baseline_git'],
            'derivation': old.get('derivation'), 'derivation_source': meta.get('derivation_source'),
            'scenarios': checks, 'evidence_checks': evidence_checks,
            'comparison_scope': 'financial, fills, daily, ownership, operating and remaining original fields',
            'passed': all(checks.values())}


def market_returns_for(bars, fx):
    active = [b for b in bars if START_MS <= b[0] < END_MS]
    previous, previous_cny = active[0][1], active[0][1] * fx(START_MS)
    usd, cny = [], []
    for bar in active:
        price, converted = bar[4], bar[4] * fx(bar[0] + DAY)
        usd.append(float(price / previous - 1))
        cny.append(float(converted / previous_cny - 1))
        previous, previous_cny = price, converted
    return usd, cny


def assess(args):
    analysis_source = source_identity()
    require(analysis_source['dirty'] is False, 'commit assessment source before running')
    env = environment(args.schedule, args.fx, args.market)
    bars, fx = load_daily(args.market, END_MS, require_through=END_MS), PriorFX(args.fx)
    usd_returns, cny_returns = market_returns_for(bars, fx)
    bundles = {}
    for kind in ('spot', 'perp'):
        expected = {n + '/' + s for n in SPEC[kind + '_candidates'] for s in SPEC[kind + '_scenarios']}
        bundles[kind] = consume(getattr(args, kind), kind, env, bars, fx, usd_returns, cny_returns, expected=expected)
    calibrated, training = calibration_document(bundles, usd_returns, fx)
    if args.calibration_out:
        write_new(args.calibration_out, calibrated)
    calibration, calibration_hash = (read_json(args.calibration) if args.calibration else (None, None))
    if calibration is not None:
        require(calibration == calibrated, 'calibration differs from deterministic training or bound raw inputs')
    report = {'format': 1, 'spec_sha256': env['spec_sha256'], 'protocol_sha256': env['protocol_sha256'],
              'analysis_source': analysis_source, 'input_environment': env,
              'training': training, 'calibration_sha256': calibration_hash,
              'baseline_verification': {}, 'selection': {}, 'risk': {}, 'risk_baseline_controls': {},
              'sensitivity': [], 'inputs': {}, 'accounts': {}, 'pending': [],
              'passive_controls': passive_controls(bars, fx), 'actual_account_days': 0, 'native_cases': 0,
              'native_qualification': 'NOT_QUALIFIED', 'prospective_alpha_proven': False,
              'limitations': ['Previously studied 2020-2026 history; stability testing, not clean out-of-sample.',
                'Spot OHLC/high-before-low and Coin minute/volume/known-mark-gap proxies retained.',
                'Continuous account MDD is distinct from closing daily MDD.',
                'Economic eligibility and descriptive HAC7 regression do not prove repeatable alpha.',
                'Calendar2026 is partial through September19. Forward diary records public observations only.']}
    initial = float(D(10000) / fx(START_MS) * D('.999'))
    for kind, bundle in bundles.items():
        baseline = SPEC[kind + '_candidates'][0]
        report['inputs'][kind] = {k: v for k, v in bundle.items() if k != 'accounts'}
        old_path = getattr(args, 'baseline_' + kind)
        if old_path:
            report['baseline_verification'][kind] = original_baseline(old_path, kind, bundle, env)
        else:
            report['baseline_verification'][kind] = {'passed': False, 'status': 'pending_original_raw_comparison'}
            report['pending'].append(kind + ':original_baseline_comparison')
        selected = selection(bundle['accounts'], kind)
        parts = selected['compatible_components']
        combo_path = getattr(args, 'combo_' + kind)
        if combo_path:
            require(len(parts) >= 2, 'unregistered/unnecessary combination account')
            name = 'combo' if kind == 'spot' else '+'.join(parts)
            combo = consume(combo_path, kind, env, bars, fx, usd_returns, cny_returns,
                            expected={name + '/' + s for s in SPEC[kind + '_scenarios']}, reference=bundle)
            require(all(a['components'] == parts for a in combo['accounts'].values()), 'combo differs from all eligible compatible directions')
            bundle['accounts'].update(combo['accounts'])
            report['inputs']['combo_' + kind] = {k: v for k, v in combo.items() if k != 'accounts'}
            selected = selection(bundle['accounts'], kind)
        if selected['combination_status'] == 'pending_actual_four_stresses':
            report['pending'].append(kind + ':combination_four_stresses')
        selected['adoption_blocked'] = not report['baseline_verification'][kind]['passed']
        report['selection'][kind] = selected
        risk_path = getattr(args, 'risk_' + kind)
        inventory, needed, control = risk_inventory(bundle, kind)
        report['risk'][kind] = inventory
        report['risk_baseline_controls'][kind] = control
        if risk_path:
            require(calibration is not None, 'actual risk bundle requires --calibration exact file')
            require(bool(needed), 'no legal risk profiles for an invalid unscaled baseline')
            risk = consume(risk_path, kind, env, bars, fx, usd_returns, cny_returns, reference=bundle,
                           calibration=calibration, calibration_sha=calibration_hash, expected=needed)
            report['inputs']['risk_' + kind] = {k: v for k, v in risk.items() if k != 'accounts'}
            for key, account in risk['accounts'].items():
                if account['candidate'] == baseline:
                    expected_evidence = bundle['accounts'][key].get('evidence_sha256', {})
                    actual_evidence = account.get('evidence_sha256', {})
                    equal = account['valid'] and bool(expected_evidence) and actual_evidence == expected_evidence
                    if account['valid'] and not equal:
                        account['valid'] = False
                        account['rejections'].append('unity_control_evidence_mismatch')
                    control.update(status='measured_passed' if equal else 'rejected_actual_unity_control',
                                   passed=bool(equal), raw_sha256=risk['raw_sha256'],
                                   rejections=account['rejections'],
                                   evidence_checks={k: v == actual_evidence.get(k) for k, v in expected_evidence.items()})
                else:
                    obligation = inventory[account['candidate']]
                    obligation.update(status='measured_valid' if account['valid'] else 'rejected_actual_rerun',
                                      raw_sha256=risk['raw_sha256'])
                    if account['valid']:
                        obligation.update(achieved_match(account['curve'], bundle['accounts'][baseline + '/base']['curve'], usd_returns, initial))
                    else:
                        obligation.update(classification='actual_rerun_invalid', rejections=account['rejections'])
                account.pop('curve', None)
                report['accounts'][kind + '/risk/' + key] = account
        elif needed or any(v['status'].startswith('pending_') for v in inventory.values()):
            report['pending'].append(kind + ':risk_base_accounts_and_unity_control')
        for key, account in bundle['accounts'].items():
            account.pop('curve', None)
            report['accounts'][kind + '/' + key] = account
    # Each sensitivity input is a real one-account producer bundle; the selected
    # candidate and incumbent are checked at the four registered perturbations.
    final = report['selection']['perp']['selected_research_candidate']
    candidates = {SPEC['perp_candidates'][0], final}
    required = {(n, budget, shift) for n in candidates for budget, shift in
                [('9900', 0), ('10100', 0), ('10000', -60000), ('10000', 60000)]}
    seen = set()
    for path in args.sensitivity_perp:
        probe, _ = read_json(path)
        capital, offset = probe['inputs']['initial_cny'], probe['inputs']['start_offset_ms']
        candidate = next(iter(probe['results']))
        del probe
        identity = (candidate, capital, offset)
        require(identity in required and identity not in seen, 'unexpected/duplicate sensitivity account')
        diagnostic = consume(path, 'perp', env, bars, fx, usd_returns, cny_returns,
                             expected={candidate + '/base'}, reference=bundles['perp'], capital=capital, offset=offset)
        account = diagnostic['accounts'][candidate + '/base']
        account.pop('curve', None)
        report['sensitivity'].append({'candidate': candidate, 'initial_cny': capital, 'start_offset_ms': offset,
                                      'raw_sha256': diagnostic['raw_sha256'], 'metadata': diagnostic['metadata'], 'account': account})
        seen.add(identity)
    if required - seen:
        report['pending'].append('perp:capital_start_sensitivity')
    report['sensitivity_missing'] = sorted(required - seen)
    report['risk_obligation_count'] = sum(len(v) for v in report['risk'].values())
    require(report['risk_obligation_count'] == 10, 'all ten registered risk obligations must remain inventoried')
    report['final_completion_note'] = 'No pending work is distinct from validation; rejected/inapplicable cases remain invalid and block rule freeze.'
    report['risk_accounts_pending'] = any(':risk_' in item for item in report['pending'])
    report['registered_work_pending'] = bool(report['pending'])
    report['all_measured_accounts_valid'] = all(a['valid'] for a in report['accounts'].values()) and all(s['account']['valid'] for s in report['sensitivity'])
    report['rules_freeze_ready'] = not report['registered_work_pending'] and all(v['passed'] for v in report['baseline_verification'].values()) and report['all_measured_accounts_valid'] and all(v['passed'] for v in report['risk_baseline_controls'].values())
    if args.final:
        require(not report['registered_work_pending'], 'final report still has pending registered work')
    write_new(args.out, report)
    if args.csv:
        export_csv(args.csv, report)
    if args.markdown:
        export_markdown(args.markdown, report)
    return report


def export_csv(path, report):
    with Path(path).open('x', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['account', 'valid', 'cagr', 'continuous_mdd', 'daily_mdd', 'fees_usdt', 'funding_usdt', 'rejections', 'raw_sha256'])
        for name, account in report['accounts'].items():
            metrics = account.get('financial', {})
            writer.writerow([name, account['valid'], account['cagr'], account['mdd'], metrics.get('metrics', {}).get('daily_mdd'),
                metrics.get('fees_usdt'), metrics.get('funding_paid_usdt'), ';'.join(account['rejections']), account['raw_bundle_sha256']])


def export_markdown(path, report):
    lines = ['# Registered BTC alpha/beta assessment', '',
             'Historical proxy evidence; previously studied history. Native qualification: NOT_QUALIFIED.', '',
             'Pending: ' + (', '.join(report['pending']) or 'none'), '',
             '| Account | Valid | CAGR | Continuous MDD | Rejection |', '|---|---|---|---|---|']
    for name, account in report['accounts'].items():
        lines.append('| ' + ' | '.join(map(str, [name, account['valid'], account['cagr'], account['mdd'], ', '.join(account['rejections'])])) + ' |')
    lines += ['', 'Full JSON contains source/input hashes, all-candidate costs, ES, capture, calendar concentration, causal diagnosis, risk match and sensitivity accounts.',
              'Post-event closes are descriptive prices, never assumed missed realized profit. 2026 ends September19.', '']
    with Path(path).open('x') as stream:
        stream.write('\n'.join(lines))


def current_source(kind):
    repo = ROOT if kind == 'spot' else ROOT.parent / 'coinquant'
    package = 'spotquant' if kind == 'spot' else 'coinquant'
    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True).stdout
    require(not git('status', '--porcelain', '--', package, 'research').strip(), 'forward requires clean source')
    digest = hashlib.sha256()
    for name in sorted(git('ls-files', package, 'research').decode().split()):
        if name.endswith('.py'):
            digest.update(name.encode() + b'\0' + (repo / name).read_bytes() + b'\0')
    return {'git_head': git('rev-parse', 'HEAD').decode().strip(), 'dirty': False, 'python_sources_sha256': digest.hexdigest()}


def forward_binding(analysis, kind):
    report, digest = read_json(analysis)
    require(report['rules_freeze_ready'] is True and not report['registered_work_pending'], 'rules not ready to freeze')
    require(report['spec_sha256'] == sha(SPEC_PATH) and report['protocol_sha256'] == sha(PROTOCOL_PATH), 'forward spec/protocol mismatch')
    current = current_source(kind)
    frozen = report['inputs'][kind]['metadata']['source']
    require(current['python_sources_sha256'] == frozen['python_sources_sha256'], 'forward source not equivalent to frozen rules')
    require(current_source('spot')['python_sources_sha256'] == report['analysis_source']['python_sources_sha256'], 'analysis executable source mismatch')
    selected = report['selection'][kind]['selected_research_candidate']
    return {'analysis_sha256': digest, 'source': frozen, 'spec_sha256': report['spec_sha256'],
            'protocol_sha256': report['protocol_sha256'], 'kind': kind, 'rule': selected,
            'baseline_sha256': report['baseline_verification'][kind]['raw_sha256']}, current


def forward_init(path, analysis, kind):
    binding, execution = forward_binding(analysis, kind)
    body = {'format': 1, 'binding': binding, 'initialized_ms': int(time.time() * 1000),
            'initial_execution_source': execution, 'cash_cny': '10000', 'btc': '0', 'observations': [],
            'native_cases': 0, 'actual_account_days': 0, 'elapsed_account_days': 0,
            'purpose': 'All-cash public observation diary; no simulated or native fills.'}
    write_new(path, {'body': body, 'sha256': checksum(body)})
    return body


def forward_append(path, analysis, kind, bar):
    """Locked atomic replacement; validate the latest completed bar at receipt.

    bar = {symbol, interval_ms, open_ms, close_ms, open, high, low, close,
           observed_ms, public_source, public_payload_sha256}. Close is the
    exclusive UTC boundary (normalize Binance's inclusive closeTime +1).
    """
    import fcntl
    path = Path(path)
    with Path(str(path) + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        wrapped, _ = read_json(path)
        body = wrapped['body']
        require(checksum(body) == wrapped['sha256'], 'forward diary integrity failure')
        binding, execution = forward_binding(analysis, kind)
        require(binding == body['binding'], 'forward source/spec/rule/baseline mismatch')
        now = int(time.time() * 1000)
        interval = DAY if kind == 'spot' else 4 * 60 * 60 * 1000
        require(bar['symbol'] == 'BTCUSDT' and bar['interval_ms'] == interval, 'forward symbol/granularity mismatch')
        for key in ('observed_ms', 'open_ms', 'close_ms'):
            require(type(bar[key]) is int, 'integer UTC milliseconds required')
        close, observed = bar['close_ms'], bar['observed_ms']
        require(body['initialized_ms'] < close <= observed <= now, 'backfill or future observation')
        require(close == now // interval * interval == observed // interval * interval and bar['open_ms'] == close - interval,
                'only latest completed public bar at receipt allowed')
        prior = body['observations'][-1]['bar']['close_ms'] if body['observations'] else body['initialized_ms']
        require(close > prior, 'bar identity must strictly advance')
        prices = {key: number(bar[key]) for key in ('open', 'high', 'low', 'close')}
        require(0 < prices['low'] <= min(prices['open'], prices['close']) <= max(prices['open'], prices['close']) <= prices['high'], 'malformed OHLC')
        require(isinstance(bar['public_source'], str) and bar['public_source'].startswith('https://') and hash_value(bar['public_payload_sha256']), 'public provenance required')
        body['observations'].append({'bar': bar, 'receipt_ms': now, 'execution_source': execution,
                                     'skipped_intervals': max(0, (close - prior) // interval - 1)})
        fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'body': body, 'sha256': checksum(body)}, stream, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spot', type=Path)
    parser.add_argument('--perp', type=Path)
    parser.add_argument('--out', type=Path)
    for name in ('calibration-out', 'calibration', 'risk-spot', 'risk-perp', 'combo-spot', 'combo-perp',
                 'baseline-spot', 'baseline-perp', 'csv', 'markdown'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--sensitivity-perp', type=Path, action='append', default=[])
    parser.add_argument('--final', action='store_true')
    parser.add_argument('--market', type=Path, default=Path('/tmp/spotquant-market/klines'))
    parser.add_argument('--fx', type=Path, default=ROOT.parent / 'starquant/data/usdcny_frankfurter.json')
    parser.add_argument('--schedule', type=Path, default=ROOT.parent / 'coinquant/research/session_schedule.json')
    parser.add_argument('--forward-init', type=Path)
    parser.add_argument('--forward-append', type=Path)
    parser.add_argument('--analysis', type=Path)
    parser.add_argument('--kind', choices=('spot', 'perp'))
    parser.add_argument('--bar', type=Path)
    args = parser.parse_args(argv)
    if args.forward_init or args.forward_append:
        require(bool(args.forward_init) != bool(args.forward_append) and args.analysis and args.kind, 'choose forward init or append with analysis/kind')
        if args.forward_init:
            forward_init(args.forward_init, args.analysis, args.kind)
        else:
            require(args.bar is not None, 'append requires public --bar JSON')
            forward_append(args.forward_append, args.analysis, args.kind, read_json(args.bar)[0])
    else:
        require(args.spot and args.perp and args.out, '--spot --perp --out required')
        assess(args)


if __name__ == '__main__':
    main()
