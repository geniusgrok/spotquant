import copy
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from research import edge_assessment as e


def spot_row():
    return dict(candidate='atr-stop', scenario='base', initial_cny='10000', final_cny='10000',
                final_usdt='1000', cagr=.1, mdd='.1', audit={'passed': True}, cash_usdt='1000', btc='0',
                fills=[], daily=[], sessions=[], client_events=[], positions={}, allocations=[], pending_intents=[],
                session_error_count=0, execution_unresolved_sessions=0, policy_pending=False,
                filters={'blocked': 0}, complete=True, risk_calibration={'scale': '1', 'sha256': None},
                research_identity={'original': True}, opportunity_ledger=[], subpools=None)


def curve(factor=1, days=None):
    days = days or list(range(e.START, e.END, e.DAY))
    value, result = 1000., []
    for i, day in enumerate(days):
        value *= 1 + factor * (.002 if i % 2 else -.001)
        result.append(dict(day_ms=day, equity_usdt=value, equity_cny=value * 10,
                           net_btc=0., price_usdt=10., gross_exposure_over_equity=0.))
    return result


def accounts(kind='spot'):
    return {e.account_id(kind, name): dict(status='complete', curve=curve(1 if name == e.BASE[kind] else 2),
        raw_sha256=hashlib.sha256((kind + name).encode()).hexdigest(), source={'frozen': kind})
        for name in (e.BASE[kind], *e.ORDER[kind])}


def gates(kind='spot'):
    base = {s: dict(cagr='.50', mdd='.30', worst_day='-.10', es99='.08', underwater=100) for s in e.STRESSES[kind]}
    candidate = copy.deepcopy(base)
    candidate['base']['cagr'] = '.51'
    achieved = dict(achieved_match=True, validation_total_usdt_return_gain='.001')
    return candidate, base, achieved


class InventoryTests(unittest.TestCase):
    def test_full_matrix_includes_losing_singles_risk_controls_offsets_budgets(self):
        matrix = e.required_matrix()
        self.assertEqual(len(matrix), 68)
        for kind in e.BASE:
            for name in (e.BASE[kind], *e.ORDER[kind]):
                self.assertIn(e.account_id(kind, name, risk=True), matrix)
        self.assertIn(e.account_id('perp', 'incumbent', offset=-60000), matrix)
        self.assertIn(e.account_id('spot', 'cash', 'outage'), matrix)
        self.assertEqual(len(e.required_matrix({'spot': 'exit-confirm', 'perp': 'incumbent'})), 71)

    def test_manifest_hash_nested_duplicate_and_original_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / 'raw.json.gz'
            raw.write_bytes(gzip.compress(json.dumps({'source': {'original': True}, 'results': {}}).encode(), mtime=0))
            manifest = root / 'manifest.json'
            body = dict(format=e.MANIFEST, files=[dict(path=raw.name, sha256=e.sha(raw))])
            e.write_new(manifest, body)
            children, bindings = e.load_files(manifest)
            self.assertEqual(children[0][2]['source'], {'original': True})
            self.assertEqual(bindings[str(raw)], e.sha(raw))
            body['files'].append(body['files'][0])
            manifest.write_text(json.dumps(body))
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                e.load_files(manifest)
            body['files'] = [dict(path=raw.name, sha256='0' * 64)]
            manifest.write_text(json.dumps(body))
            with self.assertRaisesRegex(ValueError, 'hash'):
                e.load_files(manifest)
            nested = root / 'nested.json'
            nested.write_text(json.dumps(dict(format=e.MANIFEST, files=[])))
            body['files'] = [dict(path=nested.name, sha256=e.sha(nested))]
            manifest.write_text(json.dumps(body))
            with self.assertRaisesRegex(ValueError, 'nested'):
                e.load_files(manifest)

    def test_strict_json_and_exclusive_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'raw.json'
            for text in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    e.read_json(path)
            with self.assertRaises(FileExistsError):
                e.write_new(path, {})
            with self.assertRaises(ValueError):
                e.decimal('NaN')
            with self.assertRaises(ValueError):
                e.decimal(True)

    def test_combo_all_eligible_order_ties_no_controls(self):
        kind = 'spot'
        decisions = {n: dict(eligible=True, worst_stress_cagr='.4') for n in e.ORDER[kind]}
        best, combo = e.choose_singles(kind, decisions)
        self.assertEqual(best, 'exit-confirm')
        self.assertEqual(combo, list(e.ORDER[kind]))
        self.assertEqual(e.combo_name(kind, combo), 'combo')
        with self.assertRaises(ValueError):
            e.combo_name(kind, list(reversed(combo)))
        with self.assertRaises(ValueError):
            e.components(kind, 'combo', ['exit-confirm', 'cash'])
        decisions['stop-budget']['eligible'] = False
        decisions['crowding-interaction']['eligible'] = False
        self.assertEqual(e.choose_singles(kind, decisions)[1], [])


class EqualityAndSourceTests(unittest.TestCase):
    def test_six_groups_and_operating_drift_and_original_annotations(self):
        before = spot_row()
        self.assertTrue(e.baseline_equality(before, copy.deepcopy(before), 'spot')['passed'])
        for key, value, group in [('final_usdt', '999', 'financial'), ('btc', '1', 'financial'),
                                   ('filters', {'blocked': 1}, 'operating'), ('extra_original', 1, 'remaining_original_fields'),
                                   ('risk_calibration', {'scale': '.9'}, 'original_annotations')]:
            actual = copy.deepcopy(before)
            actual[key] = value
            proof = e.baseline_equality(before, actual, 'spot')
            self.assertFalse(proof['passed'])
            self.assertIn(group, proof['differences'])
        actual = copy.deepcopy(before)
        actual['opportunity_ledger'] = [{'event': 'added_edge_instrumentation'}]
        self.assertTrue(e.baseline_equality(before, actual, 'spot')['passed'])

    def test_source_committed_tree_digest_contract_and_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'research').mkdir()
            (root / 'spotquant').mkdir()
            for name in ('edge_spec.json', 'edge-PROTOCOL.md'):
                (root / 'research' / name).write_bytes((e.ROOT / 'research' / name).read_bytes())
            for name in ('alpha_beta_spec.json', 'alpha-beta-PROTOCOL.md', 'complete-delivery-PROTOCOL.md'):
                (root / 'research' / name).write_text('fixture')
            (root / 'research/a.py').write_text('a = 1\n')
            (root / 'spotquant/b.py').write_text('b = 2\n')
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()
            git('init', '-q'); git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture')
            digest = hashlib.sha256()
            for name in ('research/a.py', 'spotquant/b.py'):
                digest.update(name.encode() + b'\0' + (root / name).read_bytes() + b'\0')
            source = dict(git_head=git('rev-parse', 'HEAD'), dirty=False, python_sources_sha256=digest.hexdigest())
            with patch.object(e, 'ROOT', root), patch.object(e.old, 'ROOT', root):
                self.assertEqual(e.verify_source(source, 'spot')['research/edge_spec.json'], e.SPEC_HASH['spot'])
                for changed in (dict(source, dirty=True), dict(source, python_sources_sha256='0' * 64)):
                    with self.assertRaises(ValueError):
                        e.verify_source(changed, 'spot')
                (root / 'research/edge_spec.json').write_text('{}')
                git('add', '.'); git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'bad spec')
                with self.assertRaisesRegex(ValueError, 'contract'):
                    e.verify_source(dict(source, git_head=git('rev-parse', 'HEAD')), 'spot')

    def test_original_session_clocks_and_outage(self):
        row = spot_row()
        start = e.START
        row['sessions'] = [dict(start_ms=start, ended_ms=start + 300000, elapsed_seconds=300.,
            archive_verified=True, archive_backup_sha256='a' * 64, status='offline_execution',
            errors=[], execution_unresolved=False)]
        self.assertEqual(e.old.row_validity(row, 'spot', [start], 10000), [])
        for change in (dict(ended_ms=start + 301000), dict(execution_unresolved=True), dict(archive_verified=False)):
            altered = copy.deepcopy(row)
            altered['sessions'][0].update(change)
            with self.assertRaises(ValueError):
                e.old.row_validity(altered, 'spot', [start], 10000)
        self.assertEqual(e.old.row_validity(row, 'spot', [start, 1583020800000], 10000, 'outage'), [])
        with self.assertRaises(ValueError):
            e.old.row_validity(row, 'spot', [start, start + e.DAY], 10000)
        row['complete'] = False
        self.assertIn('measurement_incomplete', e.old.row_validity(row, 'spot', [start, start + e.DAY], 10000))


class RiskAndGateTests(unittest.TestCase):
    def test_es99_ceil_and_sign(self):
        self.assertEqual(e.es99([-.3, -.1] + [.01] * 99), .2)
        self.assertEqual(e.es99([.1] * 100), -.1)
        self.assertEqual(e.es99([-.3] + [.01] * 99), .3)

    def test_training_only_future_perturbation_exact_731_and_project_separation(self):
        market = [.01 if i % 2 else -.005 for i in range(len(curve()))]
        data = accounts()
        doc, stats = e.calibration_document(data, 'spot', market, 1000)
        self.assertEqual(stats['exit-confirm']['training_days'], 731)
        self.assertEqual(doc['profiles']['atr-stop']['scale'], '1')
        changed = copy.deepcopy(data)
        for a in changed.values():
            for p in a['curve'][731:]:
                p['equity_usdt'] *= 1000
        other, _ = e.calibration_document(changed, 'spot', market[:731] + [999] * (len(market) - 731), 1000)
        self.assertEqual(doc, other)
        changed = copy.deepcopy(data)
        changed[e.account_id('spot', 'exit-confirm')]['curve'].pop(5)
        with self.assertRaisesRegex(ValueError, '731'):
            e.calibration_document(changed, 'spot', market, 1000)
        perp, _ = e.calibration_document(accounts('perp'), 'perp', market, 1000)
        self.assertNotEqual(doc['profiles']['crowding-interaction'], perp['profiles']['crowding-interaction'])
        self.assertEqual(perp['profiles']['crowding-interaction']['project_kind'], 'perp')

    def test_profile_schema_scale_raw_binding_and_unity(self):
        market = [.01 if i % 2 else -.005 for i in range(len(curve()))]
        doc, _ = e.calibration_document(accounts(), 'spot', market, 1000)
        names = list(doc['profiles'])
        for changes in ({'scale': '-.01'}, {'scale': 'NaN'}, {'base_bundle_sha256': 'x'},
                        {'project_kind': 'perp'}, {'calibration_end_ms': e.CUTOFF + 1}, {'extra': 1}):
            altered = copy.deepcopy(doc)
            altered['profiles']['exit-confirm'].update(changes)
            with self.assertRaises(ValueError):
                e.validate_profile_document(altered, 'spot', names)
        doc['profiles']['atr-stop']['scale'] = '.999'
        with self.assertRaises(ValueError):
            e.validate_profile_document(doc, 'spot', names)

    def test_exact_adoption_equality_and_just_fail(self):
        candidate, baseline, match = gates()
        candidate['base'].update(worst_day='-.105', es99='.084', underwater=100)
        for s in e.STRESSES['spot'][1:]:
            candidate[s]['cagr'] = '.49'
        run = lambda: e.adoption_gates('spot', candidate, baseline, match, '.30', '.30', True)
        self.assertTrue(run()['eligible'])
        for field, bad, reason in [('worst_day', '-.1050000001', 'worst_cny_day'),
                                   ('es99', '.0840000001', 'es99'), ('underwater', 101, 'underwater'),
                                   ('cagr', '.5099999999', 'base_improvement'), ('mdd', '.3000000001', 'base:mdd')]:
            saved = candidate['base'][field]
            candidate['base'][field] = bad
            self.assertIn(reason, run()['reasons'])
            candidate['base'][field] = saved
        self.assertFalse(e.adoption_gates('spot', candidate, baseline, match, '.30', '.30', False)['eligible'])

    def test_perp_strict_mdd_and_actual_risk_only_fallback(self):
        candidate, baseline, match = gates('perp')
        candidate['base']['mdd'] = '.50'
        self.assertIn('base:mdd', e.adoption_gates('perp', candidate, baseline, match, '.3', '.3', True)['reasons'])
        candidate['base']['mdd'] = '.499999'
        match['validation_total_usdt_return_gain'] = '-.01'
        self.assertTrue(e.adoption_gates('perp', candidate, baseline, match, '.29', '.30', True)['eligible'])
        self.assertFalse(e.adoption_gates('perp', candidate, baseline, match, '.290000001', '.30', True)['eligible'])
        match['achieved_match'] = False
        self.assertFalse(e.adoption_gates('perp', candidate, baseline, match, '.29', '.30', True)['eligible'])

    def test_cash_identifiable_and_degenerate_market(self):
        regression = e.old.safe_regression([0.] * 10, [.01, -.01] * 5)
        self.assertTrue(regression['identifiable'])
        self.assertEqual(regression['beta_btc'], 0)
        self.assertEqual(e.old.risk_stats([0.] * 10, [.01, -.01] * 5)['volatility'], 0)
        self.assertFalse(e.old.safe_regression([0.] * 10, [0.] * 10)['identifiable'])


class CapitalTests(unittest.TestCase):
    def pair(self):
        result = []
        for kind, budget in (('spot', 2500), ('perp', 7500)):
            initial = float(D(budget) / 10 * D('.999'))
            values = [dict(day_ms=d, equity_cny=budget * .998001, equity_usdt=initial,
                           net_btc=1., price_usdt=10.) for d in range(e.START, e.END, e.DAY)]
            result.append(dict(status='complete', kind=kind, candidate=e.BASE[kind], capital=str(budget),
                offset=0, scenario='base', risk=False, row=dict(audit={'passed': True}, initial_cny=str(budget)),
                curve=values, raw_sha256=kind, metrics=dict(fees_usdt='1', funding_paid_usdt='0')))
        return result

    def test_actual_budget_sum_exposure_conservation(self):
        a, b = self.pair()
        result = e.aggregate_pair(a, b, (2500, 7500), [.01, -.01] * (len(a['curve']) // 2) + [.01] * (len(a['curve']) % 2), lambda _: D(10))
        self.assertAlmostEqual(result['curve'][0]['equity_cny'], 9980.01)
        self.assertEqual(result['curve'][0]['absolute_btc_notional_usdt'], 20)
        self.assertIsNone(result['account_return_correlation'])
        self.assertFalse(result['neutral_reference'])
        self.assertEqual(result['initial_cny'], 10000)
        self.assertEqual(result['cash_flows'], 0)

    def test_wrong_capital_role_daily_alignment_and_curve_scaling_rejected(self):
        market = [0.] * len(curve())
        for changes in (dict(capital='10000'), dict(offset=60000), dict(risk=True), dict(status='pending'), dict(scenario='outage'), dict(candidate='cash')):
            a, b = self.pair()
            a.update(changes)
            with self.assertRaises(ValueError):
                e.aggregate_pair(a, b, (2500, 7500), market, lambda _: D(10))
        a, b = self.pair()
        b['curve'].pop()
        with self.assertRaises(ValueError):
            e.aggregate_pair(a, b, (2500, 7500), market, lambda _: D(10))
        a, b = self.pair()
        a['row']['initial_cny'] = '10000'
        with self.assertRaises(ValueError):
            e.aggregate_pair(a, b, (2500, 7500), market, lambda _: D(10))


class EnvelopeAndPhaseTests(unittest.TestCase):
    def fixture(self):
        env = dict(starts=[e.START], fx_sha256='fx', market_sha256='market', schedule_sha256='schedule')
        source = dict(git_head='a' * 40, dirty=False, python_sources_sha256='b' * 64)
        row = spot_row()
        row.update(complete=False, cagr=None)
        body = dict(format='btc-alpha-beta-edge-spot-v1', source=source,
            spec_sha256=e.SPEC_HASH['spot'], protocol_sha256=e.PROTOCOL_HASH['spot'],
            fx_sha256='fx', market_sha256='market', schedule_sha256='schedule', feature_sha256=None,
            feature_consumed=False, risk_calibration_sha256=None, risk_calibration={'scale': '1', 'sha256': None},
            edge=dict(candidate='atr-stop', scenario='base', components=[], execution='actual_finite_session_lifecycle',
                      original_candidate='atr-stop', partial_limit=1), results={'atr-stop-base': row})
        return body, env

    def test_envelope_source_contract_inputs_and_partial_retention(self):
        body, env = self.fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(e, 'verify_source', return_value={}) as proof:
            path = Path(tmp) / 'raw.json'
            e.write_new(path, body)
            accounts, files, rejected = e.ingest({'spot': path}, env, [], lambda _: D(7), [], [], {'spot': {}, 'perp': {}})
            self.assertFalse(rejected)
            row = accounts[e.account_id('spot', 'atr-stop')]
            self.assertEqual(row['status'], 'pending')
            self.assertEqual(row['bundle']['source'], body['source'])
            proof.assert_called_once_with(body['source'], 'spot')
            for field in ('spec_sha256', 'protocol_sha256', 'fx_sha256', 'schedule_sha256', 'market_sha256'):
                bad = copy.deepcopy(body); bad[field] = 'wrong'
                with self.assertRaises(ValueError):
                    e.envelope(bad, 'spot', env, {})
            bad = copy.deepcopy(body); bad['edge']['original_candidate'] = 'consensus'
            with self.assertRaises(ValueError):
                e.envelope(bad, 'spot', env, {})

    def test_duplicate_account_identity_rejected_across_distinct_files(self):
        body, env = self.fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(e, 'verify_source', return_value={}):
            root = Path(tmp)
            files = []
            for name in ('a.json', 'b.json'):
                path = root / name; e.write_new(path, body)
                files.append(dict(path=name, sha256=e.sha(path)))
            manifest = root / 'manifest.json'; e.write_new(manifest, dict(format=e.MANIFEST, files=files))
            with self.assertRaisesRegex(ValueError, 'duplicate account identity'):
                e.ingest({'spot': manifest}, env, [], lambda _: D(7), [], [], {'spot': {}, 'perp': {}})

    def test_actual_profile_exact_document_binding(self):
        data = accounts()
        doc, _ = e.calibration_document(data, 'spot', [.01, -.01] * 1227, 1000)
        body, _ = self.fixture()
        digest = 'd' * 64
        body['risk_calibration_sha256'] = digest
        body['risk_calibration'] = dict(doc['profiles']['atr-stop'], sha256=digest)
        account = dict(kind='spot', candidate='atr-stop', bundle=body)
        docs = {'spot': {digest: {'body': doc, 'bytes': b'original'}}}
        e.verify_account_binding(account, docs)
        body['risk_calibration']['base_bundle_sha256'] = 'e' * 64
        with self.assertRaises(ValueError):
            e.verify_account_binding(account, docs)
        with self.assertRaises(ValueError):
            e.verify_account_binding(account, {'spot': {}})

    def test_daily_cash_reconstruction_and_terminal_rejection(self):
        row = spot_row()
        row['initial_cny'] = '10000'; row['fills'] = []
        daily = [dict(timestamp_ms=e.START + e.DAY, cash_usdt='999', btc='0', price_usdt='10',
                      equity_usdt='999', equity_cny='9980.01')]
        e.audit_daily(row, daily, 'spot', lambda _: D(10))
        daily[0]['cash_usdt'] = '998'
        with self.assertRaisesRegex(ValueError, 'daily monetary'):
            e.audit_daily(row, daily, 'spot', lambda _: D(10))
        row.update(final_usdt='999', final_cny='9980.01', cash_usdt='999', btc='0',
                   audit=dict(passed=True, cash_from_fills='999.000', btc_from_fills='0', fees_usdt='0', no_deposits=True))
        # Audit fields retain exact original Decimal spelling; derive with helper.
        row['audit'] = e.spot_audit(e.SimpleNamespace(initial_cash=D('999.000'), cash=D(999), btc=D(0), fills=[]))
        self.assertTrue(e.monetary_audit(row, 'spot', lambda _: D(10))['passed'])
        row['final_cny'] = '9980.02'
        with self.assertRaisesRegex(ValueError, 'terminal CNY'):
            e.monetary_audit(row, 'spot', lambda _: D(10))

    def test_consumed_official_bytes_checksum_and_derived_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'tape.zip'; path.write_bytes(b'official-original')
            checksum = path.with_suffix('.zip.CHECKSUM'); checksum.write_text(e.sha(path) + '  tape.zip\n')
            bundle = dict(inputs=dict(loaded_minute_files={}, loaded_print_files={'tape.zip': e.sha(path)}),
                edge=dict(print_restore_receipts=[dict(name='tape.zip', sha256=e.sha(path),
                checksum_sha256=e.sha(checksum), derived_binary_sha256='a' * 64)]))
            env = dict(coin_market=tmp, coin_prints=tmp)
            e.verify_consumed_files(bundle, env, {})
            path.write_bytes(b'foreign')
            with self.assertRaisesRegex(ValueError, 'CHECKSUM'):
                e.verify_consumed_files(bundle, env, {})

    def test_preliminary_pending_and_final_fail_closed_without_inventory(self):
        from contextlib import ExitStack
        args = e.SimpleNamespace(phase='preliminary', schedule=Path('schedule'), fx=Path('fx'), market=Path('market'),
            features=Path('features'), coin_market=Path('coin'), coin_prints=Path('prints'), references=Path('references'),
            spot=None, perp=None, spot_calibration=None, perp_calibration=None, financial_review=None)
        with ExitStack() as stack:
            for owner, name, value in ((e.old, 'source_identity', {'dirty': False}), (e, 'verify_source', {}),
                (e, 'environment', {}), (e.old, 'load_daily', []), (e.old, 'PriorFX', lambda _: D(10)),
                (e.old, 'market_returns_for', ([], [])), (e, 'approved_references', ({'spot': {}, 'perp': {}}, {})),
                (e, 'ingest', ({}, {}, [])), (e, 'sha', 'a' * 64)):
                stack.enter_context(patch.object(owner, name, return_value=value))
            preliminary = e.assess(args)
            self.assertEqual(preliminary['status'], 'pending')
            self.assertEqual(len(preliminary['required_accounts']), 68)
            self.assertFalse(preliminary['blocking'])
            args.phase = 'final'
            final = e.assess(args)
            self.assertEqual(final['status'], 'blocked')
            self.assertIn('final requires matching independent financial-review proof', final['blocking'])
            self.assertEqual(final['native_qualification'], 'NOT_QUALIFIED')


def complete_review_fixture():
    """Complete 68-account synthetic all-flat study: all mechanisms correctly lose."""
    source = dict(git_head='a' * 40, dirty=False, python_sources_sha256='b' * 64)
    report = dict(inputs={}, environment={}, analysis_source=source, contracts={k: dict(spec_sha256=e.SPEC_HASH[k], protocol_sha256=e.PROTOCOL_HASH[k]) for k in e.BASE},
        accounts={}, input_envelopes={}, required_accounts=sorted(e.required_matrix()), selected=dict(e.BASE),
        combinations={}, calibration_documents={}, calibration_diagnostics={}, calibration_input_documents={'spot': {}, 'perp': {}},
        decisions={}, baseline_equality={}, portfolios={}, phase='preliminary', status='complete_pending_independent_review', pending=[], blocking=[])
    curves = {}
    for capital in ('2500', '5000', '7500', '10000'):
        initial_usdt = float(D(capital) / 10 * D('.999'))
        curves[capital] = [dict(day_ms=d, equity_cny=initial_usdt * 10 * .999, equity_usdt=initial_usdt,
            net_btc=0., price_usdt=10., gross_exposure_over_equity=0.) for d in range(e.START, e.END, e.DAY)]
    market = [.01 if i % 2 else -.01 for i in range(len(curves['10000']))]
    for key in report['required_accounts']:
        kind, name, scenario, capital, offset, role = key.split('|')
        capital = format(D(capital), 'f')
        points = curves[capital]
        digest = hashlib.sha256(key.encode()).hexdigest()
        path = key + '.json'
        cny, _ = e.daily_metrics([p['equity_cny'] for p in points], float(capital)); cny.pop('daily_es5_loss')
        cny['daily_es99_loss'] = e.es99([-.001999] + [0.] * (len(points) - 1))
        usd, _ = e.daily_metrics([p['equity_usdt'] for p in points], points[0]['equity_usdt']); usd.pop('daily_es5_loss')
        usd['final_usdt'] = usd.pop('final_cny')
        m = dict(metrics=cny, usdt_metrics=usd, registered_account_cagr=cny['cagr'], registered_account_usdt_cagr=0.,
            continuous_mdd_from_account=cny['daily_mdd'], fees_usdt='0', funding_paid_usdt='0', fill_count=0,
            validation_2022_plus=dict(regression={'beta_btc': 0.}, usdt={'daily_volatility_annualized': 0.}))
        a = dict(kind=kind, candidate=name, scenario=scenario, capital=capital, offset=int(offset), risk=role == 'risk',
            components=e.components(kind, name), source=source, path=path, raw_sha256=digest, status='complete', reasons=[],
            curve=points, metrics=m, monetary_audit={'passed': True}, row=dict(initial_cny=capital, audit={'passed': True}, fills=[], cagr=cny['cagr'], mdd=str(cny['daily_mdd'])))
        a['gate_inputs'] = dict(raw_sha256=digest, values=e.gate_values(a))
        report['accounts'][key] = a
        meta = dict(source=source, fx_sha256='f' * 64, schedule_sha256='d' * 64, market_sha256='e' * 64,
                    spec_sha256=e.SPEC_HASH[kind], protocol_sha256=e.PROTOCOL_HASH[kind], feature_sha256=e.FEATURE_HASH,
                    feature_file_sha256=e.FEATURE_HASH, market_identity={'original': 'market'}, risk_calibration_sha256=None)
        report['input_envelopes'][path] = meta if kind == 'spot' else dict(inputs=meta, edge=dict(meta))
        report['inputs'][path] = digest
    for kind in e.BASE:
        doc, stats = e.calibration_document(report['accounts'], kind, market, 999.)
        report['calibration_documents'][kind] = doc
        report['calibration_diagnostics'][kind] = stats
        digest = e.old.checksum(doc)
        report['calibration_input_documents'][kind][digest] = doc
        report['inputs'][kind + '-calibration.json'] = digest
        for a in report['accounts'].values():
            if a['kind'] == kind and a['risk']:
                env = report['input_envelopes'][a['path']]
                (env if kind == 'spot' else env['edge'])['risk_calibration_sha256'] = digest
        fingerprints = {g: hashlib.sha256(g.encode()).hexdigest() for g in ('financial', 'fills', 'daily', 'ownership', 'operating', 'remaining_original_fields')}
        comparisons = {}
        for scenario in (*e.STRESSES[kind], 'actual_unity'):
            key = e.account_id(kind, e.BASE[kind], 'base' if scenario == 'actual_unity' else scenario, risk=scenario == 'actual_unity')
            reference = report['accounts'][e.account_id(kind, e.BASE[kind])]['raw_sha256'] if scenario == 'actual_unity' else hashlib.sha256((kind + scenario).encode()).hexdigest()
            comparisons[scenario] = dict(passed=True, differences={}, reference=fingerprints, actual=fingerprints,
                reference_raw_sha256=reference, actual_raw_sha256=report['accounts'][key]['raw_sha256'])
        report['baseline_equality'][kind] = comparisons
        report['decisions'][kind] = e.decisions_for(report['accounts'], kind, e.ORDER[kind], True, market, 999.)
    portfolios = []
    for pair in ((2500, 7500), (5000, 5000), (7500, 2500)):
        accounts = [report['accounts'][e.account_id(k, e.BASE[k], capital=b)] for k, b in zip(e.BASE, pair)]
        portfolios.append(e.aggregate_pair(*accounts, pair, market, lambda _: D(10)))
    report['portfolios']['current_default'] = portfolios
    for a in report['accounts'].values():
        a.pop('row')
    return report


class FinancialReviewCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = complete_review_fixture()
        cls.expected = e.review_expectations(cls.report)
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.prior = cls.root / 'preliminary.json'
        e.write_new(cls.prior, cls.report)
        cls.inventory = e.old.checksum(e.inventory_binding(cls.report))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def proof(self, mutate=None):
        checks = {}
        for category, expected in self.expected.items():
            records = copy.deepcopy(list(expected.values()))
            if mutate:
                mutate(category, records)
            path = self.root / (category + '.json')
            path.write_text(json.dumps(dict(inventory_sha256=self.inventory, category=category, recomputations=records), allow_nan=False))
            checks[category] = dict(passed=True, artifact=dict(path=path.name, sha256=e.sha(path)))
        proof = dict(format='btc-edge-financial-review-v1', preliminary=dict(path=self.prior.name, sha256=e.sha(self.prior)),
                     inventory_sha256=self.inventory, reviewer_source=self.report['analysis_source'], checks=checks)
        path = self.root / 'review.json'; path.write_text(json.dumps(proof))
        return path

    def test_complete_roundtrip_and_correct_negative_candidates(self):
        self.assertEqual(len(self.report['accounts']), 68)
        self.assertEqual({k: len(v) for k, v in self.expected.items()}, dict(source_and_inputs=136,
            original_accounting=68, baseline_six_groups=10, calibration_and_actual_risk=16,
            adoption_gates=8, actual_budget_aggregation=3))
        self.assertTrue(all(not d['eligible'] for decisions in self.report['decisions'].values() for d in decisions.values()))
        path = self.proof()
        with patch.object(e, 'verify_source', return_value={}):
            self.assertEqual(e.verify_financial_review(path, self.report)['sha256'], e.sha(path))

    def test_exact_negative_mdd_complete_proof_roundtrip(self):
        self.report = complete_review_fixture()
        accounts = self.report['accounts']
        for scenario in e.STRESSES['spot']:
            a = accounts[e.account_id('spot', e.BASE['spot'], scenario)]
            a['gate_inputs']['values']['mdd'] = '0.31'
            a['metrics']['continuous_mdd_from_account'] = .31
        key = e.account_id('spot', 'exit-confirm')
        accounts[key]['gate_inputs']['values']['mdd'] = '0.30000000000000001'
        accounts[key]['metrics']['continuous_mdd_from_account'] = .3
        market = [.01 if i % 2 else -.01 for i in range((e.END - e.START) // e.DAY)]
        self.report['decisions']['spot'] = e.decisions_for(accounts, 'spot', e.ORDER['spot'], True, market, 999.)
        decision = self.report['decisions']['spot']['exit-confirm']
        self.assertFalse(decision['checks']['base_improvement'])
        self.assertFalse(decision['eligible'])
        self.expected = e.review_expectations(self.report)
        self.assertEqual(self.expected['original_accounting'][key]['values']['money']['continuous_proxy_mdd'],
                         '0.30000000000000001')
        self.assertEqual(self.expected['adoption_gates']['spot:exit-confirm']['values']['gate_inputs'][key],
                         accounts[key]['gate_inputs'])
        self.inventory = e.old.checksum(e.inventory_binding(self.report))
        self.prior = self.root / 'exact-negative-preliminary.json'
        e.write_new(self.prior, self.report)
        path = self.proof()
        with patch.object(e, 'verify_source', return_value={}):
            self.assertEqual(e.verify_financial_review(path, self.report)['sha256'], e.sha(path))
            # A freshly hashed artifact still cannot substitute its rounded display value.
            def rounded(category, records):
                if category == 'adoption_gates':
                    record = next(r for r in records if r['id'] == 'spot:exit-confirm')
                    record['values']['gate_inputs'][key]['values']['mdd'] = '0.3'
            with self.assertRaises(ValueError):
                e.verify_financial_review(self.proof(rounded), self.report)

    def test_actual_calibrated_and_coin_strict_mdd_exact_boundaries(self):
        report = complete_review_fixture()
        accounts = report['accounts']
        market = [.01 if i % 2 else -.01 for i in range((e.END - e.START) // e.DAY)]
        for kind in e.BASE:
            name = e.ORDER[kind][0]
            control = accounts[e.account_id(kind, e.BASE[kind], risk=True)]
            actual = accounts[e.account_id(kind, name, risk=True)]
            control['gate_inputs']['values']['mdd'] = '0.31'
            control['metrics']['continuous_mdd_from_account'] = .31
            for value, passed in [('0.30', True), ('0.30000000000000001', False)]:
                with self.subTest(kind=kind, actual_mdd=value):
                    actual['gate_inputs']['values']['mdd'] = value
                    actual['metrics']['continuous_mdd_from_account'] = float(value)
                    report['decisions'][kind] = e.decisions_for(accounts, kind, e.ORDER[kind], True, market, 999.)
                    self.assertIs(report['decisions'][kind][name]['checks']['actual_validation'], passed)
                    expected = e.review_expectations(report)
                    self.assertEqual(expected['calibration_and_actual_risk']['actual:' + e.account_id(kind, name, risk=True)]
                                     ['values']['validation']['continuous_proxy_mdd'], value)
        name = e.ORDER['perp'][0]
        account = accounts[e.account_id('perp', name)]
        for value, passed in [('0.49999999999999999', True), ('0.50', False), ('0.50000000000000001', False)]:
            with self.subTest(coin_strict_mdd=value):
                account['gate_inputs']['values']['mdd'] = value
                account['metrics']['continuous_mdd_from_account'] = float(value)
                report['decisions']['perp'] = e.decisions_for(accounts, 'perp', e.ORDER['perp'], True, market, 999.)
                self.assertIs(report['decisions']['perp'][name]['checks']['base:mdd'], passed)
                e.review_expectations(report)

    def test_exact_gate_input_schema_finite_and_raw_binding(self):
        account = next(iter(self.report['accounts'].values()))
        mutations = [lambda a: a.pop('gate_inputs'),
            lambda a: a.update(gate_inputs=[]),
            lambda a: a['gate_inputs'].update(raw_sha256='0' * 64),
            lambda a: a['gate_inputs'].update(extra=0),
            lambda a: a['gate_inputs']['values'].pop('es99'),
            lambda a: a['gate_inputs']['values'].update(extra=0),
            lambda a: a['gate_inputs']['values'].update(mdd=.3),
            lambda a: a['gate_inputs']['values'].update(mdd='NaN'),
            lambda a: a['gate_inputs']['values'].update(cagr=True),
            lambda a: a['gate_inputs']['values'].update(worst_day=float('inf')),
            lambda a: a['gate_inputs']['values'].update(underwater=0.)]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = copy.deepcopy(account)
                mutate(changed)
                with self.assertRaises(ValueError):
                    e.gate_values(changed)
        changed = copy.deepcopy(account)
        changed['row'] = dict(cagr=changed['gate_inputs']['values']['cagr'], mdd='0.30000000000000001')
        with self.assertRaisesRegex(ValueError, 'differ from original'):
            e.gate_values(changed)
        # This schema checks exact finite inputs; only the existing gates decide economics.
        changed = copy.deepcopy(account)
        changed['gate_inputs']['values']['mdd'] = '1.01'
        self.assertEqual(e.gate_values(changed)['mdd'], '1.01')

    def test_missing_each_category_duplicate_unknown_failed_and_wrong_raw(self):
        mutations = []
        for target in self.expected:
            mutations.append((target + ':missing', lambda category, rows, target=target: rows.pop() if category == target else None))
        mutations.extend([
            ('duplicate', lambda category, rows: rows.append(copy.deepcopy(rows[0])) if category == 'original_accounting' else None),
            ('unknown', lambda category, rows: rows[0].update(id='NOT_AN_ACCOUNT') if category == 'original_accounting' else None),
            ('failed', lambda category, rows: rows[0].update(matches=False) if category == 'original_accounting' else None),
            ('null', lambda category, rows: rows[0].update(values=None) if category == 'original_accounting' else None),
            ('wrong_raw', lambda category, rows: rows[0]['raw_bindings'].update({next(iter(rows[0]['raw_bindings'])): '0' * 64}) if category == 'original_accounting' else None),
            ('budget_value', lambda category, rows: rows[0]['values'].update(daily_equity_exposure_sha256='0' * 64) if category == 'actual_budget_aggregation' else None),
            ('false_eligibility', lambda category, rows: rows[0]['values']['result'].update(eligible=True) if category == 'adoption_gates' else None),
        ])
        with patch.object(e, 'verify_source', return_value={}):
            for name, mutation in mutations:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    e.verify_financial_review(self.proof(mutation), self.report)

    def test_typed_finite_and_exact_evidence_binding(self):
        for actual in (None, True, float('inf'), float('nan'), 'unknown'):
            with self.assertRaises(ValueError):
                e.verify_review_value(actual, 1.)
        path = self.proof()
        changed = dict(self.report, inputs=dict(self.report['inputs'], foreign='a' * 64))
        with patch.object(e, 'verify_source', return_value={}), self.assertRaisesRegex(ValueError, 'inventory'):
            e.verify_financial_review(path, changed)
        changed = dict(self.report, calibration_diagnostics={})
        with patch.object(e, 'verify_source', return_value={}), self.assertRaisesRegex(ValueError, 'inventory'):
            e.verify_financial_review(path, changed)
        # Budget outcomes themselves, not only original raw identities, are bound.
        changed = dict(self.report, portfolios={})
        with patch.object(e, 'verify_source', return_value={}), self.assertRaisesRegex(ValueError, 'inventory'):
            e.verify_financial_review(path, changed)
        text = self.prior.read_text()
        try:
            self.prior.write_text(text + ' ')
            with patch.object(e, 'verify_source', return_value={}), self.assertRaisesRegex(ValueError, 'preliminary'):
                e.verify_financial_review(path, self.report)
        finally:
            self.prior.write_text(text)


class MissingDailyEvidenceTests(unittest.TestCase):
    def spot(self, sell_time=None):
        row = spot_row()
        row.update(final_usdt='1009', final_cny='10079.91', cash_usdt='1009')
        row['fills'] = [dict(id=1, time=e.START + e.DAY // 2, buyer=True, qty='1', price='10', quote='10', commission='0', commission_asset='USDT'),
            dict(id=2, time=sell_time or e.START + e.DAY * 3 // 2, buyer=False, qty='1', price='20', quote='20', commission='0', commission_asset='USDT')]
        row['daily'] = [dict(timestamp_ms=e.END, equity_usdt='1009', equity_cny='10079.91', cash_usdt='1009', btc='0', price_usdt='20')]
        bars = [(d, D(10), D(20), D(10), D(20), D(1)) for d in range(e.START, e.END, e.DAY)]
        return row, bars

    def test_terminal_only_missing_held_day_rejected(self):
        row, bars = self.spot()
        with self.assertRaisesRegex(ValueError, 'missing daily snapshot'):
            e.original_curve(row, bars, lambda _: D(10), 'spot')

    def test_already_flat_but_unreflected_cash_change_rejected(self):
        row, bars = self.spot(e.START + e.DAY * 3 // 4)
        with self.assertRaisesRegex(ValueError, 'missing daily snapshot'):
            e.original_curve(row, bars, lambda _: D(10), 'spot')

    def test_genuine_eventless_flat_carry_and_verified_intraday_flat(self):
        row, bars = self.spot()
        row.update(fills=[], final_usdt='999', final_cny='9980.01', cash_usdt='999')
        row['daily'][0].update(equity_usdt='999', equity_cny='9980.01', cash_usdt='999')
        result = e.original_curve(row, bars, lambda _: D(10), 'spot')
        self.assertEqual(len(result), (e.END - e.START) // e.DAY)
        self.assertTrue(all(p['net_btc'] == 0 and p['equity_usdt'] == 999 for p in result))
        # A real, already verified post-trade flat snapshot can begin a new carry.
        row, bars = self.spot(e.START + e.DAY * 3 // 4)
        row['daily'].insert(0, dict(row['daily'][0], timestamp_ms=e.START + e.DAY * 3 // 4 + 1))
        result = e.original_curve(row, bars, lambda _: D(10), 'spot')
        self.assertEqual(result[0]['equity_usdt'], 1009)

    def test_perp_midnight_funding_before_same_stamp_fill_preserved(self):
        row = dict(initial_cny='10000', trades=[dict(time=e.START + 1, qty='1', price='10', side='BUY'),
            dict(time=e.START + e.DAY, qty='1', price='20', side='SELL')],
            funding_ledger=[dict(time=e.START + e.DAY, incomeType='FUNDING_FEE', income='-1'),
                            dict(time=e.START + e.DAY, incomeType='REALIZED_PNL', income='10')])
        close = dict(stamp_ms=e.START + e.DAY, wallet_usdt='998', quantity_btc='1', mark_usdt='20', equity_usdt='1008', equity_cny='10069.92')
        flat = dict(stamp_ms=e.START + e.DAY + 1, wallet_usdt='1008', quantity_btc='0', mark_usdt='20', equity_usdt='1008', equity_cny='10069.92')
        e.audit_daily(row, [close, flat], 'perp', lambda _: D(10))
        with self.assertRaisesRegex(ValueError, 'missing daily snapshot'):
            e.audit_daily(row, [flat], 'perp', lambda _: D(10))

if __name__ == '__main__':
    unittest.main()
