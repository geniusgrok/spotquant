"""Canonical ATR decision and durable migration boundaries, entirely offline."""
import copy
from decimal import Decimal as D
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from spotquant import model, preview, session
from spotquant.config import Config
from spotquant.model import DAY, ORIGIN, Model
from spotquant.offline import P4Venue
from spotquant.state import State
from spotquant.types import Blocked


def checkpoint_hash(saved):
    saved['sha256'] = hashlib.sha256(json.dumps(saved['body'], sort_keys=True).encode()).hexdigest()
    return saved


def models():
    result = {}
    for w in (30, 40, 50):
        m = Model(w)
        for i, p in enumerate([D(90)] * 400 + [D(100), D(100)]):
            m.update(ORIGIN + i * DAY, p, p, p)
        result[w] = m
    return result


class AdoptionTests(unittest.TestCase):
    def test_completed_atr_checkpoint_and_chronology(self):
        m = Model()
        for i in range(15):
            m.update(ORIGIN + i * DAY, D(100 + i + 2), D(100 + i - 1), D(100 + i))
        self.assertEqual(getattr(m, 'atr14', None), D(3))
        saved = m.checkpoint()
        self.assertEqual(Model.restore(saved).checkpoint(), saved)
        self.assertEqual(m.trail, D('.28'))
        for mutate in [lambda b: b.update(version=4),
                       lambda b: b['true_ranges'].pop(),
                       lambda b: b['true_ranges'][0].__setitem__(1, 'NaN'),
                       lambda b: b['true_ranges'][0].__setitem__(0, ORIGIN),
                       lambda b: b['true_ranges'].reverse(),
                       lambda b: b.update(true_ranges='3' * 14),
                       lambda b: b.update(last=ORIGIN + 13 * DAY)]:
            broken = copy.deepcopy(saved)
            mutate(broken['body'])
            with self.assertRaises(Blocked):
                Model.restore(checkpoint_hash(broken))
        m.update(ORIGIN + 15 * DAY, D(1000), D(1), D(100))
        self.assertEqual(Model.restore(saved).atr14, D(3))

    def test_atr_counts_the_previous_close_gap_and_waits_for_fourteen_ranges(self):
        m = Model()
        m.update(ORIGIN, D(100), D(100), D(100))
        m.update(ORIGIN + DAY, D(201), D(199), D(200))
        for day in range(2, 14):
            m.update(ORIGIN + day * DAY, D(202), D(198), D(200))
        self.assertIsNone(m.atr14)
        m.update(ORIGIN + 14 * DAY, D(202), D(198), D(200))
        self.assertEqual(m.atr14, (D(101) + 13 * D(4)) / 14)
        m.update(ORIGIN + 15 * DAY, D(202), D(198), D(200))
        self.assertEqual(m.atr14, D(4))

    def test_legacy_flat_held_and_pending_reject_before_recovery(self):
        for held, pending in [(False, False), (True, False), (False, True)]:
            with self.subTest(held=held, pending=pending), tempfile.TemporaryDirectory() as directory:
                cfg = Config('1', directory, 2, 1, 'demo', '1000')
                venue = P4Venue([(ORIGIN, D(100), D(100), D(100))])
                with State(directory, cfg.scope) as state:
                    state.set_many({'rule': '2026-09-29-static-stop',
                                    'models': {str(w): m.checkpoint() for w, m in models().items()},
                                    'positions': {'30': {'qty': '1'} if held else None}})
                    if pending:
                        state.db.execute("INSERT INTO intents VALUES ('old','p4','{}','unknown','{}',0)")
                        state.db.commit()
                    before = list(state.db.iterdump())
                    with patch('spotquant.execution.Lifecycle.recover') as recover:
                        with self.assertRaises(Blocked):
                            session.cycle(venue, state, cfg, execute=True)
                        recover.assert_not_called()
                    self.assertEqual(list(state.db.iterdump()), before)
                    self.assertEqual(venue.sent, [])

    def test_decision_floor_actual_peak_and_through_mark_exit(self):
        views = models()
        position = dict(first_ms=ORIGIN + 401 * DAY, peak='105')
        views[30].note_entry(100, 105)
        views[30].protection = 'breached'
        owners = {'1': dict(sleeves=[30], order=dict(type='STOP_LOSS', stopPrice='97'),
                           native_status='CANCELED', signal_ms=ORIGIN + 400 * DAY)}
        self.assertTrue(hasattr(preview, 'decision_view'))
        view = preview.decision_view(views[30], position, owners)
        self.assertEqual(view.stop_price(D(105)), D(97))
        self.assertEqual(view.position_peak, D(105))
        self.assertEqual(view.protection, 'resting')
        self.assertEqual(views[30].trail, D('.28'))
        self.assertEqual(views[30].protection, 'breached')
        snap = dict(btc='1', usdt_free='100', open_orders=0, avg_price='96')
        decision = preview.decision(views, {30: D(1), 40: D(0), 50: D(0)}, snap,
                                    positions={30: position}, owners=owners,
                                    entries_enabled=True, capital_limit=D(1000))
        self.assertEqual([o['side'] for o in decision['orders']], ['SELL'])
        self.assertEqual(decision['orders'][0]['quantity'], '1')
        self.assertEqual(decision['sleeves']['30']['action'], 'exit')
        self.assertEqual(decision['sleeves']['40']['action'], 'flat')

    def test_malformed_checkpoint_or_pending_payload_blocks_recovery(self):
        for corruption in ('version', 'queue', 'parameters', 'research', 'pending', 'missing', 'positions'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                cfg = Config('1', directory, 2, 1, 'demo', '1000')
                venue = P4Venue([(ORIGIN, D(100), D(100), D(100))])
                with State(directory, cfg.scope) as state:
                    saved = {str(w): m.checkpoint() for w, m in models().items()}
                    if corruption == 'version':
                        saved['30']['body']['version'] = 4
                    if corruption == 'queue':
                        saved['30']['body']['true_ranges'] = []
                    if corruption == 'parameters':
                        saved['30']['body']['trail'] = '.20'
                    checkpoint_hash(saved['30'])
                    if corruption == 'research':
                        saved['30'] = {'model': saved['30'], 'identity': {'candidate': 'atr-stop'}}
                    state.set_many({'rule': session.RULE, 'models': None if corruption == 'missing' else saved,
                                    'positions': None if corruption == 'positions' else {str(w): None for w in (30, 40, 50)},
                                    'follows': {str(w): None for w in (30, 40, 50)},
                                    'entries_after': ORIGIN + 401 * DAY})
                    if corruption != 'positions':
                        state.db.execute("INSERT INTO intents VALUES ('old','p4','{}','unknown','{}',0)")
                    state.db.commit()
                    before = list(state.db.iterdump())
                    with patch('spotquant.execution.Lifecycle.recover') as recover:
                        with self.assertRaises(Blocked):
                            session.cycle(venue, state, cfg, execute=True)
                        recover.assert_not_called()
                    self.assertEqual(list(state.db.iterdump()), before)

    def test_canonical_meter_rejects_surrounding_research_hook(self):
        from research import alpha_spot
        from research.adoption_spot import measure
        from research.session_account import HistoricalVenue
        from test_alpha_spot import bars
        venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        with alpha_spot.configured(alpha_spot.Policy('atr-stop', venue)):
            with self.assertRaisesRegex(ValueError, 'canonical'):
                measure('base', bars(), [ORIGIN + 401 * DAY], lambda t: D(7), limit=1)

    def test_native_floor_status_and_campaign_boundary(self):
        view = models()[30]
        position = {'first_ms': ORIGIN + 401 * DAY + 60000}
        owner = dict(sleeves=[30], order=dict(type='STOP_LOSS', stopPrice='99'),
                     native_status='NEW', signal_ms=ORIGIN + 400 * DAY)
        for status in ('NEW', 'PARTIALLY_FILLED', 'CANCELED', 'FILLED', 'EXPIRED', 'EXPIRED_IN_MATCH'):
            owner['native_status'] = status
            self.assertEqual(preview.decision_view(view, position, {'1': owner}).stop_price(D(100)), D(99))
        for change in ({'native_status': 'REJECTED'}, {'native_status': None},
                       {'signal_ms': ORIGIN + 400 * DAY - 1}, {'sleeves': [40]}):
            ignored = dict(owner, native_status='NEW')
            ignored.update(change)
            self.assertEqual(preview.decision_view(view, position, {'1': ignored}).stop_price(D(100)), D(90))
        self.assertEqual(preview.decision_view(view, None, {'1': owner}).stop_price(D(100)), D(90))

    def test_canonical_synthetic_account_matches_selected_research_all_six_groups(self):
        from research import alpha_spot
        from research.adoption_spot import measure
        from research.alpha_assessment import evidence_fingerprints
        from test_alpha_spot import bars
        rows = bars()
        starts = [ORIGIN + i * DAY for i in (401, 402, 403)]
        original = alpha_spot.measure('atr-stop', 'base', rows, starts, lambda t: D(7), limit=3)
        adopted = measure('base', rows, starts, lambda t: D(7), limit=3)
        self.assertEqual(evidence_fingerprints(original, 'spot'), evidence_fingerprints(adopted, 'spot'))

    def test_canonical_meter_never_installs_research_decisions(self):
        from research import complete_spot
        self.assertTrue('canonical' in __import__('inspect').signature(complete_spot.measure).parameters)
        from research.adoption_spot import measure
        from test_alpha_spot import bars
        starts = [ORIGIN + i * DAY for i in (401, 402, 403)]
        with patch.object(complete_spot, 'Policy', side_effect=AssertionError('research policy')), \
                patch.object(complete_spot, 'configured', side_effect=AssertionError('research hooks')), \
                patch('research.alpha_spot.Policy', side_effect=AssertionError('alpha policy')), \
                patch('research.alpha_spot.configured', side_effect=AssertionError('alpha hooks')):
            result = measure('base', bars(), starts, lambda t: D(7), limit=3)
        self.assertTrue(result['audit']['passed'])
        self.assertTrue(result['fills'])
        self.assertEqual(result['execution_unresolved_sessions'], 0)
        self.assertEqual(result['filters'], {'blocked': 0, 'missing': 0})
        self.assertTrue(all(r['archive_verified'] for r in result['sessions']))
        self.assertIs(session.Model, model.Model)
        self.assertIs(session.State, State)
        self.assertIs(session.portfolio, preview.decision)

    def test_diagnostic_profile_cutoff_owned_sizes_and_rerun_binding(self):
        from research import complete_spot
        self.assertTrue('canonical' in __import__('inspect').signature(complete_spot.measure).parameters)
        from pathlib import Path
        from research.adoption_spot import risk_identity
        from research.alpha_spot import CUTOFF, SPEC, digest
        profile = dict(scale='.5', effective_from_ms=CUTOFF, calibration_end_ms=CUTOFF,
                       training_end_day_exclusive='2022-01-01', base_bundle_sha256='a' * 64,
                       baseline_candidate='consensus')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'risk.json'
            path.write_text(json.dumps(dict(format=1, cutoff_ms=CUTOFF,
                spec_sha256=digest(SPEC.read_bytes()), profiles={'atr-stop': profile})))
            identity = risk_identity(path)
            venue = P4Venue([(ORIGIN, D(100), D(100), D(100))])
            venue._adoption_risk = identity
            with State(Path(directory) / 'state', 'test') as state:
                venue.now_ms = CUTOFF - 1
                self.assertEqual(session._allocation_scale(state, venue), D(1))
                venue.now_ms = CUTOFF
                self.assertEqual(session._allocation_scale(state, venue), D('.5'))
                state.set('adoption_risk', identity)
                venue._adoption_risk = dict(identity, scale='.4')
                with self.assertRaises(Blocked):
                    session._allocation_scale(state, venue)
                del venue._adoption_risk
                with self.assertRaises(Blocked):
                    session._allocation_scale(state, venue)
            views = models()
            views[30].note_entry(100, 100)
            owned = {30: D(1), 40: D(0), 50: D(0)}
            snap = dict(btc='1', usdt_free='100', avg_price='100', open_orders=0)
            kw = dict(positions={30: dict(first_ms=views[30].last)}, owners={},
                      entries_enabled=True, capital_limit=D(150))
            full = preview.decision(views, owned, snap, **kw)
            half = preview.decision(views, owned, snap, allocation_scale=D('.5'), **kw)
            self.assertEqual(D(full['orders'][0]['quoteOrderQty']), D(50))
            self.assertEqual(D(half['orders'][0]['quoteOrderQty']), D(25))
            self.assertEqual(full['protections'], half['protections'])
            self.assertEqual(owned[30], D(1))


if __name__ == '__main__':
    unittest.main()
