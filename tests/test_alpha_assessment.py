import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import alpha_assessment as a


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.env = {'starts': list(range(795)), 'schedule_sha256': 's' * 64,
                    'primary_sha256': 'p' * 64, 'fx_sha256': 'f' * 64,
                    'market_sha256': 'm' * 64, 'spec_sha256': a.sha(a.SPEC_PATH),
                    'protocol_sha256': a.sha(a.PROTOCOL_PATH)}
        self.source = {'dirty': False, 'git_head': '1' * 40, 'python_sources_sha256': 'a' * 64}

    def spot(self):
        results = {}
        for n in a.SPEC['spot_candidates']:
            for s in a.SPEC['spot_scenarios']:
                core = n if n.startswith('core-') else None
                results[n + '-' + s] = {'candidate': n, 'scenario': s, 'complete': False,
                    'initial_cny': '10000', 'audit': {'passed': True}, 'sessions': [],
                    'execution_unresolved_sessions': 0, 'policy_pending': False, 'pending_intents': [],
                    'opportunity_ledger': [], 'research_identity': {'candidate': n,
                        'components': [] if n == 'consensus' else [n], 'core_mode': core,
                        'core_fraction': '.20' if core else '0', 'spec_sha256': self.env['spec_sha256'],
                        'calibration_sha256': None, 'risk_scale': '1'},
                    'risk_calibration': {'scale': '1', 'sha256': None}}
        return {'format': 1, 'source': self.source, **{k: v for k, v in self.env.items() if k.endswith('sha256') and k != 'primary_sha256'},
                'risk_calibration_sha256': None, 'results': results}

    def consume(self, body, expected=None):
        p = self.root / 'case.json'
        p.write_text(json.dumps(body))
        with patch.object(a, 'verify_source'):
            return a.consume(p, 'spot', self.env, [], None, [], [], expected=expected)

    def test_exact_matrix_missing_extra_malformed_and_retained_incomplete(self):
        body = self.spot()
        expected = {n + '/' + s for n in a.SPEC['spot_candidates'] for s in a.SPEC['spot_scenarios']}
        result = self.consume(body, expected)
        self.assertEqual(len(result['accounts']), 28)
        self.assertTrue(all(x['rejections'] == ['measurement_incomplete'] for x in result['accounts'].values()))
        del body['results']['consensus-base']
        with self.assertRaisesRegex(ValueError, 'missing matrix'):
            self.consume(body, expected)
        body = self.spot()
        body['results']['consensus-base']['candidate'] = 'default'
        with self.assertRaisesRegex(ValueError, 'candidate/key'):
            self.consume(body)
        body = self.spot()
        del body['results']['consensus-base']['complete']
        with self.assertRaisesRegex(ValueError, 'completion/audit'):
            self.consume(body)

    def test_source_input_spec_protocol_candidate_capital_starts_fail_closed(self):
        for key in ('spec_sha256', 'protocol_sha256', 'fx_sha256', 'schedule_sha256', 'market_sha256'):
            body = self.spot()
            body[key] = '0' * 64
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.consume(body)
        for change in ({'initial_cny': '9900'}, {'sessions': [{'start_ms': 1234}]}):
            body = self.spot()
            body['results']['consensus-base'].update(change)
            with self.assertRaises(ValueError):
                self.consume(body)
        with self.assertRaisesRegex(ValueError, 'clean measured'):
            a.verify_source(dict(self.source, dirty=True), 'spot')
        left = self.spot()
        right = copy.deepcopy(left)
        right['source']['python_sources_sha256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'source differs'):
            a.equivalent_inputs(left, right, 'spot')

    def perp(self):
        meta = {'source': self.source, 'measured_source': self.source,
                'spec_sha256': self.env['spec_sha256'], 'protocol_sha256': self.env['protocol_sha256'],
                'fx_sha256': self.env['fx_sha256'], 'schedule_sha256': self.env['primary_sha256'],
                'original_schedule_sha256': self.env['primary_sha256'], 'actual_starts_sha256': a.checksum(self.env['starts']),
                'initial_cny': '10000', 'start_offset_ms': 0, 'crowding_source': 'not used by alpha/beta mechanisms',
                'crowding_sha256': a.checksum(None), 'market_identity': {'fixture': 'synthetic'},
                'loaded_minute_files': {}, 'loaded_print_files': {}, 'risk_calibration_sha256': None,
                'risk_profiles': {}, 'candidates': {n: [] if n == 'incumbent' else [n] for n in a.SPEC['perp_candidates']}}
        return {'inputs': meta, 'conditions': {'conversion_each_way': '.001', 'terminal_funding_exclusive_ms': a.END_MS},
                'results': {n: {s: {'initial_cny': '10000', 'complete': False, 'audit': {'passed': True}, 'sessions': [],
                    'known_path': True, 'failure': None, 'execution_unresolved': 0, 'opportunity_ledger': []}
                    for s in a.SPEC['perp_scenarios']} for n in a.SPEC['perp_candidates']}}

    def test_coin_nested_schema_and_own_hash_scheme_accepts_all20_rejects_mismatch(self):
        body = self.perp()
        expected = {n + '/' + s for n in a.SPEC['perp_candidates'] for s in a.SPEC['perp_scenarios']}
        p = self.root / 'perp.json'
        def consume():
            p.write_text(json.dumps(body))
            with patch.object(a, 'verify_source'):
                return a.consume(p, 'perp', self.env, [], None, [], [], expected=expected)
        self.assertEqual(len(consume()['accounts']), 20)
        body['inputs']['actual_starts_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'start hash'):
            consume()
        body = self.perp()
        body['inputs']['measured_source'] = dict(self.source, git_head='2' * 40)
        with self.assertRaisesRegex(ValueError, 'conflicting Coin'):
            consume()
        body = self.perp()
        body['inputs']['candidates']['fresh-entry'] = ['atr-trail']
        with self.assertRaisesRegex(ValueError, 'component mismatch'):
            consume()
        body = self.perp()
        del body['results']['single-topup']['trigger-slip']
        with self.assertRaisesRegex(ValueError, 'missing matrix'):
            consume()

    def test_risk_requires_exact_file_hash_profile_and_bound_candidate(self):
        body = self.spot()
        body['results'] = {k: v for k, v in body['results'].items() if k.endswith('-base')}
        profile = {'scale': '.5', 'base_bundle_sha256': 'a' * 64, 'baseline_candidate': 'consensus',
                   'effective_from_ms': a.CUTOFF, 'calibration_end_ms': a.CUTOFF,
                   'training_end_day_exclusive': '2022-01-01'}
        cal = {'profiles': {n: dict(profile, scale='1' if n == 'consensus' else '.5') for n in a.SPEC['spot_candidates']}}
        for row in body['results'].values():
            p = cal['profiles'][row['candidate']]
            row['risk_calibration'] = dict(p, sha256='b' * 64)
            row['research_identity'].update(calibration_sha256='b' * 64, risk_scale=p['scale'])
        body['risk_calibration_sha256'] = 'b' * 64
        path = self.root / 'risk.json'
        def consume(digest='b' * 64):
            path.write_text(json.dumps(body))
            with patch.object(a, 'verify_source'):
                return a.consume(path, 'spot', self.env, [], None, [], [], calibration=cal, calibration_sha=digest)
        self.assertEqual(len(consume()['accounts']), 7)
        with self.assertRaisesRegex(ValueError, 'raw hash'):
            consume('c' * 64)
        body['results']['trend-reentry-base']['risk_calibration']['scale'] = '.6'
        with self.assertRaisesRegex(ValueError, 'row calibration'):
            consume()

    def test_forward_binding_accepts_proved_doc_commit_and_rejects_python_change(self):
        report = {'rules_freeze_ready': True, 'registered_work_pending': False,
                  'spec_sha256': a.sha(a.SPEC_PATH), 'protocol_sha256': a.sha(a.PROTOCOL_PATH),
                  'inputs': {'spot': {'metadata': {'source': self.source}}}, 'analysis_source': self.source,
                  'selection': {'spot': {'selected_research_candidate': 'consensus'}},
                  'baseline_verification': {'spot': {'raw_sha256': 'b' * 64}}}
        path = self.root / 'report.json'
        path.write_text(json.dumps(report))
        current = dict(self.source, git_head='2' * 40)
        with patch.object(a, 'current_source', return_value=current):
            binding, source = a.forward_binding(path, 'spot')
            self.assertEqual(binding['source'], self.source)
            self.assertEqual(source, current)
        with patch.object(a, 'current_source', return_value=dict(current, python_sources_sha256='c' * 64)):
            with self.assertRaisesRegex(ValueError, 'not equivalent'):
                a.forward_binding(path, 'spot')

    def test_lossless_gzip_hashes_original_bytes_and_rejects_duplicate_keys(self):
        plain, zipped = self.root / 'a.json', self.root / 'a.json.gz'
        plain.write_text('{"x":1}')
        zipped.write_bytes(gzip.compress(plain.read_bytes(), mtime=0))
        p, ph = a.read_json(plain)
        z, zh = a.read_json(zipped)
        self.assertEqual(p, z)
        self.assertNotEqual(ph, zh)
        plain.write_text('{"x":1,"x":2}')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            a.read_json(plain)

    def curve(self, multiplier=1):
        values, value = [], 100.0
        returns = [.01, -.02, .015, -.005, .012, -.008] * 2
        for i, ret in enumerate(returns):
            value *= 1 + ret * multiplier
            values.append({'day_ms': a.CUTOFF + (i - 6) * a.DAY, 'equity_usdt': value})
        return values, returns

    def test_training_only_scale_is_independent_of_future_values_and_self_unity(self):
        baseline, market = self.curve()
        candidate, _ = self.curve(2)
        scale, _ = a.calibrate(candidate, baseline, market, 100)
        self.assertAlmostEqual(float(scale), .5)
        changed = copy.deepcopy(candidate)
        for row in changed[6:]:
            row['equity_usdt'] *= 1000
        self.assertEqual(a.calibrate(candidate, baseline, market, 100), a.calibrate(changed, baseline, market, 100))
        self.assertEqual(a.calibrate(baseline, baseline, market, 100)[0], '1.0')

    def test_actual_validation_bad_risk_is_never_labeled_matched_alpha(self):
        baseline, market = self.curve()
        candidate, _ = self.curve(2)
        bad = a.achieved_match(candidate, baseline, market, 100)
        self.assertFalse(bad['achieved_match'])
        self.assertEqual(bad['classification'], 'risk_match_failed_or_unidentifiable')
        self.assertTrue(a.achieved_match(baseline, baseline, market, 100)['achieved_match'])
        self.assertFalse(a.safe_regression([0, 0, 0], [0, 0, 0])['identifiable'])
        self.assertEqual(a.safe_regression([0, 0, 0], [-1, 0, 1])['beta_btc'], 0)

    def selection_rows(self, kind):
        return {n + '/' + s: {'candidate': n, 'valid': True, 'cagr': 1.0 if i == 0 else 1.1,
                              'mdd': .4 if i == 0 else .39}
                for i, n in enumerate(a.SPEC[kind + '_candidates']) for s in a.SPEC[kind + '_scenarios']}

    def test_selection_all_stresses_core_tie_slow_combo_pending_and_failed_fallback(self):
        rows = self.selection_rows('spot')
        result = a.selection(rows, 'spot')
        self.assertEqual(result['selected_research_candidate'], 'trend-reentry')
        self.assertIn('core-slow', result['compatible_components'])
        self.assertNotIn('core-permanent', result['compatible_components'])
        self.assertEqual(result['combination_status'], 'pending_actual_four_stresses')
        for s in a.SPEC['spot_scenarios']:
            rows['combo/' + s] = {'candidate': 'combo', 'valid': True, 'cagr': .1, 'mdd': .7}
        self.assertEqual(a.selection(rows, 'spot')['selected_research_candidate'], 'trend-reentry')
        rows['trend-reentry/outage']['valid'] = False
        self.assertFalse(a.selection(rows, 'spot')['decisions']['trend-reentry']['eligible'])
        rows['consensus/base']['valid'] = False
        self.assertEqual(a.selection(rows, 'spot')['selected_research_candidate'], 'consensus')

    def test_reduced_mdd_cannot_hide_more_than_one_percent_cagr_loss(self):
        rows = self.selection_rows('perp')
        rows['fresh-entry/base'].update(cagr=.98, mdd=.1)
        self.assertFalse(a.selection(rows, 'perp')['decisions']['fresh-entry']['eligible'])
        rows['fresh-entry/base'].update(cagr=.995)
        self.assertTrue(a.selection(rows, 'perp')['decisions']['fresh-entry']['eligible'])
        rows['fresh-entry/trigger-slip']['mdd'] = .5
        self.assertFalse(a.selection(rows, 'perp')['decisions']['fresh-entry']['eligible'])

    def test_outage_uses_actual_filtered_789_and_complete_requires_archive(self):
        # Synthetic 795-start fixture; six starts lie in the registered outage.
        starts = ([a.START_MS + i * a.DAY for i in range(50)] +
                  [1583020800000 + i * a.DAY for i in range(6)] +
                  [1584835200000 + i * a.DAY for i in range(739)])
        actual = [v for v in starts if not 1583020800000 <= v < 1584835200000]
        self.assertEqual(len(actual), 789)
        row = self.spot()['results']['consensus-base']
        row.update(complete=True, cagr=.1, mdd='.2', final_cny='11000', final_usdt='1500',
                   client_events=[], sessions=[{'start_ms': s, 'ended_ms': s + 300000, 'status': 'offline_execution',
                     'errors': [], 'execution_unresolved': False, 'archive_verified': True,
                     'archive_backup_sha256': 'a' * 64, 'elapsed_seconds': 300.0} for s in actual])
        self.assertEqual(a.row_validity(row, 'spot', starts, '10000', 'outage'), [])
        with self.assertRaises(ValueError):
            a.row_validity(row, 'spot', starts, '10000', 'base')
        row['sessions'][0]['archive_verified'] = False
        with self.assertRaisesRegex(ValueError, 'archive'):
            a.row_validity(row, 'spot', starts, '10000', 'outage')

    def evidence_row(self):
        row = self.spot()['results']['consensus-base']
        start = 1578636000000
        identity = 'sq-original'
        order = {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': '100'}
        row.update(complete=True, final_cny='10000', final_usdt='1400', cagr=0, mdd='.1', cash_usdt='1000', btc='.01',
                   audit={'passed': True, 'fees_usdt': '.1', 'cash_from_fills': '1000', 'btc_from_fills': '.01', 'no_deposits': True},
                   fills=[{'id': 1, 'order_id': 1, 'time': start + 2000, 'qty': '.01', 'quote': '100', 'price': '10000',
                           'buyer': True, 'commission': '.00001', 'commission_asset': 'BTC'}], daily={},
                   positions={'30': {'qty': '.01', 'first_ms': start + 2000, 'sell_applied': {}}},
                   allocations=[[identity, json.dumps({'order': order, 'sleeves': [30], 'signal_ms': start - a.DAY}),
                                 'settled', json.dumps(dict(order, id=identity, clientOrderId=identity, orderId=1, executedQty='.01'))]],
                   filters={'blocked': 0, 'missing': 0}, session_error_count=1,
                   client_events=[{'method': 'POST', 'client_id': identity, 'sent_ms': start + 1000,
                                   'received_ms': start + 2000, 'order': order}],
                   sessions=[{'start_ms': start, 'status': 'unknown', 'cycles': 45,
                              'errors': [{'cycle': 45, 'reason': 'session deadline reached'}], 'pending_intents': 0,
                              'elapsed_seconds': 300.1989998817444, 'ended_ms': 1578636300199,
                              'archive_verified': True, 'archive_backup_sha256': 'a' * 64, 'execution_unresolved': False}])
        return row

    def test_original_read_completion_and_actual_write_deadlines_not_epsilon(self):
        row = self.evidence_row()
        a.verify_spot_timing(row)
        start = row['sessions'][0]['start_ms']
        # A write sent 1ms before deadline is allowed to finish 999ms afterward.
        row['client_events'][0].update(sent_ms=start + 299999, received_ms=start + 300999)
        row['sessions'][0].update(ended_ms=start + 300999, elapsed_seconds=(start + 300999) / 1000 - start / 1000)
        a.verify_spot_timing(row)
        row['client_events'][0].update(sent_ms=start + 300000, received_ms=start + 301000)
        with self.assertRaisesRegex(ValueError, 'dispatch outside'):
            a.verify_spot_timing(row)
        row = self.evidence_row()
        row['sessions'][0].update(ended_ms=start + 300200, elapsed_seconds=(start + 300200) / 1000 - start / 1000)
        with self.assertRaisesRegex(ValueError, 'lacks an actual'):
            a.verify_spot_timing(row)
        row = self.evidence_row()
        row['sessions'][0]['elapsed_seconds'] += .000001
        with self.assertRaisesRegex(ValueError, 'elapsed clock'):
            a.verify_spot_timing(row)
        row = self.evidence_row()
        row['client_events'][0]['received_ms'] += 1
        with self.assertRaisesRegex(ValueError, 'latency'):
            a.verify_spot_timing(row)

    def test_full_baseline_evidence_detects_money_ownership_actions_and_clocks(self):
        row = self.evidence_row()
        expected = a.evidence_fingerprints(row, 'spot')
        mutations = [lambda r: r['audit'].update(fees_usdt='99'),
                     lambda r: r['sessions'][0].update(cycles=1),
                     lambda r: r['sessions'][0].update(status='blocked'),
                     lambda r: r['sessions'][0].update(ended_ms=0),
                     lambda r: r.update(client_events=[]),
                     lambda r: r['client_events'][0]['order'].update(quoteOrderQty='1'),
                     lambda r: r.update(positions={}),
                     lambda r: r['allocations'][0].__setitem__(2, 'unknown'),
                     lambda r: r['fills'][0].update(qty='999')]
        for mutate in mutations:
            changed = copy.deepcopy(row)
            mutate(changed)
            self.assertNotEqual(expected, a.evidence_fingerprints(changed, 'spot'))
        changed = json.loads(json.dumps(row).replace('sq-original', 'sq-renamed'))
        changed['sessions'][0]['archive_backup_sha256'] = 'b' * 64
        self.assertEqual(expected, a.evidence_fingerprints(changed, 'spot'))
        changed['sessions'][0]['archive_verified'] = False
        with self.assertRaisesRegex(ValueError, 'archive proof'):
            a.evidence_fingerprints(changed, 'spot')

    def test_original_reference_is_pinned_before_any_source_or_comparison_work(self):
        fake = self.root / 'substitute.json'
        fake.write_text(json.dumps({'plausible': 'substituted reference'}))
        for kind in ('spot', 'perp'):
            with self.assertRaisesRegex(ValueError, 'approved immutable'):
                a.original_baseline(fake, kind, {}, self.env)
        self.assertEqual(a.APPROVED_BASELINES['spot'], 'cf075b86ba5df590e078a7c626c53cb20937be955cfb6ad8d41eae19b78645e8')
        self.assertEqual(a.APPROVED_BASELINES['perp'], '15dd7bfc242d52bf692663179cd1e3867418f8b55a4cad0894d253a8b296f00a')

    def test_invalid_base_keeps_all_ten_obligations_with_no_fabricated_profile(self):
        inventories = []
        for kind in ('spot', 'perp'):
            names = a.SPEC[kind + '_candidates']
            accounts = {n + '/base': {'valid': True, 'rejections': [], 'raw_bundle_sha256': 'a' * 64} for n in names}
            accounts[names[1] + '/base'].update(valid=False, rejections=['measurement_incomplete'])
            inventory, needed, control = a.risk_inventory({'accounts': accounts}, kind)
            inventories.extend(inventory)
            self.assertEqual(inventory[names[1]]['status'], 'not_applicable_due_to_invalid_unscaled_base')
            self.assertEqual(inventory[names[1]]['rejections'], ['measurement_incomplete'])
            self.assertNotIn(names[1] + '/base', needed)
            self.assertIn(names[0] + '/base', needed)
            self.assertEqual(control['status'], 'pending_actual_unity_control')
            accounts[names[0] + '/base'].update(valid=False, rejections=['audit_failed'])
            blocked, _, _ = a.risk_inventory({'accounts': accounts}, kind)
            self.assertEqual(blocked[names[2]]['status'], 'pending_valid_unscaled_baseline_for_calibration')
        self.assertEqual(len(inventories), 10)
        body = self.spot()
        path = self.root / 'missing-profile.json'
        path.write_text(json.dumps(body))
        with patch.object(a, 'verify_source'), self.assertRaisesRegex(ValueError, 'no legal calibration profile'):
            a.consume(path, 'spot', self.env, [], None, [], [], calibration={'profiles': {}})

    def test_final_rejected_inventory_closes_work_without_claiming_validity(self):
        from decimal import Decimal as D
        from types import SimpleNamespace
        from contextlib import ExitStack
        baseline_files = {'spot': self.root / 'spot-original', 'perp': self.root / 'perp-original'}
        bundles = {}
        for kind in ('spot', 'perp'):
            accounts = {n + '/' + scenario: {'candidate': n, 'scenario': scenario, 'valid': False,
                        'rejections': ['measurement_incomplete'], 'cagr': None, 'mdd': None,
                        'raw_bundle_sha256': 'a' * 64}
                        for n in a.SPEC[kind + '_candidates'] for scenario in a.SPEC[kind + '_scenarios']}
            bundles[kind] = {'accounts': accounts, 'raw_sha256': 'a' * 64, 'metadata': {'source': self.source}}
        sensitivities = []
        for index, (capital, offset) in enumerate([('9900', 0), ('10100', 0), ('10000', -60000), ('10000', 60000)]):
            path = self.root / ('sensitivity-' + str(index) + '.json')
            path.write_text(json.dumps({'inputs': {'initial_cny': capital, 'start_offset_ms': offset},
                                        'results': {'incumbent': {'base': {}}}}))
            sensitivities.append(path)
        args = SimpleNamespace(schedule=None, fx=None, market=None, spot='spot', perp='perp',
            calibration=None, calibration_out=self.root / 'calibration.json',
            baseline_spot=baseline_files['spot'], baseline_perp=baseline_files['perp'],
            combo_spot=None, combo_perp=None, risk_spot=None, risk_perp=None,
            sensitivity_perp=sensitivities, out=self.root / 'final.json', csv=None, markdown=None, final=True)
        def consume(path, kind, *args, **kwargs):
            if path in ('spot', 'perp'):
                return copy.deepcopy(bundles[kind])
            return {'accounts': {'incumbent/base': copy.deepcopy(bundles['perp']['accounts']['incumbent/base'])},
                    'metadata': {}, 'raw_sha256': 'b' * 64}
        with ExitStack() as stack:
            for name, value in [('source_identity', self.source), ('environment', self.env), ('load_daily', []),
                                ('PriorFX', lambda timestamp: D(7)), ('market_returns_for', ([], [])),
                                ('passive_controls', {}), ('original_baseline', {'passed': False})]:
                stack.enter_context(patch.object(a, name, return_value=value))
            stack.enter_context(patch.object(a, 'consume', side_effect=consume))
            report = a.assess(args)
        self.assertFalse(report['registered_work_pending'])
        self.assertFalse(report['risk_accounts_pending'])
        self.assertFalse(report['all_measured_accounts_valid'])
        self.assertFalse(report['rules_freeze_ready'])
        self.assertEqual(report['risk_obligation_count'], 10)
        self.assertTrue(all(item['status'] == 'not_applicable_due_to_invalid_unscaled_base'
                            for group in report['risk'].values() for item in group.values()))
        self.assertEqual(json.loads(args.calibration_out.read_text())['profiles'], {})

    def test_causal_sizes_are_actual_and_coin_trigger_gap_has_identity(self):
        spot = [{'event': 'decision', 'opportunity_id': 'op1', 'constraints': [{'reason': 'cash'}],
                 'desired_orders': [{'side': 'BUY', 'sleeves': [30], 'quoteOrderQty': '1000'}],
                 'accepted_orders': [{'side': 'BUY', 'sleeves': [30], 'quoteOrderQty': '100'}]}]
        old = a.diagnose(spot, [], 'spot')
        spot[0]['desired_orders'][0]['quoteOrderQty'] = '999999'
        spot[0]['accepted_orders'][0]['quoteOrderQty'] = '1'
        new = a.diagnose(spot, [], 'spot')
        self.assertNotEqual(old, new)
        self.assertEqual(new['sizing_observations'][0]['values']['quoteOrderQty']['max'], '999999')
        coin = [{'event': 'decision', 'opportunity': 123, 'trigger': {'close': '100'},
                 'decision_mark': '120', 'reason': 'hold', 'constraint': 'target'},
                {'event': 'entry_sizing', 'opportunity': 123, 'desired_btc': '2', 'accepted_btc': '1', 'constraint': 'cash'},
                {'event': 'fill', 'trade': {'orderId': 1, 'side': 'BUY', 'qty': '.5', 'price': '120'}}]
        result = a.diagnose(coin, [], 'perp')
        self.assertEqual(result['decision_vs_trigger_return']['n'], 1)
        self.assertAlmostEqual(result['decision_vs_trigger_return']['mean'], .2)
        self.assertEqual(result['decision_price_gap_observations'][0]['identity']['opportunity'], 123)
        self.assertEqual(result['fill_size_observations'][0]['values']['qty']['sum'], '0.5')

    def test_diagnostic_warmup_distinct_ids_future_tail_and_actual_fill(self):
        events = [{'event': 'opportunity', 'identity': 1, 'at_ms': a.START_MS - a.DAY, 'close': '100'},
                  {'event': 'opportunity', 'identity': 2, 'at_ms': a.START_MS, 'close': '100'},
                  {'event': 'opportunity', 'identity': 2, 'at_ms': a.START_MS, 'close': '100'},
                  {'event': 'fill', 'sleeves': [30], 'qty': '1', 'exit_type': 'stop'}]
        bars = [(a.START_MS + 4 * a.DAY, 0, 0, 0, 110, 0)]
        result = a.diagnose(events, bars, 'perp')
        self.assertEqual(result['distinct_opportunities'], 2)
        self.assertEqual(result['pre_window_opportunities'], 1)
        self.assertEqual(result['post_event_completed_day_closes']['5']['sample_size'], 1)
        self.assertEqual(result['post_event_completed_day_closes']['20']['unavailable_endpoints'], 1)
        self.assertEqual(result['filled']['quantity_btc'], 1)

    def test_explicit_manifest_verifies_raw_hashes_and_rejects_duplicates(self):
        body = self.spot()
        raw = self.root / 'part.json'
        raw.write_text(json.dumps(body))
        manifest = {'format': 'alpha-account-manifest-v1', 'kind': 'spot',
                    'files': [{'path': 'part.json', 'sha256': a.sha(raw)}]}
        result = self.consume(manifest)
        self.assertEqual(len(result['account_source_manifests']), 1)
        manifest['files'] *= 2
        with self.assertRaisesRegex(ValueError, 'duplicate manifest'):
            self.consume(manifest)
        manifest['files'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'raw bytes'):
            self.consume(manifest)

    def test_canonical_financial_cash_path_is_retained_with_real_helpers(self):
        from decimal import Decimal as D
        bars = []
        for index, day in enumerate(range(a.START_MS - 30 * a.DAY, a.END_MS, a.DAY)):
            close = D(100 + index % 7)
            bars.append((day, close, close + 1, close - 1, close, D(1)))
        fx = lambda stamp: D(7)
        cash = D(10000) / D(7) * D('.999')
        final = cash * D(7) * D('.999')
        row = {'daily': [{'timestamp_ms': a.START_MS, 'btc': '0', 'cash_usdt': str(cash)}],
               'final_cny': str(final), 'mdd': '.001999', 'audit': {'fees_usdt': '0'}, 'fills': []}
        curve = a.canonical(row, bars, fx, 10000, 'spot')
        row['cagr'] = a.daily_metrics([r['equity_cny'] for r in curve], 10000)[0]['cagr']
        market, cny = a.market_returns_for(bars, fx)
        result = a.financial(row, curve, bars, fx, market, cny, 10000)
        self.assertEqual(len(curve), (a.END_MS - a.START_MS) // a.DAY)
        self.assertEqual(result['usdt_btc_regression']['beta_btc'], 0)
        self.assertIn('validation_2022_plus', result)
        self.assertEqual(result['fees_usdt'], '0')
        self.assertEqual(result['calendar_2026'], 'partial through 2026-09-19 UTC')
        row['cagr'] = 10
        with self.assertRaisesRegex(ValueError, 'CAGR differs'):
            a.financial(row, curve, bars, fx, market, cny, 10000)

    def test_forward_latest_only_atomic_integrity_future_and_source_mismatch(self):
        ledger = self.root / 'forward.json'
        binding = {'rule': 'consensus', 'source': self.source}
        source2 = dict(self.source, git_head='2' * 40)
        initial = a.CUTOFF
        with patch.object(a, 'forward_binding', return_value=(binding, self.source)), patch.object(a.time, 'time', return_value=initial / 1000):
            body = a.forward_init(ledger, 'analysis', 'spot')
            self.assertEqual(body['actual_account_days'], 0)
            self.assertEqual(body['observations'], [])
            with self.assertRaises(FileExistsError):
                a.forward_init(ledger, 'analysis', 'spot')
        now = initial + 3 * a.DAY + 1000
        bar = {'symbol': 'BTCUSDT', 'interval_ms': a.DAY, 'open_ms': initial + 2 * a.DAY,
               'close_ms': initial + 3 * a.DAY, 'observed_ms': now, 'open': '100', 'high': '110',
               'low': '90', 'close': '105', 'public_source': 'https://data.binance.vision', 'public_payload_sha256': 'a' * 64}
        with patch.object(a, 'forward_binding', return_value=(binding, source2)), patch.object(a.time, 'time', return_value=now / 1000):
            for change in ({'observed_ms': now + 1}, {'close_ms': initial + a.DAY}, {'symbol': 'ETHUSDT'}):
                with self.assertRaises(ValueError):
                    a.forward_append(ledger, 'analysis', 'spot', dict(bar, **change))
            body = a.forward_append(ledger, 'analysis', 'spot', bar)
            self.assertEqual(body['observations'][0]['skipped_intervals'], 2)
            self.assertEqual(body['observations'][0]['execution_source']['git_head'], '2' * 40)
            self.assertEqual(body['binding']['source']['git_head'], '1' * 40)
            self.assertEqual(body['actual_account_days'], 0)
            with self.assertRaisesRegex(ValueError, 'strictly advance'):
                a.forward_append(ledger, 'analysis', 'spot', bar)
        with patch.object(a, 'forward_binding', return_value=({'rule': 'different'}, source2)):
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                a.forward_append(ledger, 'analysis', 'spot', bar)
        wrapped = json.loads(ledger.read_text())
        wrapped['body']['cash_cny'] = '1'
        ledger.write_text(json.dumps(wrapped))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            a.forward_append(ledger, 'analysis', 'spot', bar)


if __name__ == '__main__':
    unittest.main()
