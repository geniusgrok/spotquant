"""Read-only, source-bound assessment of the frozen edge account inventory.

Every input is an original producer file (or one-level hash manifest). No producer,
network client, order, curve scaling, or parameter search runs in this module.
"""
import argparse
import bisect
from collections import Counter
from decimal import Decimal as D
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
from types import SimpleNamespace

from research import alpha_assessment as old
from research.complete_assessment import canonical, daily_metrics
from research.edge_features import FeatureBook
from research.session_account import audit as spot_audit

ROOT = Path(__file__).resolve().parents[1]
DAY, START, END, CUTOFF = old.DAY, old.START_MS, old.END_MS, old.CUTOFF
BASE = {'spot': 'atr-stop', 'perp': 'incumbent'}
ORDER = {'spot': ('exit-confirm', 'stop-budget', 'crowding-interaction'),
         'perp': ('quality-budget', 'cost-horizon', 'crowding-interaction')}
STRESSES = {'spot': ('base', 'fee150', 'slip2', 'outage'),
            'perp': ('base', 'fees-x1.5', 'read-400ms', 'trigger-slip')}
CONTROLS = ('cash', 'protected-participation-25', 'protected-participation-50',
            'protected-participation-75', 'protected-participation-100')
SPEC_HASH = {'spot': 'af23d8afdce40c7cfc60387cc70e0332739a017c7b9a5460b9a5dbe9444b409f',
             'perp': '53296233aadc6429359dd7b664c4e03a28eb451d6bacf4318fced4ce13f7e3de'}
PROTOCOL_HASH = {'spot': 'c20446129708ef998ce9ab403ec6a14760b008cc00ad0ad49ea3488c2dc897bd',
                 'perp': '714e0eae887fc9aac79380662c7db1e931823484fc2fe3781c80c2853bb4163b'}
FEATURE_HASH = 'bf920626cc13653b8bbeafd171650cc1a76888e15aad0c20bbd1061fb41da512'
PRIMARY_HASH = 'f8fb73bebf142ddcc3ed4a3e6b12b4dd7abed1e27bcd8a4ff1c93aec4fe0b32a'
PINNED_REFERENCES = {
    'review/final64-financial-review-proof.json': '9453b031924322612da3445e305e0499dce87eda20eb604def14ff7aa54241de',
    'scratch/registered-final-repaired.json': 'a35ef6727d1a3befa1c86778eadc3bb604ffff3ad2b7cb63275419dc30c9a5d6',
    'review/spot-canonical-five-financial-proof.json': 'c1803ecb6c66a1f6397415ce2b033f569ebca46e6df42419bee87ea45f351b8d',
    'scratch/spot-canonical/canonical-inventory.json': 'db23143dffbd6d8c5711789fee8b3ac92454d7712bcad14644cd244197701b32',
    'scratch/perp-singletons-retry1.json.gz': 'bf1d167979f4bb3d15925f0bcaf5337bd1cc0a17523628b3af0599875dc5a7c1',
    'scratch/spot-singletons/atr-stop.json.gz': 'f8f2e18abbeb7f062796026c074fb7868e3b2524a52c5a5bcf89131b6e655917',
}
MANIFEST = 'btc-edge-account-manifest-v1'
require, sha, write_new = old.require, old.sha, old.write_new


def read_json(path):
    body, digest = old.read_json(path)
    def finite(value):
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)
        elif isinstance(value, float):
            require(math.isfinite(value), 'nonfinite JSON numeric overflow')
    finite(body)
    return body, digest


def decimal(value):
    require(not isinstance(value, bool), 'boolean numeric value')
    result = D(str(value))
    require(result.is_finite(), 'nonfinite numeric value')
    return result


def committed_hash(repo, head, name):
    raw = subprocess.run(['git', 'show', head + ':' + name], cwd=repo,
                         check=True, capture_output=True).stdout
    return hashlib.sha256(raw).hexdigest()


def verify_source(source, kind):
    """Prove the full Python digest and both actual committed protocol families."""
    require(kind in BASE, 'unknown project')
    files = old.verify_source(source, kind)
    repo = ROOT if kind == 'spot' else ROOT.parent / 'coinquant'
    for name in ('edge_spec.json', 'edge-PROTOCOL.md', 'complete-delivery-PROTOCOL.md'):
        path = 'research/' + name
        files[path] = committed_hash(repo, source['git_head'], path)
    require(files['research/edge_spec.json'] == SPEC_HASH[kind] and
            files['research/edge-PROTOCOL.md'] == PROTOCOL_HASH[kind], 'committed edge contract mismatch')
    changed = subprocess.run(['git', 'diff', '--name-only', source['git_head'], 'HEAD', '--', 'research',
                              kind.replace('perp', 'coin') + 'quant'], cwd=repo, check=True, capture_output=True, text=True).stdout.splitlines()
    allowed = {'research/edge_assessment.py'} if kind == 'spot' else set()
    require(not [p for p in changed if p.endswith('.py') and p not in allowed], 'measured execution differs from current frozen producer')
    return files


def components(kind, name, explicit=None):
    if name == BASE[kind] or (kind == 'spot' and name in CONTROLS):
        parts = []
    elif name in ORDER[kind]:
        parts = [name]
    else:
        parts = explicit if kind == 'spot' and name == 'combo' else name.split('+')
        require(isinstance(parts, list) and len(parts) >= 2 and
                parts == [p for p in ORDER[kind] if p in parts], 'unregistered combination/order')
    if explicit is not None:
        require(parts == explicit, 'component envelope mismatch')
    return parts


def account_id(kind, name, scenario='base', capital='10000', offset=0, risk=False):
    return '|'.join((kind, name, scenario, str(decimal(capital).normalize()), str(offset), 'risk' if risk else 'unscaled'))


def required_matrix(selected=None, combos=None):
    selected, combos = selected or BASE, combos or {}
    result = set()
    for kind in BASE:
        names = (BASE[kind], *ORDER[kind], *(CONTROLS if kind == 'spot' else ()))
        for name in names:
            result.update(account_id(kind, name, s) for s in STRESSES[kind])
        for name in (BASE[kind], *ORDER[kind]):
            result.add(account_id(kind, name, risk=True))
        if combos.get(kind):
            name = combo_name(kind, combos[kind])
            result.update(account_id(kind, name, s) for s in STRESSES[kind])
            result.add(account_id(kind, name, risk=True))
        for name in {BASE[kind], selected[kind]}:
            result.update(account_id(kind, name, capital=b) for b in (2500, 5000, 7500))
    result.update(account_id('perp', BASE['perp'], offset=o) for o in (-60000, 60000))
    return result


def load_files(path):
    """One-level manifests keep every original child envelope and exact byte hash."""
    path = Path(path)
    body, digest = read_json(path)
    if body.get('format') != MANIFEST:
        return [(path, digest, body)], {str(path): digest}
    require(set(body) == {'format', 'files'} and isinstance(body['files'], list), 'manifest schema')
    result, bindings, seen = [], {str(path): digest}, set()
    for item in body['files']:
        require(set(item) == {'path', 'sha256'} and old.hash_value(item['sha256']), 'manifest child binding')
        child = (path.parent / item['path']).resolve()
        require(child not in seen, 'duplicate manifest file')
        seen.add(child)
        raw, actual = read_json(child)
        require(actual == item['sha256'], 'manifest original child hash mismatch')
        require(raw.get('format') != MANIFEST and 'files' not in raw, 'nested manifests forbidden')
        result.append((child, actual, raw))
        bindings[str(child)] = actual
    return result, bindings


def environment(schedule, fx, market, features):
    schedule_body, schedule_hash = read_json(schedule)
    starts = schedule_body['primary']['starts_ms']
    require(len(starts) == 795 and starts == sorted(set(starts)) and all(type(v) is int for v in starts), '795 frozen starts required')
    primary = hashlib.sha256(json.dumps(starts, separators=(',', ':')).encode()).hexdigest()
    require(primary == schedule_body['primary']['sha256'] == PRIMARY_HASH and
            schedule_body['session_seconds'] == 300 and schedule_body['poll_seconds'] == 5, 'registered schedule differs')
    book = FeatureBook(features, expected_sha256=FEATURE_HASH)
    require(book.source['market_identities']['spot_daily_sha256'] == old.file_digest(market), 'feature/market bytes mismatch')
    return dict(starts=starts, primary_sha256=primary, schedule_sha256=schedule_hash,
                fx_sha256=sha(fx), market_sha256=old.file_digest(market), feature_source=book.source,
                features_sha256=sha(features))


def envelope(bundle, kind, env, source_cache):
    edge = bundle['edge']
    meta = bundle if kind == 'spot' else bundle['inputs']
    binding = bundle if kind == 'spot' else edge
    source = meta['source']
    key = (kind, json.dumps(source, sort_keys=True))
    if key not in source_cache:
        source_cache[key] = verify_source(source, kind)
    proof = source_cache[key]
    for name in ('spec_sha256', 'protocol_sha256'):
        expected = SPEC_HASH[kind] if name == 'spec_sha256' else PROTOCOL_HASH[kind]
        require(binding[name] == expected, 'edge ' + name + ' mismatch')
    require(meta['fx_sha256'] == env['fx_sha256'], 'FX input mismatch')
    if kind == 'spot':
        require(bundle['format'] == 'btc-alpha-beta-edge-spot-v1', 'Spot producer format')
        require(meta['schedule_sha256'] == env['schedule_sha256'] and
                meta['market_sha256'] == env['market_sha256'], 'Spot market/schedule mismatch')
        require(binding['feature_sha256'] in (None, FEATURE_HASH), 'Spot feature hash mismatch')
        require(edge['execution'] == 'actual_finite_session_lifecycle', 'actual Spot execution required')
        require(len(bundle['results']) == 1, 'Spot envelope is single-account')
        name, scenario = edge['candidate'], edge['scenario']
        parts = components(kind, name, edge['components'])
        require(bundle['feature_consumed'] is ('crowding-interaction' in parts), 'feature consumption mismatch')
        require('crowding-interaction' not in parts or binding['feature_sha256'] == FEATURE_HASH, 'missing consumed features')
        row = bundle['results'][name + '-' + scenario]
        require(row['candidate'] == edge['original_candidate'] == name and row['scenario'] == scenario, 'original Spot identity mismatch')
        return [(name, scenario, row)], binding, source, 0
    require(edge['measured_source'] == source, 'conflicting Coin measured source')
    require(edge['original_inputs'] == meta and edge['original_conditions'] == bundle['conditions'], 'original Coin envelope altered')
    require(meta['protocol_sha256'] == proof['research/complete-delivery-PROTOCOL.md'], 'original Coin meter protocol mismatch')
    require(meta['schedule_sha256'] == edge['original_schedule_sha256'] == env['primary_sha256'], 'Coin schedule mismatch')
    offset = edge['start_offset_ms']
    require(type(offset) is int and offset in (-60000, 0, 60000), 'unregistered start offset')
    require(edge['actual_starts_sha256'] == old.checksum([v + offset for v in env['starts']]), 'actual starts digest mismatch')
    require(edge['feature_file_sha256'] == FEATURE_HASH and edge['feature_source'] == env['feature_source'] and
            edge['feature_reader_sha256'] == proof['research/edge_features.py'], 'Coin feature source mismatch')
    require(meta['crowding_sha256'] == old.checksum(None) and meta['crowding_source'] == 'not used by alpha/beta mechanisms', 'foreign original crowding input')
    require(meta['market_identity'] == env['feature_source']['futures_market_identity'], 'Coin original market identity mismatch')
    for field in ('loaded_minute_files', 'loaded_print_files'):
        require(isinstance(meta[field], dict) and all(old.hash_value(v) for v in meta[field].values()), 'invalid consumed market hashes')
    conditions = bundle['conditions']
    require(decimal(conditions['conversion_each_way']) == D('.001') and
            conditions['initial_fx_corrected_before_first_metric'] is True and
            conditions['read_latency_ms'] == 200 and conditions['write_latency_ms'] == 1000 and
            decimal(conditions['default_fee']) == D('.00075'), 'Coin original economic conditions mismatch')
    require(set(bundle['results']) == set(edge['candidates']), 'Coin candidate manifest mismatch')
    rows = []
    for name, scenarios in bundle['results'].items():
        components(kind, name, edge['candidates'][name])
        for scenario, row in scenarios.items():
            expected_options = {'base': {}, 'fees-x1.5': {'fee': '0.001125'},
                                'read-400ms': {'read_latency_ms': 400}, 'trigger-slip': {'trigger_slippage': '0.0015'}}
            require(scenario in expected_options and conditions['scenarios'].get(scenario) == expected_options[scenario], 'original scenario conditions mismatch')
            if row['complete']:
                require(conditions.get('terminal_funding_exclusive_ms') == END, 'exclusive terminal funding boundary missing')
            require(decimal(row['initial_cny']) == decimal(edge['initial_cny']) == decimal(conditions['start_cny']), 'Coin capital envelope mismatch')
            rows.append((name, scenario, row))
    return rows, binding, source, offset


def compare_inputs(left, right, kind, *, current=False):
    """Compare economics across old/new families without overwriting source labels."""
    a = left if kind == 'spot' else left['inputs']
    b = right if kind == 'spot' else right['inputs']
    keys = ('fx_sha256', 'schedule_sha256', 'market_sha256') if kind == 'spot' else (
        'fx_sha256', 'schedule_sha256', 'market_identity', 'crowding_sha256', 'crowding_source')
    for key in keys:
        require(a[key] == b[key], 'economic input disagreement: ' + key)
    if kind == 'perp':
        for key in ('loaded_minute_files', 'loaded_print_files'):
            require(all(a[key][n] == b[key][n] for n in a[key].keys() & b[key].keys()), 'consumed file disagreement: ' + key)
        excluded = {'start_cny', 'scenarios', 'alpha_beta_research', 'post_run_diagnostic_prices_used_for_decisions'}
        require({k: v for k, v in left['conditions'].items() if k not in excluded} ==
                {k: v for k, v in right['conditions'].items() if k not in excluded}, 'original operating conditions disagreement')
        for s in left['conditions']['scenarios'].keys() & right['conditions']['scenarios'].keys():
            require(left['conditions']['scenarios'][s] == right['conditions']['scenarios'][s], 'scenario conditions disagreement')
    if current:
        require(a['source'] == b['source'], 'current producer source disagreement')
        x, y = (left, right) if kind == 'spot' else (left['edge'], right['edge'])
        for key in ('spec_sha256', 'protocol_sha256'):
            require(x[key] == y[key], 'current contract disagreement')


def baseline_equality(reference, actual, kind):
    """Six established groups plus fields the old helper excluded as annotations.

    Only opportunity_ledger is a new baseline instrumentation field. Existing
    research_identity, risk_calibration, subpools and original identifiers remain
    checked here; no clock, status, constraint or owner data is discarded.
    """
    before, after = old.evidence_fingerprints(reference, kind), old.evidence_fingerprints(actual, kind)
    differences = {k: {'reference_sha256': v, 'actual_sha256': after[k]}
                   for k, v in before.items() if after[k] != v}
    annotations = ('candidate', 'scenario', 'original_row_sha256', 'research_identity', 'risk_calibration', 'subpools')
    drift = {k: {'reference': reference.get(k), 'actual': actual.get(k)}
             for k in annotations if (k in reference) != (k in actual) or reference.get(k) != actual.get(k)}
    if drift:
        differences['original_annotations'] = drift
    return {'passed': not differences, 'reference': before, 'actual': after, 'differences': differences}


def approved_references(directory):
    """Trust pinned acceptance bytes and original raws, never a caller's passed flag."""
    directory = Path(directory)
    bound, documents = {}, {}
    for name, digest in PINNED_REFERENCES.items():
        body, actual = read_json(directory / name)
        require(actual == digest, 'approved reference bytes mismatch: ' + name)
        bound[str(directory / name)] = actual
        documents[name] = body
    inventory = documents['scratch/spot-canonical/canonical-inventory.json']
    bridge = documents['review/spot-canonical-five-financial-proof.json']
    result = {'spot': {}, 'perp': {}}
    singleton = documents['scratch/spot-singletons/atr-stop.json.gz']
    coin = documents['scratch/perp-singletons-retry1.json.gz']
    for kind, body in (('spot', singleton), ('perp', coin)):
        meta = body if kind == 'spot' else body['inputs']
        proof = old.verify_source(meta['source'], kind)
        require(meta['spec_sha256'] == proof['research/alpha_beta_spec.json'] and
                meta['protocol_sha256'] == proof['research/alpha-beta-PROTOCOL.md'], 'prior committed contract mismatch')
    for item in inventory['files']:
        if item['calibrated']:
            continue
        path = directory / 'scratch/spot-canonical' / item['path']
        body, digest = read_json(path)
        require(digest == item['sha256'] and digest in bridge['identities'].values(), 'canonical original raw/bridge mismatch')
        require(body['source'] == bridge['canonical_source'], 'canonical bridge source mismatch')
        old.verify_source(body['source'], 'spot')
        row = body['results']['atr-stop-' + item['scenario']]
        require(old.evidence_fingerprints(singleton['results']['atr-stop-' + item['scenario']], 'spot') == old.evidence_fingerprints(row, 'spot'), 'canonical singleton bridge mismatch')
        result['spot'][item['scenario']] = {'bundle': body, 'row': row, 'raw_sha256': digest}
        bound[str(path)] = digest
    require(set(result['spot']) == set(STRESSES['spot']), 'approved canonical stress inventory incomplete')
    require(result['spot']['base']['raw_sha256'] == 'dc94a7b315ee8ea11cc7215cd8ff7c02a8ab94180499b2c4ebf7fe1448682323', 'wrong current Spot baseline')
    for scenario in STRESSES['perp']:
        result['perp'][scenario] = {'bundle': coin, 'row': coin['results']['incumbent'][scenario],
                                    'raw_sha256': PINNED_REFERENCES['scratch/perp-singletons-retry1.json.gz']}
    return result, bound


def monetary_audit(row, kind, fx):
    """Reconstruct the funded wallet from original fills/income, independently of passed."""
    initial = decimal(row['initial_cny']) / fx(START) * D('.999')
    if kind == 'spot':
        fills = []
        for fill in row['fills']:
            require(type(fill['buyer']) is bool and fill['commission_asset'] in ('BTC', 'USDT'), 'invalid spot fill')
            trade = dict(fill, **{k: decimal(fill[k]) for k in ('qty', 'quote', 'price', 'commission')})
            require(all(trade[k] >= 0 for k in ('qty', 'quote', 'price', 'commission')), 'negative fill values')
            fills.append(trade)
        proof = spot_audit(SimpleNamespace(initial_cash=initial, cash=decimal(row['cash_usdt']), btc=decimal(row['btc']), fills=fills))
        require(proof == row['audit'] and proof['passed'], 'rederived Spot audit mismatch')
    else:
        # Same original average-cost accounting, without importing a sibling runtime.
        qty, entry, realized = D(0), D(0), D(0)
        identities = set()
        for trade in row['trades']:
            require(trade['id'] not in identities and trade['side'] in ('BUY', 'SELL'), 'duplicate/invalid Coin fill')
            identities.add(trade['id'])
            part, price = decimal(trade['qty']), decimal(trade['price'])
            require(part > 0 and price > 0, 'invalid Coin fill money')
            signed = part if trade['side'] == 'BUY' else -part
            if not qty or qty * signed > 0:
                entry = (abs(qty) * entry + part * price) / (abs(qty) + part)
            else:
                realized += min(abs(qty), part) * (price - entry) * (1 if qty > 0 else -1)
                if part > abs(qty):
                    entry = price
            qty += signed
            if not qty:
                entry = D(0)
        totals = Counter()
        for income in row['funding_ledger']:
            require(type(income['time']) is int and START <= income['time'] < END, 'income outside exclusive window')
            totals[income['incomeType']] += decimal(income['income'])
        require(set(totals) <= {'REALIZED_PNL', 'COMMISSION', 'INSURANCE_CLEAR', 'FUNDING_FEE'}, 'external cash flows')
        wallet = initial + sum(totals.values(), D(0))
        near = lambda a, b: abs(a - b) <= D('1e-8')
        require(near(qty, decimal(row['position'])) and near(realized, totals['REALIZED_PNL']) and
                near(decimal(row['fees']), -totals['COMMISSION'] - totals['INSURANCE_CLEAR']) and
                near(decimal(row['funding']), -totals['FUNDING_FEE']), 'rederived Coin fill/income mismatch')
        equity = wallet + (qty * (decimal(row['final_mark']) - entry) if qty else 0)
        require(near(equity, decimal(row['final_usdt'])) and
                near(wallet, decimal(row['audit']['wallet_from_ledger_usdt'])) and
                all(row['audit']['checks'].values()), 'rederived Coin terminal mismatch')
        proof = {'passed': True, 'wallet_usdt': str(wallet), 'position_btc': str(qty), 'entry_usdt': str(entry)}
    require(abs(decimal(row['final_usdt']) * fx(END) * D('.999') - decimal(row['final_cny'])) <= D('.000001'), 'terminal CNY conversion mismatch')
    return proof


def es99(returns):
    values = [decimal(v) for v in returns]
    require(bool(values), 'ES99 needs returns')
    count = (len(values) + 99) // 100
    return float(-sum(sorted(values)[:count], D(0)) / count)


def curve_metrics(row, curve, kind, bars, fx, usd_market, cny_market):
    initial = float(decimal(row['initial_cny']))
    metrics = old.financial(row, curve, bars, fx, usd_market, cny_market, initial, kind=kind)
    _, returns = daily_metrics([p['equity_cny'] for p in curve], initial)
    metrics['fill_count'] = len(row.get('fills', row.get('trades', [])))
    metrics['closing_gross_exposure_over_equity'] = curve[-1]['gross_exposure_over_equity']
    metrics['closing_signed_exposure_over_equity'] = (1 if curve[-1]['net_btc'] >= 0 else -1) * curve[-1]['gross_exposure_over_equity']
    metrics['metrics']['daily_es99_loss'] = es99(returns)
    metrics['metrics'].pop('daily_es5_loss', None)
    for key in ('cny', 'usdt'):
        stats = metrics['validation_2022_plus'][key]
        start = (CUTOFF - START) // DAY
        vals = [p['equity_' + key] for p in curve]
        stats['daily_es99_loss'] = es99(daily_metrics(vals[start:], vals[start - 1])[1])
        stats.pop('daily_es5_loss', None)
    metrics['usdt_metrics'].pop('daily_es5_loss', None)
    metrics['registered_account_usdt_cagr'] = (curve[-1]['equity_usdt'] / float(decimal(row['initial_cny']) / fx(START) * D('.999'))) ** (
        metrics['annualization']['account_year_days'] * DAY / (END - START)) - 1
    return metrics


def validate_profile_document(document, kind, names):
    require(set(document) == {'format', 'project_kind', 'baseline_candidate', 'cutoff_ms', 'spec_sha256', 'profiles'}, 'calibration document schema')
    require(type(document['format']) is int and document['format'] == 1 and document['project_kind'] == kind and
            document['baseline_candidate'] == BASE[kind] and type(document['cutoff_ms']) is int and
            document['cutoff_ms'] == CUTOFF and document['spec_sha256'] == SPEC_HASH[kind] and
            set(document['profiles']) == set(names), 'calibration identity/inventory')
    fields = {'candidate', 'project_kind', 'scale', 'base_bundle_sha256', 'baseline_candidate',
              'effective_from_ms', 'calibration_end_ms', 'training_end_day_exclusive'}
    for name, profile in document['profiles'].items():
        require(set(profile) == fields and profile['candidate'] == name and profile['project_kind'] == kind and
                profile['baseline_candidate'] == BASE[kind] and type(profile['scale']) is str and
                0 <= decimal(profile['scale']) <= 1 and old.hash_value(profile['base_bundle_sha256']) and
                profile['training_end_day_exclusive'] == '2022-01-01' and
                all(type(profile[k]) is int and profile[k] == CUTOFF for k in ('effective_from_ms', 'calibration_end_ms')) and
                (name != BASE[kind] or decimal(profile['scale']) == 1), 'invalid calibration profile')


def calibration_document(accounts, kind, market_returns, initial_usdt, combo=None):
    names = [BASE[kind], *ORDER[kind], *([combo] if combo else [])]
    baseline = accounts[account_id(kind, BASE[kind])]['curve']
    profiles, diagnostics = {}, {}
    expected_days = list(range(START, CUTOFF, DAY))
    require(len(expected_days) == 731 and len(market_returns) >= 731, '731 training returns required')
    for name in names:
        account = accounts[account_id(kind, name)]
        require(account['status'] == 'complete', 'calibration requires complete actual unscaled base')
        curve = account['curve']
        for values in (curve, baseline):
            require([p['day_ms'] for p in values if p['day_ms'] < CUTOFF] == expected_days, 'exact 731-day training coverage required')
        scale, stats = old.calibrate(curve[:731], baseline[:731], market_returns[:731], initial_usdt)
        require(all(v[k] is not None and math.isfinite(v[k]) for v in stats.values() for k in ('beta', 'volatility')), 'unidentifiable calibration risk')
        profiles[name] = dict(candidate=name, project_kind=kind, scale='1' if name == BASE[kind] else scale,
                              effective_from_ms=CUTOFF, calibration_end_ms=CUTOFF,
                              training_end_day_exclusive='2022-01-01', baseline_candidate=BASE[kind],
                              base_bundle_sha256=account['raw_sha256'])
        diagnostics[name] = dict(stats, source=account['source'], raw_sha256=account['raw_sha256'], training_days=731)
    document = dict(format=1, project_kind=kind, baseline_candidate=BASE[kind], cutoff_ms=CUTOFF,
                    spec_sha256=SPEC_HASH[kind], profiles=profiles)
    validate_profile_document(document, kind, names)
    return document, diagnostics


def risk_match(candidate, baseline, market_returns, initial_usdt):
    require([p['day_ms'] for p in candidate] == [p['day_ms'] for p in baseline] == list(range(START, END, DAY)), 'actual risk daily alignment')
    result = old.achieved_match(candidate, baseline, market_returns, initial_usdt)
    c, b = result['candidate'], result['baseline']
    identified = all(v[k] is not None and math.isfinite(v[k]) for v in (c, b) for k in ('volatility', 'beta'))
    result['achieved_match'] = identified and decimal(c['volatility']) <= decimal(b['volatility']) * D('1.05') and decimal(c['beta']) <= decimal(b['beta']) + D('.02')
    return result


def adoption_gates(kind, candidate, baseline, achieved, actual_candidate_mdd, actual_baseline_mdd, equality):
    """Pure exact threshold checks; inputs are independently recomputable metrics."""
    require(set(candidate) == set(baseline) == set(STRESSES[kind]), 'matched four stresses required')
    c, b = candidate['base'], baseline['base']
    checks = {'baseline_equality': equality is True}
    for scenario in STRESSES[kind]:
        x, y = candidate[scenario], baseline[scenario]
        checks[scenario + ':cagr'] = decimal(x['cagr']) >= decimal(y['cagr']) - D('.01')
        checks[scenario + ':mdd'] = (decimal(x['mdd']) <= decimal(y['mdd']) if kind == 'spot' else decimal(x['mdd']) < D('.50'))
    checks['base_improvement'] = decimal(c['cagr']) >= decimal(b['cagr']) + D('.01') or (
        decimal(c['mdd']) <= decimal(b['mdd']) - D('.01') and decimal(c['cagr']) >= decimal(b['cagr']) - D('.01'))
    checks['worst_cny_day'] = decimal(c['worst_day']) >= decimal(b['worst_day']) - D('.005')
    checks['es99'] = decimal(c['es99']) <= decimal(b['es99']) * D('1.05')
    checks['underwater'] = decimal(c['underwater']) <= decimal(b['underwater'])
    checks['actual_risk_bands'] = achieved.get('achieved_match') is True
    gain = decimal(achieved['validation_total_usdt_return_gain'])
    checks['actual_validation'] = gain > 0 or (decimal(actual_candidate_mdd) <= decimal(actual_baseline_mdd) - D('.01') and gain >= D('-.01'))
    return {'eligible': all(checks.values()), 'checks': checks,
            'reasons': [key for key, passed in checks.items() if not passed],
            'worst_stress_cagr': str(min(decimal(r['cagr']) for r in candidate.values()))}


def combo_name(kind, parts):
    require(len(parts) >= 2 and parts == [n for n in ORDER[kind] if n in parts], 'all registered ordered eligible components required')
    return 'combo' if kind == 'spot' else '+'.join(parts)


def choose_singles(kind, decisions):
    require(set(decisions) == set(ORDER[kind]), 'every registered single needs a decision')
    eligible = [name for name in ORDER[kind] if decisions[name].get('eligible') is True]
    best = min(eligible, key=lambda n: (-decimal(decisions[n]['worst_stress_cagr']), ORDER[kind].index(n))) if eligible else BASE[kind]
    return best, eligible if len(eligible) >= 2 else []


def aggregate_pair(spot, perp, budgets, market_returns, fx, candidates=None):
    candidates = BASE if candidates is None else candidates
    require(tuple(budgets) in ((2500, 7500), (5000, 5000), (7500, 2500)) and sum(budgets) == 10000, 'fixed CNY10000 budgets required')
    for account, budget, kind in zip((spot, perp), budgets, ('spot', 'perp')):
        require(account['candidate'] == candidates[kind], 'wrong budget candidate')
        require(account['status'] == 'complete' and account['kind'] == kind and
                decimal(account['capital']) == budget and account['offset'] == 0 and
                account['scenario'] == 'base' and not account['risk'], 'actual funded base budget account required')
        require(account['row']['audit']['passed'] is True and decimal(account['row']['initial_cny']) == budget, 'wrong raw budget capital')
    a, b = spot['curve'], perp['curve']
    require([p['day_ms'] for p in a] == [p['day_ms'] for p in b] == list(range(START, END, DAY)), 'full synchronized budget daily alignment required')
    curve = []
    for x, y in zip(a, b):
        equity = x['equity_usdt'] + y['equity_usdt']
        gross = abs(x['net_btc']) * x['price_usdt'] + abs(y['net_btc']) * y['price_usdt']
        signed = x['net_btc'] * x['price_usdt'] + y['net_btc'] * y['price_usdt']
        curve.append(dict(day_ms=x['day_ms'], equity_cny=x['equity_cny'] + y['equity_cny'],
                          equity_usdt=equity, absolute_btc_notional_usdt=gross,
                          gross_exposure_over_equity=gross / equity, signed_exposure_over_equity=signed / equity))
    cny, returns = daily_metrics([p['equity_cny'] for p in curve], 10000)
    cny.pop('daily_es5_loss', None)
    cny['daily_es99_loss'] = es99(returns)
    initial = float(D(10000) / fx(START) * D('.999'))
    usd, usd_returns = daily_metrics([p['equity_usdt'] for p in curve], initial)
    usd.pop('daily_es5_loss', None)
    usd['final_usdt'] = usd.pop('final_cny')
    ra = old.returns_for(a, float(D(budgets[0]) / fx(START) * D('.999')))
    rb = old.returns_for(b, float(D(budgets[1]) / fx(START) * D('.999')))
    correlation = statistics.correlation(ra, rb) if len(set(ra)) > 1 and len(set(rb)) > 1 else None
    return dict(budgets=budgets, candidates=[spot['candidate'], perp['candidate']], initial_cny=10000,
                cash_flows=0, daily_cny=cny, daily_usdt=usd, regression=old.safe_regression(usd_returns, market_returns),
                account_return_correlation=correlation, curve=curve,
                closing_gross_exposure_over_equity=curve[-1]['gross_exposure_over_equity'],
                account_raw_sha256=[spot['raw_sha256'], perp['raw_sha256']],
                fees_usdt=[v['metrics']['fees_usdt'] for v in (spot, perp)],
                funding_usdt=[v['metrics']['funding_paid_usdt'] for v in (spot, perp)],
                fill_counts=[len(v['row'].get('fills', v['row'].get('trades', []))) for v in (spot, perp)],
                neutral_reference=budgets == (5000, 5000),
                limitations='Actual independently funded accounts retain fees, minimum notionals and rounding; budget-sensitive paths. Daily joint MDD is not independently measured continuous joint MDD. Other splits are robustness, never an optimized recommendation.')




def verify_coin_timing(row):
    starts = [s['start_ms'] for s in row['sessions']]
    for index, session in enumerate(row['sessions']):
        require(session['index'] == index and session['status'] in ('executed', 'no_action', 'unknown') and
                type(session['cycles']) is int and session['cycles'] >= 0, 'invalid original Coin session')
    for event in row['opportunity_ledger']:
        if event['event'] not in ('decision', 'edge_predecision', 'write_attempt'):
            continue
        stamp = event['at_ms']
        require(type(stamp) is int, 'invalid Coin journal clock')
        index = bisect.bisect_right(starts, stamp) - 1
        require(index >= 0 and stamp <= starts[index] + 300000, 'Coin decision outside original session')
        if event['event'] == 'write_attempt':
            require(stamp < starts[index] + 300000, 'Coin write attempt outside original deadline')
        if 'completed_at_ms' in event:
            require(type(event['completed_at_ms']) is int and event['completed_at_ms'] >= stamp, 'Coin completion clock precedes decision')

def verify_consumed_files(bundle, env, checked):
    """Verify retained official bytes/CHECKSUMs, not a cache manifest's assertion."""
    receipts = bundle['edge']['print_restore_receipts']
    for field, root in (('loaded_minute_files', Path(env['coin_market'])),
                        ('loaded_print_files', Path(env['coin_prints']))):
        for name, digest in bundle['inputs'][field].items():
            path = (root / name).resolve()
            require(path.is_relative_to(root.resolve()), 'consumed input escapes root')
            checksum = path.with_suffix(path.suffix + '.CHECKSUM')
            if str(path) not in checked:
                fields = checksum.read_text().split()
                require(len(fields) == 2 and fields[1].lstrip('*') == path.name and
                        fields[0] == digest == sha(path), 'consumed official ZIP/CHECKSUM mismatch')
                checked[str(path)], checked[str(checksum)] = digest, sha(checksum)
            require(checked[str(path)] == digest, 'conflicting consumed market bytes')
            if field == 'loaded_print_files':
                matches = [r for r in receipts if r['name'] == name]
                require(matches and all(r['sha256'] == digest and
                    r['checksum_sha256'] == checked[str(checksum)] and
                    old.hash_value(r['derived_binary_sha256']) for r in matches), 'missing/invalid derived tape receipt')


def audit_daily(row, daily, kind, fx):
    """Audit original snapshots and every UTC close; only verified eventless flat carry is legal."""
    cash = decimal(row['initial_cny']) / fx(START) * D('.999')
    qty, entry = D(0), D(0)
    fills = row['fills'] if kind == 'spot' else row['trades']
    incomes = [] if kind == 'spot' else row['funding_ledger']
    for events in (fills, incomes):
        require([r['time'] for r in events] == sorted(r['time'] for r in events), 'cash/fill events out of order')
    key = 'timestamp_ms' if kind == 'spot' else 'stamp_ms'
    stamps = [point[key] for point in daily]
    require(stamps == sorted(set(stamps)) and all(type(v) is int and START <= v <= END for v in stamps),
            'daily timestamps duplicate/unordered/outside window')
    snapshots = {point[key]: point for point in daily}
    times = sorted(set(stamps) | set(range(START + DAY, END + DAY, DAY)))
    fi, ii = 0, 0
    verified_cash, verified_qty, verified_events = cash, qty, (0, 0)
    near = lambda a, b: abs(decimal(a) - decimal(b)) <= D('1e-8')
    for stamp in times:
        point = snapshots.get(stamp)
        # Original Coin hook recaptures after boundary funding, before the
        # next minute's same-stamp print fills. END funding is excluded above.
        boundary = stamp - (1 if kind == 'perp' and stamp % DAY == 0 else 0)
        while fi < len(fills) and fills[fi]['time'] <= boundary:
            trade = fills[fi]
            part, price = decimal(trade['qty']), decimal(trade['price'])
            buy = trade['buyer'] if kind == 'spot' else trade['side'] == 'BUY'
            signed = part if buy else -part
            if kind == 'spot':
                fee = decimal(trade['commission'])
                cash -= (decimal(trade['quote']) if buy else -decimal(trade['quote'])) + (fee if trade['commission_asset'] == 'USDT' else 0)
                qty += signed - (fee if trade['commission_asset'] == 'BTC' else 0)
            else:
                if not qty or qty * signed > 0:
                    entry = (abs(qty) * entry + part * price) / (abs(qty) + part)
                elif part > abs(qty):
                    entry = price
                qty += signed
                if not qty:
                    entry = D(0)
            fi += 1
        while ii < len(incomes) and (incomes[ii]['time'] <= boundary or (incomes[ii]['time'] == stamp and incomes[ii]['incomeType'] == 'FUNDING_FEE')):
            cash += decimal(incomes[ii]['income'])
            ii += 1
        if point is None:
            require(qty == verified_qty == 0 and cash == verified_cash and
                    (fi, ii) == verified_events,
                    'missing daily snapshot for held exposure or unreflected cash/fill events at ' + str(stamp))
            continue
        price = decimal(point['price_usdt' if kind == 'spot' else 'mark_usdt'])
        equity = cash + qty * (price if kind == 'spot' else price - entry)
        require(near(cash, point['cash_usdt' if kind == 'spot' else 'wallet_usdt']) and
                near(qty, point['btc' if kind == 'spot' else 'quantity_btc']) and
                near(equity, point['equity_usdt']) and
                near(equity * fx(stamp) * D('.999'), point['equity_cny']), 'daily monetary reconstruction mismatch at ' + str(stamp))
        verified_cash, verified_qty, verified_events = cash, qty, (fi, ii)

def original_curve(row, bars, fx, kind):
    raw = list(row['daily'].values()) if isinstance(row['daily'], dict) else row['daily']
    audit_daily(row, raw, kind, fx)
    curve = canonical(row, bars, fx, float(decimal(row['initial_cny'])), kind)
    # Perpetual exposure uses its actual closing mark, not the Spot BTC comparator.
    if kind == 'perp':
        daily = {p['stamp_ms']: p for p in raw}
        for point in curve:
            if point['net_btc']:
                record = daily[point['day_ms'] + DAY]
                mark = decimal(record['mark_usdt'])
                require(mark > 0 and abs(decimal(record['quantity_btc']) * mark) == decimal(record['gross_btc_exposure_usdt']), 'perp daily exposure mismatch')
                point['price_usdt'] = float(mark)
                point['gross_exposure_over_equity'] = float(decimal(record['gross_btc_exposure_usdt']) / decimal(record['equity_usdt']))
    return curve


def verify_account_binding(account, documents):
    kind, name, bundle = account['kind'], account['candidate'], account['bundle']
    binding = bundle if kind == 'spot' else bundle['edge']
    digest = binding['risk_calibration_sha256']
    if digest is None:
        expected = {'scale': '1', 'sha256': None} if kind == 'spot' else {}
        require(binding['risk_calibration' if kind == 'spot' else 'risk_profiles'] == expected, 'unscaled profile mismatch')
        if kind == 'perp':
            require(binding['risk_calibration_document'] is None, 'undeclared risk document')
        profile = expected
    else:
        require(digest in documents[kind], 'missing exact project calibration bytes')
        doc = documents[kind][digest]['body']
        profile = doc['profiles'][name]
        if kind == 'spot':
            profile = dict(profile, sha256=digest)
            require(binding['risk_calibration'] == profile, 'Spot actual calibration/profile binding mismatch')
        else:
            require(binding['risk_profiles'] == doc['profiles'] and
                    binding['risk_calibration_document'].encode() == documents[kind][digest]['bytes'], 'Coin exact calibration bytes/profile mismatch')
    if kind == 'spot' and name != BASE[kind]:
        row = account['row']
        require(row['risk_calibration'] == profile, 'row profile mismatch')
        identity = dict(candidate=name, components=account['components'], project_kind=kind,
                        spec_sha256=SPEC_HASH[kind], risk_calibration=profile,
                        execution_source_sha256=account['source']['python_sources_sha256'],
                        features_sha256=FEATURE_HASH if 'crowding-interaction' in account['components'] else None)
        require(row['research_identity'] == identity, 'row research source/profile/features mismatch')
        for event in row['opportunity_ledger']:
            if event['event'] == 'decision':
                for diag in event['diagnostics']:
                    if diag['mechanism'] == 'new-buy':
                        expected_scale = decimal(profile['scale']) if event['decision_ms'] >= CUTOFF else D(1)
                        require(decimal(diag['risk_scale']) == expected_scale, 'new-order calibration clock/scale mismatch')
    if kind == 'perp' and account['components']:
        identity = dict(candidate=name, components=account['components'], binding={k: v for k, v in binding.items() if k not in (
            'candidates', 'original_inputs', 'original_conditions', 'decision_coverage', 'print_restore_receipts')},
            features_sha256=FEATURE_HASH, profile=profile, spec_sha256=SPEC_HASH[kind], protocol_sha256=PROTOCOL_HASH[kind])
        expected = old.checksum(identity)
        for event in account['row']['opportunity_ledger']:
            if event['event'] == 'edge_predecision':
                require(event['binding'] == expected, 'Coin decision source/profile binding mismatch')


def ingest(paths, env, bars, fx, market_returns, cny_returns, documents):
    accounts, files, rejected, cache, consumed = {}, {}, [], {}, {}
    for kind, path in paths.items():
        if path is None:
            continue
        children, bindings = load_files(path)
        files.update(bindings)
        for child, digest, bundle in children:
            try:
                rows, binding, source, offset = envelope(bundle, kind, env, cache)
                if kind == 'perp':
                    verify_consumed_files(bundle, env, consumed)
            except (ValueError, KeyError, TypeError, ArithmeticError, OSError, subprocess.CalledProcessError) as exc:
                rejected.append(dict(path=str(child), raw_sha256=digest, kind=kind, reason=str(exc)))
                continue
            for name, scenario, row in rows:
                capital, risk = str(decimal(row['initial_cny'])), binding['risk_calibration_sha256'] is not None
                key = account_id(kind, name, scenario, capital, offset, risk)
                require(key not in accounts, 'duplicate account identity: ' + key)
                parts = components(kind, name, bundle['edge']['components'] if kind == 'spot' else bundle['edge']['candidates'][name])
                account = dict(kind=kind, candidate=name, scenario=scenario, capital=capital, offset=offset,
                               risk=risk, components=parts, path=str(child), raw_sha256=digest,
                               source=source, bundle=bundle, row=row, status='rejected', reasons=[])
                accounts[key] = account
                try:
                    require(scenario in STRESSES[kind] and decimal(capital) in (2500, 5000, 7500, 10000), 'unregistered scenario/capital')
                    require(not offset or (kind == 'perp' and name == BASE[kind] and scenario == 'base' and decimal(capital) == 10000 and not risk), 'unregistered offset account')
                    require(decimal(capital) == 10000 or (scenario == 'base' and not risk and not offset), 'unregistered budget role')
                    require(not risk or (name not in CONTROLS and scenario == 'base' and decimal(capital) == 10000 and not offset), 'unregistered calibrated role')
                    verify_account_binding(account, documents)
                    reasons = old.row_validity(row, kind, [s + offset for s in env['starts']], capital, scenario)
                    if kind == 'perp':
                        verify_coin_timing(row)
                    account['reasons'] = reasons
                    account['status'] = 'pending' if reasons else 'complete'
                    if not reasons:
                        account['monetary_audit'] = monetary_audit(row, kind, fx)
                        account['curve'] = original_curve(row, bars, fx, kind)
                        account['metrics'] = curve_metrics(row, account['curve'], kind, bars, fx, market_returns, cny_returns)
                        account['gate_inputs'] = dict(raw_sha256=digest, values=gate_values(account))
                except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    account['status'] = 'rejected'
                    account['reasons'].append(str(exc))
    files.update(consumed)
    return accounts, files, rejected


def gate_values(account):
    """One exact raw-bound gate input contract for decisions and proof comparison."""
    bound = account.get('gate_inputs')
    if bound is not None:
        require(type(bound) is dict, 'exact gate input binding type')
    if 'row' in account:
        m = account['metrics']['metrics']
        values = dict(cagr=account['row']['cagr'], mdd=account['row']['mdd'], worst_day=m['worst_day'],
                      es99=m['daily_es99_loss'], underwater=m['longest_daily_underwater_days'])
        if bound is not None:
            require(bound.get('values') == values, 'retained gate inputs differ from original row/statistics')
    else:
        require(type(bound) is dict, 'missing exact original gate inputs')
        values = bound.get('values')
    if bound is not None:
        require(set(bound) == {'raw_sha256', 'values'} and bound['raw_sha256'] == account['raw_sha256'] and
                old.hash_value(bound['raw_sha256']), 'exact gate raw binding mismatch')
    require(type(values) is dict and set(values) == {'cagr', 'mdd', 'worst_day', 'es99', 'underwater'}, 'exact gate input fields')
    require(type(values['mdd']) is str and 0 <= decimal(values['mdd']) <= 1 and
            all(type(values[k]) in (int, float) for k in ('cagr', 'worst_day', 'es99')) and
            type(values['underwater']) is int and values['underwater'] >= 0, 'exact gate input types/range')
    for value in values.values():
        decimal(value)
    return values


def decisions_for(accounts, kind, names, equality, market_returns, initial_usdt):
    decisions = {}
    for name in names:
        keys = [account_id(kind, n, scenario) for n in (BASE[kind], name) for scenario in STRESSES[kind]]
        keys += [account_id(kind, n, risk=True) for n in (BASE[kind], name)]
        missing = [k for k in keys if k not in accounts or accounts[k]['status'] != 'complete']
        if missing:
            decisions[name] = dict(eligible=False, status='pending', required_accounts=missing)
            continue
        actual, control = (accounts[account_id(kind, n, risk=True)] for n in (name, BASE[kind]))
        achieved = risk_match(actual['curve'], control['curve'], market_returns, initial_usdt)
        outcome = adoption_gates(kind,
            {s: gate_values(accounts[account_id(kind, name, s)]) for s in STRESSES[kind]},
            {s: gate_values(accounts[account_id(kind, BASE[kind], s)]) for s in STRESSES[kind]},
            achieved, gate_values(actual)['mdd'], gate_values(control)['mdd'], equality)
        decisions[name] = dict(outcome, status='complete', achieved_risk=achieved)
    return decisions


def inventory_binding(report):
    """Stable cycle-free review target, independent of phase/output paths."""
    return {'inputs': report['inputs'], 'analysis_source': report['analysis_source'],
            'contracts': report['contracts'], 'accounts': {
                k: {f: v[f] for f in ('raw_sha256', 'source', 'status', 'reasons', 'components')}
                for k, v in report['accounts'].items()},
            'account_evidence_sha256': {k: old.checksum(v) for k, v in report['accounts'].items()},
            'portfolio_evidence_sha256': old.checksum(report['portfolios']),
            'input_envelopes_sha256': old.checksum(report['input_envelopes']),
            'calibration_input_documents': report['calibration_input_documents'],
            'calibration_diagnostics_sha256': old.checksum(report['calibration_diagnostics']),
            'environment_sha256': old.checksum(report['environment']),
            'combinations': report['combinations'],
            'required_accounts': report['required_accounts'], 'selected': report['selected'],
            'calibration_documents': report['calibration_documents'],
            'decisions': report['decisions'], 'baseline_equality': report['baseline_equality']}


def review_expectations(report):
    """Derive the exact review coverage and typed values from completed evidence.

    Artifacts contain a list of {id, raw_bindings, values, matches:true} records.
    Independent reviewers must recompute values from originals; this function is
    the comparison contract, not evidence that an independent review occurred.
    Hashes of daily curves bind every value, not just terminal money or a flag.
    """
    categories = ('source_and_inputs', 'original_accounting', 'baseline_six_groups',
                  'calibration_and_actual_risk', 'adoption_gates', 'actual_budget_aggregation')
    result = {name: {} for name in categories}
    accounts = report['accounts']
    required = required_matrix(report['selected'], report['combinations'])
    require(set(report['required_accounts']) == set(accounts) == required and
            len(report['required_accounts']) == len(required) and not report['pending'] and not report['blocking'],
            'review requires exact complete applicable account inventory')

    def add(category, identity, raw, **values):
        require(identity not in result[category], 'duplicate expected review identity')
        require(raw and all(old.hash_value(v) for v in raw.values()), 'review original raw SHA required')
        result[category][identity] = dict(id=identity, raw_bindings=raw, values=values, matches=True)

    def numeric(values):
        for value in values.values():
            decimal(value)
        return values

    files = {}
    for key, account in accounts.items():
        require(account['status'] == 'complete' and not account['reasons'], 'review account not complete')
        require(key == account_id(account['kind'], account['candidate'], account['scenario'], account['capital'], account['offset'], account['risk']), 'review account identity mismatch')
        path, digest = account['path'], account['raw_sha256']
        require(report['inputs'][path] == digest, 'review raw input mismatch')
        if path in files:
            require(files[path] == (account['kind'], digest, account['source']), 'review raw/source conflict')
        files[path] = (account['kind'], digest, account['source'])
        add('source_and_inputs', 'account:' + key, {key: digest},
            project_kind=account['kind'], candidate=account['candidate'], scenario=account['scenario'],
            initial_cny=account['capital'], start_offset_ms=account['offset'], calibrated=account['risk'],
            components=account['components'], source=account['source'], original_file=path)
        curve, m = account['curve'], account['metrics']
        require([p['day_ms'] for p in curve] == list(range(START, END, DAY)), 'review daily coverage incomplete')
        for point in curve:
            numeric({k: point[k] for k in ('equity_cny', 'equity_usdt', 'net_btc', 'price_usdt')})
        money = numeric(dict(initial_cny=account['capital'], final_cny=curve[-1]['equity_cny'],
            final_usdt=curve[-1]['equity_usdt'], fees_usdt=m['fees_usdt'], funding_paid_usdt=m['funding_paid_usdt'],
            fill_count=m['fill_count'], continuous_proxy_mdd=gate_values(account)['mdd'],
            cny_cagr=m['registered_account_cagr'], usdt_cagr=m['registered_account_usdt_cagr']))
        require(account['monetary_audit']['passed'] is True, 'review monetary audit failed')
        add('original_accounting', key, {key: digest}, money=money, gate_inputs=account['gate_inputs'],
            daily_days=len(curve), daily_curve_sha256=old.checksum(curve),
            ledger_reconstruction_sha256=old.checksum(account['monetary_audit']),
            daily_cny_metrics=numeric(m['metrics']), daily_usdt_metrics=numeric(m['usdt_metrics']))
    require(set(files) == set(report['input_envelopes']), 'review original envelope coverage mismatch')
    for path, (kind, digest, source) in files.items():
        envelope = report['input_envelopes'][path]
        meta, edge = (envelope, envelope) if kind == 'spot' else (envelope['inputs'], envelope['edge'])
        require(meta['source'] == source and report['contracts'][kind] == dict(spec_sha256=SPEC_HASH[kind], protocol_sha256=PROTOCOL_HASH[kind]), 'review source/contract mismatch')
        add('source_and_inputs', 'raw:' + path, {path: digest}, project_kind=kind, source=source,
            spec_sha256=edge['spec_sha256'], protocol_sha256=edge['protocol_sha256'],
            fx_sha256=meta['fx_sha256'], schedule_sha256=meta['schedule_sha256'],
            market_sha256=meta['market_sha256'] if kind == 'spot' else old.checksum(meta['market_identity']),
            feature_sha256=(edge['feature_sha256'] if kind == 'spot' else edge['feature_file_sha256']) or 'not_supplied',
            original_envelope_sha256=old.checksum(envelope),
            all_input_bytes_sha256=old.checksum(report['inputs']))
    groups = {'financial', 'fills', 'daily', 'ownership', 'operating', 'remaining_original_fields'}
    for kind in BASE:
        for scenario in (*STRESSES[kind], 'actual_unity'):
            comparison = report['baseline_equality'][kind][scenario]
            actual_id = account_id(kind, BASE[kind], 'base' if scenario == 'actual_unity' else scenario, risk=scenario == 'actual_unity')
            require(comparison['passed'] is True and not comparison['differences'] and
                    set(comparison['reference']) == set(comparison['actual']) == groups and
                    comparison['reference'] == comparison['actual'] and
                    comparison['actual_raw_sha256'] == accounts[actual_id]['raw_sha256'], 'review baseline consistency failed')
            add('baseline_six_groups', kind + ':' + scenario,
                {'reference': comparison['reference_raw_sha256'], actual_id: comparison['actual_raw_sha256']},
                reference_groups=comparison['reference'], actual_groups=comparison['actual'])
    for label, document in report['calibration_documents'].items():
        kind = document['project_kind']
        names = [BASE[kind], *ORDER[kind]]
        if label.endswith('_combo'):
            names.append(combo_name(kind, report['combinations'][kind]))
        else:
            require(label == kind, 'review foreign calibration project')
        validate_profile_document(document, kind, names)
        matching_bytes = sorted(h for h, body in report['calibration_input_documents'][kind].items() if body == document)
        require(matching_bytes, 'review exact calibration bytes missing')
        for name, profile in document['profiles'].items():
            base_id = account_id(kind, name)
            require(profile['base_bundle_sha256'] == accounts[base_id]['raw_sha256'], 'review calibration base raw mismatch')
            stats = report['calibration_diagnostics'][label][name]
            require(stats['training_days'] == 731 and stats['raw_sha256'] == profile['base_bundle_sha256'], 'review training binding mismatch')
            numeric(stats['candidate']); numeric(stats['baseline'])
            add('calibration_and_actual_risk', 'profile:' + label + ':' + name,
                {base_id: profile['base_bundle_sha256']}, profile=profile, document_sha256=matching_bytes,
                training_days=731, training_candidate=stats['candidate'], training_baseline=stats['baseline'], source=stats['source'])
    require(set(report['calibration_documents']) == set(BASE) | {k + '_combo' for k in report['combinations']}, 'review calibration project/combo coverage missing')
    for key, account in accounts.items():
        if not account['risk']:
            continue
        kind, name = account['kind'], account['candidate']
        envelope = report['input_envelopes'][account['path']]
        binding = envelope if kind == 'spot' else envelope['edge']
        digest = binding['risk_calibration_sha256']
        document = report['calibration_input_documents'][kind][digest]
        require(document in report['calibration_documents'].values(), 'review foreign actual calibration')
        profile = document['profiles'][name]
        control_id = account_id(kind, BASE[kind], risk=True)
        validation = account['metrics']['validation_2022_plus']
        prior = next(p for p in account['curve'] if p['day_ms'] == CUTOFF - DAY)
        add('calibration_and_actual_risk', 'actual:' + key,
            {key: account['raw_sha256'], 'unscaled_base': profile['base_bundle_sha256'], 'actual_unity': accounts[control_id]['raw_sha256']},
            document_sha256=digest, profile=profile, validation=numeric(dict(
                beta=validation['regression']['beta_btc'], volatility=validation['usdt']['daily_volatility_annualized'],
                total_usdt_return=account['curve'][-1]['equity_usdt'] / prior['equity_usdt'] - 1,
                continuous_proxy_mdd=gate_values(account)['mdd'])))
    for kind in BASE:
        decisions = report['decisions'][kind]
        singles = {n: decisions[n] for n in ORDER[kind]}
        best, parts = choose_singles(kind, singles)
        require(report['combinations'].get(kind, []) == parts, 'review all-component combination mismatch')
        names = [*ORDER[kind], *([combo_name(kind, parts)] if parts else [])]
        require(set(decisions) == set(names), 'review candidate gate coverage mismatch')
        for name in names:
            decision = decisions[name]
            require(decision['status'] == 'complete' and type(decision['eligible']) is bool and
                    all(type(v) is bool for v in decision['checks'].values()), 'review gate result incomplete')
            def values(candidate):
                return {s: gate_values(accounts[account_id(kind, candidate, s)]) for s in STRESSES[kind]}
            actual = accounts[account_id(kind, name, risk=True)]
            control = accounts[account_id(kind, BASE[kind], risk=True)]
            achieved = decision['achieved_risk']
            statistics_pair, ratios = [], []
            for a in (actual, control):
                validation = a['metrics']['validation_2022_plus']
                statistics_pair.append(numeric(dict(volatility=validation['usdt']['daily_volatility_annualized'],
                    beta=validation['regression']['beta_btc'], days=(END - CUTOFF) // DAY)))
                prior = next(p for p in a['curve'] if p['day_ms'] == CUTOFF - DAY)
                ratios.append(a['curve'][-1]['equity_usdt'] / prior['equity_usdt'])
            c, b = statistics_pair
            matched = decimal(c['volatility']) <= decimal(b['volatility']) * D('1.05') and decimal(c['beta']) <= decimal(b['beta']) + D('.02')
            require(achieved['candidate'] == c and achieved['baseline'] == b and
                    achieved['achieved_match'] is matched and
                    achieved['validation_total_usdt_return_gain'] == ratios[0] - ratios[1], 'review actual risk bands/gain inconsistent')
            derived = adoption_gates(kind, values(name), values(BASE[kind]), decision['achieved_risk'],
                gate_values(actual)['mdd'], gate_values(control)['mdd'], True)
            require(all(decision[k] == v for k, v in derived.items()), 'review gates inconsistent with exact original inputs')
            keys = [account_id(kind, n, s) for n in (BASE[kind], name) for s in STRESSES[kind]]
            keys += [account_id(kind, n, risk=True) for n in (BASE[kind], name)]
            add('adoption_gates', kind + ':' + name, {k: accounts[k]['raw_sha256'] for k in keys},
                gate_inputs={k: accounts[k]['gate_inputs'] for k in keys}, result=decision)
        selected = combo_name(kind, parts) if parts and decisions[combo_name(kind, parts)]['eligible'] else best
        require(report['selected'][kind] == selected, 'review registered ranking/selection mismatch')
        ranked = sorted((n for n in ORDER[kind] if singles[n]['eligible']), key=lambda n: (-decimal(singles[n]['worst_stress_cagr']), ORDER[kind].index(n)))
        add('adoption_gates', kind + ':ranking', {n: accounts[account_id(kind, n)]['raw_sha256'] for n in names},
            registered_order=list(ORDER[kind]), ranked_eligible_singles=ranked, best_single=best,
            all_eligible_components=parts, selected=selected)
    selections = {'current_default': BASE}
    if report['selected'] != BASE:
        selections['selected'] = report['selected']
    require(set(report['portfolios']) == set(selections), 'review portfolio selection coverage mismatch')
    for label, selected in selections.items():
        portfolios = report['portfolios'][label]
        require(len(portfolios) == 3 and {tuple(p['budgets']) for p in portfolios} == {(2500, 7500), (5000, 5000), (7500, 2500)}, 'review fixed budget coverage mismatch')
        for portfolio in portfolios:
            budgets = portfolio['budgets']
            keys = [account_id(k, selected[k], capital=b) for k, b in zip(BASE, budgets)]
            require(portfolio['account_raw_sha256'] == [accounts[k]['raw_sha256'] for k in keys] and
                    portfolio['candidates'] == [selected[k] for k in BASE] and
                    portfolio['initial_cny'] == 10000 and portfolio['cash_flows'] == 0, 'review budget raw/capital mismatch')
            require([p['day_ms'] for p in portfolio['curve']] == list(range(START, END, DAY)), 'review aggregate daily coverage incomplete')
            for point, a, b in zip(portfolio['curve'], accounts[keys[0]]['curve'], accounts[keys[1]]['curve']):
                require(point['equity_cny'] == a['equity_cny'] + b['equity_cny'] and
                        point['equity_usdt'] == a['equity_usdt'] + b['equity_usdt'] and
                        point['absolute_btc_notional_usdt'] == abs(a['net_btc']) * a['price_usdt'] + abs(b['net_btc']) * b['price_usdt'], 'review actual budget sums mismatch')
            correlation = portfolio['account_return_correlation']
            add('actual_budget_aggregation', label + ':' + '/'.join(map(str, budgets)),
                {k: accounts[k]['raw_sha256'] for k in keys}, budgets=list(budgets), initial_cny=10000, cash_flows=0,
                daily_days=len(portfolio['curve']), daily_equity_exposure_sha256=old.checksum(portfolio['curve']),
                daily_cny_metrics=numeric(portfolio['daily_cny']), daily_usdt_metrics=numeric(portfolio['daily_usdt']),
                beta=decimal_string(portfolio['regression']['beta_btc']),
                correlation={'identifiable': False} if correlation is None else {'identifiable': True, 'value': decimal_string(correlation)},
                closing_gross_exposure=decimal_string(portfolio['closing_gross_exposure_over_equity']),
                fees_usdt=[decimal_string(v) for v in portfolio['fees_usdt']],
                funding_usdt=[decimal_string(v) for v in portfolio['funding_usdt']], fill_counts=portfolio['fill_counts'])
    return result


def decimal_string(value):
    return format(decimal(value).normalize(), 'f')


def verify_review_value(actual, expected):
    """Exact typed finite comparison; negative eligibility is a legitimate value."""
    require(expected is not None and type(actual) is type(expected), 'review value type/null mismatch')
    if isinstance(expected, dict):
        require(set(actual) == set(expected), 'review value fields missing/extra')
        for key in expected:
            verify_review_value(actual[key], expected[key])
    elif isinstance(expected, list):
        require(len(actual) == len(expected), 'review list coverage mismatch')
        for a, b in zip(actual, expected):
            verify_review_value(a, b)
    else:
        if type(expected) in (float, int):
            decimal(actual); decimal(expected)
        require(actual == expected, 'review recomputation mismatch')

def verify_financial_review(path, report):
    """A proof binds the completed preliminary report, inventory and review artifacts.

    Schema: format='btc-edge-financial-review-v1', preliminary={path,sha256},
    inventory_sha256, reviewer_source (clean full Spot Python proof), checks
    (all six named categories, each {artifact:{path,sha256},passed:true}).
    Artifacts have exactly inventory_sha256, category, recomputations (a list).
    Each record has id, raw_bindings, values, matches:true. review_expectations
    derives exact per-category coverage and typed finite values from the completed
    accounts, original documents, six groups, gates/ranking and actual portfolios.
    Booleans alone cannot qualify; correctly recomputed rejected candidates can.
    """
    proof, digest = read_json(path)
    required = {'source_and_inputs', 'original_accounting', 'baseline_six_groups',
                'calibration_and_actual_risk', 'adoption_gates', 'actual_budget_aggregation'}
    require(set(proof) == {'format', 'preliminary', 'inventory_sha256', 'reviewer_source', 'checks'} and
            proof['format'] == 'btc-edge-financial-review-v1' and set(proof['checks']) == required, 'financial review schema')
    prior_path = Path(path).parent / proof['preliminary']['path']
    prior, prior_hash = read_json(prior_path)
    require(prior_hash == proof['preliminary']['sha256'] and prior['phase'] == 'preliminary' and
            prior['status'] == 'complete_pending_independent_review' and not prior['pending'] and not prior['blocking'], 'review needs complete preliminary evidence')
    current = inventory_binding(report)
    require(inventory_binding(prior) == current and proof['inventory_sha256'] == old.checksum(current), 'financial review inventory/source mismatch')
    verify_source(proof['reviewer_source'], 'spot')
    expected = review_expectations(report)
    artifacts = {}
    for name, check in proof['checks'].items():
        require(set(check) == {'passed', 'artifact'} and check['passed'] is True and
                set(check['artifact']) == {'path', 'sha256'}, 'missing independent recomputation artifact')
        artifact_path = Path(path).parent / check['artifact']['path']
        artifact, actual = read_json(artifact_path)
        require(actual == check['artifact']['sha256'] and artifact.get('inventory_sha256') == proof['inventory_sha256'] and
                set(artifact) == {'inventory_sha256', 'category', 'recomputations'} and
                artifact['category'] == name and type(artifact['recomputations']) is list, 'financial review artifact schema/binding mismatch')
        seen = set()
        for record in artifact['recomputations']:
            require(type(record) is dict and set(record) == {'id', 'raw_bindings', 'values', 'matches'} and
                    type(record['id']) is str and record['id'] in expected[name] and record['id'] not in seen and
                    record['matches'] is True, 'review record unknown/duplicate/failed consistency')
            seen.add(record['id'])
            verify_review_value(record, expected[name][record['id']])
        require(seen == set(expected[name]), 'review recomputation coverage missing')
        artifacts[str(artifact_path)] = actual
    return {'path': str(path), 'sha256': digest, 'preliminary_sha256': prior_hash,
            'reviewer_source': proof['reviewer_source'], 'artifacts': artifacts}


def assess(args):
    source = old.source_identity()
    require(source['dirty'] is False, 'clean committed assessor source required')
    verify_source(source, 'spot')
    env = environment(args.schedule, args.fx, args.market, args.features)
    env.update(coin_market=str(args.coin_market), coin_prints=str(args.coin_prints))
    bars = old.load_daily(args.market, END, require_through=END)
    fx = old.PriorFX(args.fx)
    market_returns, cny_returns = old.market_returns_for(bars, fx)
    references, reference_files = approved_references(args.references)
    documents, document_bindings = {'spot': {}, 'perp': {}}, {}
    for kind in BASE:
        for path in getattr(args, kind + '_calibration') or []:
            doc, digest = read_json(path)
            require(Path(path).suffix != '.gz', 'producer calibration must be original JSON bytes')
            names = [BASE[kind], *ORDER[kind]]
            extra = set(doc['profiles']) - set(names)
            require(len(extra) <= 1, 'only one applicable all-component combo profile')
            for name in extra:
                components(kind, name, list(ORDER[kind]) if kind == 'spot' else None)
            validate_profile_document(doc, kind, [*names, *extra])
            require(digest not in documents[kind], 'duplicate calibration document')
            documents[kind][digest] = {'body': doc, 'bytes': Path(path).read_bytes(), 'path': str(path)}
            document_bindings[str(path)] = digest
    accounts, files, rejected = ingest({'spot': args.spot, 'perp': args.perp}, env, bars, fx, market_returns, cny_returns, documents)
    report = dict(format='btc-edge-assessment-v1', phase=args.phase, analysis_source=source,
                  contracts={k: {'spec_sha256': SPEC_HASH[k], 'protocol_sha256': PROTOCOL_HASH[k]} for k in BASE},
                  inputs=dict(files, **reference_files, **document_bindings,
                              **{str(p): sha(p) for p in (args.schedule, args.fx, args.features)}),
                  environment=env, accounts=accounts, rejected_files=rejected,
                  pending=[], blocking=[r['reason'] for r in rejected], baseline_equality={},
                  calibration_documents={}, calibration_diagnostics={},
                  calibration_input_documents={kind: {h: d['body'] for h, d in values.items()} for kind, values in documents.items()}, decisions={}, selected=dict(BASE), combinations={}, portfolios={},
                  native_cases=0, actual_account_days=0, native_qualification='NOT_QUALIFIED', prospective_alpha_proven=False,
                  limitations=['Continuous OHLC/minute/envelope MDD remains an original historical proxy, not independent continuous replay.',
                               '2022+ history is contaminated; HAC7 intervals and upper risk bands are descriptive.',
                               'Cash/protected participation are actual finite protected accounts, not pure buy-and-hold or maintained fractional weights.',
                               'No native execution or prospective alpha qualification; offsets are diagnostics, no schedule or capital optimization.'])
    equal = {}
    initial_usdt = float(D(10000) / fx(START) * D('.999'))
    for kind in BASE:
        equality = {}
        for scenario in STRESSES[kind]:
            key = account_id(kind, BASE[kind], scenario)
            account = accounts.get(key)
            if account and account['status'] == 'complete':
                try:
                    compare_inputs(account['bundle'], references[kind][scenario]['bundle'], kind)
                    equality[scenario] = dict(baseline_equality(references[kind][scenario]['row'], account['row'], kind),
                        reference_raw_sha256=references[kind][scenario]['raw_sha256'], actual_raw_sha256=account['raw_sha256'])
                    require(equality[scenario]['passed'], 'baseline fingerprint mismatch: ' + key)
                except (ValueError, KeyError, TypeError) as exc:
                    report['blocking'].append(str(exc))
            else:
                equality[scenario] = {'passed': False, 'pending': key}
        base_account = accounts.get(account_id(kind, BASE[kind]))
        if base_account and base_account['status'] == 'complete':
            for key, account in accounts.items():
                if account['kind'] != kind or account['status'] != 'complete':
                    continue
                try:
                    compare_inputs(account['bundle'], base_account['bundle'], kind, current=True)
                except (ValueError, KeyError, TypeError) as exc:
                    account['status'] = 'rejected'
                    account['reasons'].append(str(exc))
                    report['blocking'].append(key + ': ' + str(exc))
        unity = accounts.get(account_id(kind, BASE[kind], risk=True))
        if unity and base_account and unity['status'] == base_account['status'] == 'complete':
            equality['actual_unity'] = dict(baseline_equality(base_account['row'], unity['row'], kind),
                reference_raw_sha256=base_account['raw_sha256'], actual_raw_sha256=unity['raw_sha256'])
            if not equality['actual_unity']['passed']:
                report['blocking'].append(kind + ' actual unity baseline fingerprint mismatch')
        else:
            equality['actual_unity'] = {'passed': False, 'pending': account_id(kind, BASE[kind], risk=True)}
        report['baseline_equality'][kind] = equality
        equal[kind] = all(v['passed'] for v in equality.values())
        base_keys = [account_id(kind, n) for n in (BASE[kind], *ORDER[kind])]
        if all(k in accounts and accounts[k]['status'] == 'complete' for k in base_keys):
            document, diagnostics = calibration_document(accounts, kind, market_returns, initial_usdt)
            report['calibration_documents'][kind] = document
            report['calibration_diagnostics'][kind] = diagnostics
        else:
            report['pending'].append(kind + ' calibration requires four complete unscaled bases')
        singles = decisions_for(accounts, kind, ORDER[kind], equal[kind], market_returns, initial_usdt)
        report['decisions'][kind] = singles
        selected, combo = choose_singles(kind, singles)
        report['selected'][kind] = selected
        if any(v['status'] == 'pending' for v in singles.values()):
            report['pending'].append(kind + ' individual eligibility remains pending')
        elif combo:
            report['combinations'][kind] = combo
            name = combo_name(kind, combo)
            for a in accounts.values():
                if a['kind'] == kind and a['candidate'] == name:
                    require(a['components'] == combo, 'combo must contain ALL individually eligible components in registered order')
            combo_base = accounts.get(account_id(kind, name))
            if combo_base and combo_base['status'] == 'complete':
                document, diagnostics = calibration_document(accounts, kind, market_returns, initial_usdt, name)
                report['calibration_documents'][kind + '_combo'] = document
                report['calibration_diagnostics'][kind + '_combo'] = diagnostics
            outcome = decisions_for(accounts, kind, [name], equal[kind], market_returns, initial_usdt)[name]
            report['decisions'][kind][name] = outcome
            if outcome['eligible']:
                report['selected'][kind] = name
            if outcome['status'] == 'pending':
                report['pending'].append(kind + ' applicable combination pending')
    # Actual profiles must equal independently recomputed training and raw binding.
    for kind in BASE:
        for digest, entry in documents[kind].items():
            expected = report['calibration_documents'].get(kind + ('_combo' if len(entry['body']['profiles']) > 4 else ''))
            if expected is None or entry['body'] != expected:
                report['blocking'].append(kind + ' calibration differs from deterministic source-bound training: ' + digest)
    expected = required_matrix(report['selected'], report['combinations'])
    report['required_accounts'] = sorted(expected)
    extra = sorted(set(accounts) - expected)
    if extra:
        report['blocking'].append('unregistered/inapplicable account identities: ' + ', '.join(extra))
    report['pending'].extend(sorted(expected - set(accounts)))
    for key, a in accounts.items():
        if a['status'] != 'complete':
            report['pending'].append(key + ': ' + '; '.join(a['reasons']))
            if a['status'] == 'rejected':
                report['blocking'].append(key + ': rejected account')
    for label, selected in (('current_default', BASE), ('selected', report['selected'])):
        if label == 'selected' and selected == BASE:
            continue
        portfolios = []
        for budgets in ((2500, 7500), (5000, 5000), (7500, 2500)):
            pair = [accounts.get(account_id(k, selected[k], capital=b)) for k, b in zip(BASE, budgets)]
            if all(a and a['status'] == 'complete' for a in pair):
                portfolios.append(aggregate_pair(*pair, budgets, market_returns, fx, selected))
        report['portfolios'][label] = portfolios
    report['targets'] = {}
    for kind, name in report['selected'].items():
        a = accounts.get(account_id(kind, name))
        met = bool(a and a['status'] == 'complete' and decimal(a['row']['cagr']) >= (1 if kind == 'spot' else D('1.5')) and
                   (decimal(a['row']['mdd']) <= D('.30') if kind == 'spot' else decimal(a['row']['mdd']) < D('.50')))
        report['targets'][kind] = {'status': 'MET' if met else 'NOT_MET', 'selection_separate_from_targets': True}
    # Preserve original per-file envelopes in inventory (one copy per original file).
    report['input_envelopes'] = {a['path']: {k: v for k, v in a['bundle'].items() if k != 'results'} for a in accounts.values()}
    report['accounts'] = {k: {f: v for f, v in a.items() if f not in ('bundle', 'row')} for k, a in accounts.items()}
    report['status'] = 'blocked' if report['blocking'] else ('pending' if report['pending'] else 'complete_pending_independent_review')
    report['financial_review'] = None
    if args.financial_review:
        require(not report['blocking'] and not report['pending'], 'financial review cannot approve incomplete or blocked inventory')
        report['financial_review'] = verify_financial_review(args.financial_review, report)
        report['status'] = 'complete_reviewed'
    if args.phase == 'final' and report['financial_review'] is None:
        report['blocking'].append('final requires matching independent financial-review proof')
        report['status'] = 'blocked'
    report['inventory_sha256'] = old.checksum(inventory_binding(report))
    require(old.source_identity() == source, 'assessor source changed during read')
    return report


def export_csv(path, report):
    with Path(path).open('x', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('account', 'status', 'reasons', 'raw_sha256', 'cagr', 'continuous_proxy_mdd', 'es99_cny_loss'))
        for key, account in sorted(report['accounts'].items()):
            m = account.get('metrics', {})
            writer.writerow((key, account['status'], '; '.join(account['reasons']), account['raw_sha256'],
                             m.get('registered_account_cagr'), m.get('continuous_mdd_from_account'), m.get('metrics', {}).get('daily_es99_loss')))


def export_markdown(path, report):
    with Path(path).open('x') as stream:
        stream.write('# Frozen BTC edge assessment\n\nStatus: ' + report['status'] + '\n\n')
        stream.write('Native cases 0; actual account-days 0; NOT_QUALIFIED; prospective_alpha_proven=false.\n\n')
        stream.write('Command: `' + ' '.join(report['command']) + '`\n\n')
        stream.write('Inventory SHA256: `' + report['inventory_sha256'] + '`\n\n')
        stream.write('Assessor source: `' + json.dumps(report['analysis_source'], sort_keys=True) + '`\n\n')
        for kind in BASE:
            stream.write(f"{kind}: selected {report['selected'][kind]}; original targets {report['targets'][kind]['status']}.\n\n")
        for name in ('pending', 'blocking', 'limitations'):
            stream.write(name + ':\n\n')
            for value in report[name]:
                stream.write('- ' + value + '\n')
            stream.write('\n')
        stream.write('Exact input identities:\n\n')
        for path, digest in report['inputs'].items():
            stream.write(f'- `{path}`: `{digest}`\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('preliminary', 'calibration', 'final'), required=True)
    for kind in BASE:
        parser.add_argument('--' + kind, type=Path)
        parser.add_argument('--' + kind + '-calibration', type=Path, action='append')
    parser.add_argument('--schedule', type=Path, default=ROOT.parent / 'coinquant/research/session_schedule.json')
    parser.add_argument('--fx', type=Path, default=ROOT.parent / 'starquant/data/usdcny_frankfurter.json')
    parser.add_argument('--market', type=Path, default=Path('/tmp/spotquant-market/klines'))
    parser.add_argument('--features', type=Path, required=True)
    parser.add_argument('--coin-market', type=Path, default=Path('/tmp/coinquant-market'))
    parser.add_argument('--coin-prints', type=Path, default=Path('/workspace/scratch/alpha-beta-next/public-print-vault'))
    parser.add_argument('--references', type=Path, default=ROOT / 'evidence/alpha-beta-next-20261002')
    parser.add_argument('--financial-review', type=Path)
    parser.add_argument('--calibration-out-dir', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--csv', type=Path)
    parser.add_argument('--markdown', type=Path)
    args = parser.parse_args(argv)
    for path in (args.out, args.csv, args.markdown, args.calibration_out_dir):
        require(path is None or not path.exists(), 'exclusive output already exists: ' + str(path))
    report = assess(args)
    report['command'] = [sys.executable, '-m', 'research.edge_assessment', *(sys.argv[1:] if argv is None else argv)]
    if args.calibration_out_dir:
        require(not report['blocking'], 'blocked accounts cannot produce calibration documents')
        args.calibration_out_dir.mkdir()
        for name, document in report['calibration_documents'].items():
            write_new(args.calibration_out_dir / (name + '.json'), document)
    write_new(args.out, report)
    if args.csv:
        export_csv(args.csv, report)
    if args.markdown:
        export_markdown(args.markdown, report)
    print(json.dumps({'out': str(args.out), 'sha256': sha(args.out), 'status': report['status']}, allow_nan=False))
    return 2 if report['blocking'] or (args.phase == 'final' and report['status'] != 'complete_reviewed') else 0


if __name__ == '__main__':
    raise SystemExit(main())
