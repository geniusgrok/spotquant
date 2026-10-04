"""Pure affected checks: no State, Lifecycle, account initialization, or HTTP."""
import copy
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from spotquant import crowding as c, preview, session
from spotquant.types import Blocked, floor_step
from research import edge_forward as f, edge_spot, adoption_spot
from test_alpha_spot import views_at


class Features:
    def __init__(self, now, funding='.0004', basis='.02'):
        self.now, self.funding, self.basis = now, funding, basis
        self.sha256 = 'a' * 64
        self.filters = {'blocked': 0, 'missing': 0}

    def value(self, name, now):
        value = self.funding if name == 'funding' else self.basis
        boundary = now // c.DAY * c.DAY
        self.last_lookup = dict(name=name, value=value, cause='unavailable' if value is None else None,
                                observation_ms=now - c.FUNDING_LAG if name == 'funding' else boundary,
                                available_ms=now if name == 'funding' else boundary + c.BASIS_LAG)
        return D(value) if value is not None else None


class CanonicalCrowdingTests(unittest.TestCase):
    def setUp(self):
        self.views = views_at()
        for v in self.views.values():
            v.closes[-6] = v.closes[-1]
        self.now = self.views[30].last + c.DAY + 120000
        self.owned = {w: D(0) for w in self.views}
        self.snap = dict(usdt_free='1000', btc='0', avg_price='100', open_orders=0)
        self.kw = dict(positions={}, owners={}, entries_enabled=True, capital_limit=D(10000))

    def decide(self, features=None, **kw):
        return preview.decision(self.views, self.owned, self.snap, **dict(self.kw, **kw),
                                crowding_source=features, decision_ms=self.now)

    def test_shared_research_predicate_and_exactly_one_half_size(self):
        source = Features(self.now)
        canonical = self.decide(source)
        policy = edge_spot.Policy('crowding-interaction', SimpleNamespace(now_ms=self.now), source)
        historical = policy(self.views, self.owned, self.snap, **self.kw)
        original = preview.atr_decision(self.views, self.owned, self.snap, **self.kw)
        self.assertTrue(original['orders'])
        self.assertEqual(canonical['orders'], historical['orders'])
        self.assertEqual(canonical['protections'], historical['protections'])
        self.assertEqual(D(canonical['orders'][0]['quoteOrderQty']), floor_step(D(original['orders'][0]['quoteOrderQty']) / 2, preview.QUOTE_STEP))
        self.assertEqual(canonical['crowding'][0]['scale'], '0.5')

    def test_strict_thresholds_and_strong_momentum_leave_original_size(self):
        original = preview.atr_decision(self.views, self.owned, self.snap, **self.kw)
        for funding, basis in [('.0003', '.02'), ('.0004', '.01'), ('-.0001', '.02')]:
            with self.subTest(funding=funding, basis=basis):
                self.assertEqual(self.decide(Features(self.now, funding, basis))['orders'], original['orders'])
        self.views[30].closes[-6] -= 1
        self.assertEqual(self.decide(Features(self.now))['orders'], original['orders'])

    def test_missing_blocks_new_risk_and_never_adds_held_risk(self):
        missing = self.decide(Features(self.now, funding=None))
        self.assertEqual(missing['orders'], [])
        self.assertEqual(missing['crowding'][0]['blocked_reason'], 'missing_causal_crowding_or_momentum')
        self.assertEqual(self.decide()['orders'], [])
        self.owned[30] = D('.000001')
        held = self.decide(Features(self.now))
        self.assertFalse(any(o['side'] == 'BUY' and 30 in o['sleeves'] for o in held['orders']))

    def test_causal_expiry_jitter_prior_date_and_future_bar(self):
        stamp = self.now - c.FUNDING_LAG + 17
        record = dict(observation_ms=stamp, available_ms=stamp + c.FUNDING_LAG, value='-.0004')
        self.assertEqual(c.value_at('funding', record, stamp + c.FUNDING_LAG - 1)[1], 'not_yet_available')
        self.assertEqual(c.value_at('funding', record, stamp + c.FUNDING_LAG)[0], D('-.0004'))
        self.assertIsNotNone(c.value_at('funding', record, stamp + 2 * c.FUNDING_LAG - 1)[0])
        self.assertEqual(c.value_at('funding', record, stamp + 2 * c.FUNDING_LAG)[1], 'stale_funding')
        day = self.now // c.DAY * c.DAY
        record = dict(observation_ms=day, available_ms=day + 60000, value='.02')
        self.assertEqual(c.value_at('basis', record, day + 59999)[1], 'not_yet_available')
        self.assertEqual(c.value_at('basis', record, day + c.DAY)[1], 'basis_availability_date_mismatch')
        self.views[30].last = self.now
        self.assertEqual(self.decide(Features(self.now))['orders'], [])

    def test_missing_inputs_preserve_safety_exit_and_native_stop_floor(self):
        self.owned[30] = D(1)
        self.views[30].note_entry(100, 100)
        self.views[30].bull = False
        position = dict(qty='1', peak='100', first_ms=self.views[30].last, repair=False)
        owner = dict(sleeves=[30], signal_ms=self.views[30].last, native_status='NEW',
                     order=dict(type='STOP_LOSS', stopPrice='99'))
        self.snap['btc'] = '1'
        kw = dict(self.kw, positions={30: position}, owners={'stop': owner})
        original = preview.atr_decision(self.views, self.owned, self.snap, **kw)
        result = self.decide(None, positions=kw['positions'], owners=kw['owners'])
        self.assertTrue(any(o['side'] == 'SELL' for o in original['orders']))
        self.assertEqual(result['orders'], original['orders'])
        self.assertEqual([p for p in result['protections'] if 'quantity' in p],
                         [p for p in original['protections'] if 'quantity' in p])
        self.assertEqual(preview.decision_view(self.views[30], position, {'stop': owner})._stop_floor, D(99))

    def test_rule_guard_precedes_lifecycle_recovery(self):
        state = SimpleNamespace(get=lambda key: '2026-10-02-atr-stop' if key == 'rule' else None)
        with patch('spotquant.execution.Lifecycle') as lifecycle:
            with self.assertRaises(Blocked): session.cycle(object(), state, object(), execute=True)
            lifecycle.assert_not_called()
        self.assertEqual(session.RULE, c.RULE)

    def bar(self, start, close):
        return [start, close, close, close, close, '1', start + c.DAY - 1, '1', 1, '1', '1', '0']

    def observations(self):
        day = self.now // c.DAY * c.DAY
        bodies = {'funding': [{'symbol': 'BTCUSDT', 'fundingTime': self.now - c.FUNDING_LAG,
                               'fundingRate': '.0004'}],
                  'spot_bars': [self.bar(day - c.DAY, '100'), self.bar(day, '999999')],
                  'futures_bars': [self.bar(day - c.DAY, '102'), self.bar(day, '1')]}
        return [dict(category=k, url=c.PUBLIC_URLS[k], request_ms=self.now - 20,
                     receipt_ms=self.now - 10, sha256=f.sha(f.dump(body)), body=body) for k, body in bodies.items()]

    def test_actual_public_pair_and_settlement_ignore_running_bars(self):
        observed = c.ObservedFeatures(self.observations())
        self.assertEqual(observed.value('funding', self.now), D('.0004'))
        self.assertEqual(observed.value('basis', self.now), D('.02'))
        self.assertEqual(len(observed.last_lookup['provenance']), 2)
        self.assertEqual(observed.last_lookup['observation_ms'], self.now // c.DAY * c.DAY)
        missing = c.ObservedFeatures(self.observations()[:-1])
        self.assertIsNone(missing.value('basis', self.now))
        failed = self.observations(); failed[0]['error'] = 'HTTPError'
        self.assertIsNone(c.ObservedFeatures(failed).value('funding', self.now))

    def test_public_future_receipt_conflicting_settlement_and_bad_pair_block(self):
        for mutate in (lambda rows: rows[0].update(receipt_ms=self.now + 1),
                       lambda rows: rows[0]['body'].append(dict(rows[0]['body'][0], fundingRate='.1')),
                       lambda rows: rows[0]['body'][0].update(fundingTime=self.now + 1)):
            rows = self.observations(); mutate(rows)
            self.assertIsNone(c.ObservedFeatures(rows).value('funding', self.now))
        rows = self.observations(); rows[2]['body'][0][6] -= 1
        self.assertIsNone(c.ObservedFeatures(rows).value('basis', self.now))

    def test_forward_exact_raw_source_and_native_decision_agree(self):
        self.views = views_at()  # Genuine checkpoint history, including recomputed SMA.
        with tempfile.TemporaryDirectory() as tmp:
            receipts = []
            for index, observation in enumerate(self.observations()):
                path = Path(tmp) / str(index); path.write_bytes(f.dump(observation['body']))
                receipt = {k: v for k, v in observation.items() if k != 'body'}
                receipt.update(path=str(path), category=f.endpoint(observation['url']))
                receipts.append(receipt)
            source = f.crowding_from(receipts, self.now)
            book = dict(missing=['depth'], mark='100', crowding_source=source)
            state = dict(engine=dict(models={str(w): v.checkpoint() for w, v in self.views.items()}, positions={}, owners={}),
                         btc='0', wallet='1000', initial_wallet='10000', unresolved=[])
            proposal, money, _ = f.spot_decide(state, [{'close': '100'}], book, self.now, True)
            # Model restoration recomputes the real completed history; use it on both paths.
            from spotquant.model import Model
            restored = {w: Model.restore(v.checkpoint()) for w, v in self.views.items()}
            direct = preview.decision(restored, self.owned, self.snap, **self.kw,
                                      crowding_source=source, decision_ms=self.now)
            self.assertEqual(proposal['orders'], direct['orders'])
            self.assertEqual(proposal['crowding'], direct['crowding'])
            self.assertEqual(money, [])
            f.dump(proposal)  # exact forward serialization, no Decimal escape hatch
            Path(receipts[0]['path']).write_bytes(b'[]')
            with self.assertRaises(ValueError): f.crowding_from(receipts, self.now)

    def test_public_failure_receipt_retained_and_predicted_rate_rejected(self):
        url = c.PUBLIC_URLS['funding']
        with tempfile.TemporaryDirectory() as tmp, patch.object(f, 'now_ms', side_effect=[self.now - 20, self.now - 10]), \
                patch.object(f, 'urlopen', side_effect=HTTPError(url, 451, 'unavailable', {}, io.BytesIO(b'<html>access denied</html>'))):
            receipt = f.acquire(url, tmp)
            self.assertEqual(receipt['error'], 'HTTPError')
            self.assertEqual(f.payload(receipt), b'<html>access denied</html>')
            self.assertEqual(receipt['http_status'], 451)
            self.assertIsNone(f.crowding_from([receipt], self.now).value('funding', self.now))
        with self.assertRaises(ValueError): f.endpoint('https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT')

    def test_unity_risk_profile_accepts_decimal_one_and_rejects_changed_size(self):
        with patch.object(adoption_spot, 'calibration', return_value={'scale': '1.0', 'sha256': None}):
            self.assertEqual(adoption_spot.risk_identity()['scale'], '1.0')
        with patch.object(adoption_spot, 'calibration', return_value={'scale': '.9', 'sha256': None}):
            with self.assertRaises(ValueError): adoption_spot.risk_identity()

    def test_native_public_collection_caches_actual_receipts_and_honors_deadline(self):
        bodies = {r['url']: f.dump(r['body']) for r in self.observations()}
        class Response(io.BytesIO):
            def __init__(self, url):
                super().__init__(bodies[url]); self.url = url
            def geturl(self): return self.url
        source = c.PublicFeatures()
        with patch.object(c, 'urlopen', side_effect=lambda url, timeout: Response(url)) as opener, \
                patch.object(c.time, 'time_ns', return_value=(self.now - 1) * 1000000):
            source.refresh(self.now - 1)
            self.assertEqual(source.value('basis', self.now), D('.02'))
            self.assertEqual(source.value('funding', self.now), D('.0004'))
            source.refresh(self.now)
            self.assertEqual(opener.call_count, 3)
        with patch.object(c, 'urlopen') as opener, patch.object(c.time, 'time_ns', return_value=self.now * 1000000):
            source.refresh(self.now + 60000, lambda: True)
            opener.assert_not_called()
            self.assertIsNone(source.value('funding', self.now + 60000))

    def test_old_atr_canonical_calibration_and_forward_adapter_rejected(self):
        from research.alpha_spot import CUTOFF, SPEC, digest
        old_profile = dict(scale='.5', effective_from_ms=CUTOFF, calibration_end_ms=CUTOFF,
                           training_end_day_exclusive='2022-01-01', base_bundle_sha256='a' * 64,
                           baseline_candidate='consensus')
        old_document = dict(format=1, cutoff_ms=CUTOFF, spec_sha256=digest(SPEC.read_bytes()),
                            profiles={'atr-stop': old_profile})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old-atr-calibration.json'
            path.write_text(json.dumps(old_document))
            with self.assertRaises(ValueError): adoption_spot.risk_identity(path)
        with self.assertRaisesRegex(ValueError, 'unsupported canonical Spot selection'):
            f.verify_spot_bridge(dict(candidate='atr-stop', adapter='canonical-incumbent-v1',
                                     components=[], scale='1'), {})

    def malformed_receipts(self, directory, category, raw):
        receipts = []
        for index, observation in enumerate(self.observations()):
            body = raw if observation['category'] == category else f.dump(observation['body'])
            path = Path(directory) / str(index); path.write_bytes(body)
            receipt = {k: v for k, v in observation.items() if k != 'body'}
            receipt.update(path=str(path), category=f.endpoint(observation['url']), sha256=f.sha(body))
            receipts.append(receipt)
        return receipts

    def test_malformed_crowding_missing_blocks_buy_but_safety_processing_continues(self):
        class SafetyReached(Exception): pass
        variants = (b'<html>temporarily unavailable</html>', b'{"value":NaN}',
                    b'\xff', b'{"value":1,"value":2}')
        for category, name in (('funding', 'funding'), ('futures_bars', 'basis')):
            for raw in variants:
                with self.subTest(category=category, raw=raw), tempfile.TemporaryDirectory() as directory:
                    receipts = self.malformed_receipts(directory, category, raw)
                    source = f.crowding_from(receipts, self.now)
                    self.assertIsNone(source.value(name, self.now))
                    missing = copy.deepcopy(source.last_lookup)
                    self.assertIn('public_endpoint_', missing['cause'])
                    self.assertIn(f.sha(raw), [p['sha256'] for p in missing['provenance']])
                    self.assertEqual(self.decide(source)['orders'], [])
                    # Native collection and forward replay use the same missing conversion.
                    native = c.PublicFeatures()
                    native.observations = [dict(r, **c.public_body(raw)) if r['category'] == category else r
                                           for r in self.observations()]
                    self.assertIsNone(native.value(name, self.now))
                    self.assertEqual(native.last_lookup['cause'], missing['cause'])
                    replay = f.crowding_from(receipts, self.now)
                    self.assertIsNone(replay.value(name, self.now))
                    self.assertEqual(replay.last_lookup, missing)
                    # Real shared safety decision, with no State/Lifecycle/account setup.
                    views = copy.deepcopy(self.views)
                    views[30].note_entry(100, 100); views[30].bull = False
                    owned = dict(self.owned)
                    owned[30] = D(1)
                    safety = preview.decision(views, owned, dict(self.snap, btc='1'),
                        **dict(self.kw, positions={30: dict(qty='1', peak='100', first_ms=views[30].last)}),
                        crowding_source=source, decision_ms=self.now)
                    self.assertEqual([o['side'] for o in safety['orders']], ['SELL'])
                    # Stop the pure mocked transition at the passive safety handler:
                    # malformed features must not abort before it is reached.
                    def receipt(url, content, suffix):
                        path = Path(directory) / suffix; path.write_bytes(content)
                        return dict(category=f.endpoint(url), url=url, path=str(path), sha256=f.sha(content),
                                    request_ms=self.now - 20, receipt_ms=self.now - 10)
                    fx = receipt(f.FX_URL, b'FX fixture', 'fx')
                    fx['metadata'] = receipt(f.FX_METADATA_URL, b'FX metadata fixture', 'fx-metadata')
                    latest = self.now // c.DAY * c.DAY
                    state = dict(wallet='1000', btc='1', fees='0', funding='0', market_ready=True,
                                 last_interval=latest - c.DAY, initialized_ms=latest - 2*c.DAY,
                                 market_ready_at_ms=latest - 2*c.DAY)
                    def safety_handler(state, book, call):
                        self.assertIsNone(book['crowding_source'].value(name, call))
                        raise SafetyReached
                    with patch.object(f, 'book_from', return_value={}), patch.object(f, 'fx_from', return_value={}), \
                            patch.object(f, 'passive_fills', side_effect=safety_handler) as passive:
                        with self.assertRaises(SafetyReached):
                            f.apply_observation(state, receipts + [fx], self.now, self.now, False, {})
                        passive.assert_called_once()

    def test_crowding_missing_conversion_keeps_receipt_integrity_and_spot_bars_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            receipts = self.malformed_receipts(directory, 'funding', b'not-json')
            corrupt = copy.deepcopy(receipts); corrupt[0]['sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'provenance'): f.crowding_from(corrupt, self.now)
            corrupt = copy.deepcopy(receipts); corrupt[0]['url'] = c.PUBLIC_URLS['futures_bars']
            with self.assertRaisesRegex(ValueError, 'provenance'): f.crowding_from(corrupt, self.now)
            receipts = self.malformed_receipts(directory, 'spot_bars', b'not-json')
            with self.assertRaises(ValueError): f.crowding_from(receipts, self.now)


# Historical entry/ATR/crowding scenarios, separate from the actual target-core default.
from legacy_policy import legacy_policy
setUpModule, tearDownModule = legacy_policy()
