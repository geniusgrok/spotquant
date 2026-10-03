"""Registered mechanisms, real protected fills, and pre-recovery identity gates."""
import copy
from contextlib import contextmanager
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import adoption_spot, complete_spot, edge_spot as edge
from research.alpha_assessment import evidence_fingerprints
from research.edge_features import FeatureBook
from research.session_account import HistoricalVenue
from spotquant import execution, follow, model, preview, session
from spotquant.config import Config
from spotquant.model import DAY, ORIGIN, Model, SLEEVES
from spotquant.offline import P4Venue
from spotquant.state import State
from spotquant.types import Blocked
from test_alpha_spot import bars, views_at


def snap(cash='1000', btc='0', mark='100'):
    return dict(usdt_free=cash, usdt_locked='0', btc=btc, avg_price=mark, open_orders=0)


def position(qty='1'):
    return dict(qty=qty, first_ms=ORIGIN + 400 * DAY, entry_fill='100', peak='100',
                repair=False, repair_peak=None, adverse=False, through=ORIGIN + 401 * DAY,
                protection='resting', entry_open_ms=ORIGIN + 400 * DAY)


def protection(group=(30,), quantity='1', stop='90'):
    return dict(sleeves=list(group), weights={str(w): '1' for w in group},
                order=dict(type='STOP_LOSS', side='SELL', quantity=quantity, stopPrice=stop),
                native_status='NEW', native_executed_qty='0', signal_ms=ORIGIN + 400 * DAY)


@contextmanager
def policy(candidate, *, features=None, risk=None, now=None):
    venue = HistoricalVenue(bars(), ORIGIN + 402 * DAY, D(1000), lambda t: D(7))
    if now is not None:
        venue.now_ms = now
    p = edge.Policy(candidate, venue, features, risk=risk)
    with tempfile.TemporaryDirectory() as directory, edge.configured(p), session.State(directory, 'test') as state:
        p.state = state
        yield p


def decide(p, views=None, owned=None, snapshot=None, positions=None, owners=None, **kwargs):
    return p(views or views_at(), owned or {30: D(0), 40: D(0), 50: D(0)}, snapshot or snap(),
             positions=positions or {}, owners=owners or {}, entries_enabled=True, capital_limit=D(10000), **kwargs)


def risk_document():
    profiles = {name: dict(candidate=name, project_kind='spot', scale='1' if name == 'atr-stop' else '.4',
                          baseline_candidate='atr-stop', effective_from_ms=edge.CUTOFF, calibration_end_ms=edge.CUTOFF,
                          training_end_day_exclusive='2022-01-01', base_bundle_sha256='a' * 64)
                for name in ('atr-stop', *edge.MECHANISMS)}
    return dict(format=1, project_kind='spot', cutoff_ms=edge.CUTOFF, baseline_candidate='atr-stop',
                spec_sha256=edge.digest(edge.SPEC.read_bytes()), profiles=profiles)


class EdgeSpotTests(unittest.TestCase):
    def test_registered_components_and_minimum_rounding(self):
        self.assertEqual(edge.components_for('combo', ('stop-budget', 'exit-confirm')), ('exit-confirm', 'stop-budget'))
        for candidate, parts in [('combo', ()), ('combo', ('exit-confirm',)),
                                 ('combo', ('stop-budget', 'stop-budget')), ('atr-stop', ('exit-confirm',))]:
            with self.assertRaises(ValueError):
                edge.components_for(candidate, parts)
        with policy('exit-confirm', risk=dict(scale='.004999', sha256=None), now=edge.CUTOFF) as p:
            self.assertEqual(decide(p)['orders'], [])
            self.assertEqual(p.journal[-1]['diagnostics'][-1]['blocked_reason'], 'below_minimum_after_rounding_or_risk_scale')
        with policy('exit-confirm', risk=dict(scale='.005001', sha256=None), now=edge.CUTOFF) as p:
            self.assertEqual(D(decide(p)['orders'][0]['quoteOrderQty']), D(5))

    def test_previous_sma_uses_its_own_completed_window_and_equality_is_below(self):
        v = Model(30)
        for i, price in enumerate([D(100)] * 400 + [D(101), D(99)]):
            v.update(ORIGIN + i * DAY, price, price, price)
        self.assertEqual(edge.previous_sma(v), (29 * D(100) + D(101)) / 30)
        self.assertNotEqual(edge.previous_sma(v), v.sma)
        v.closes = [D(100)] * 30
        self.assertIsNone(edge.previous_sma(v))
        v.closes = [D(100)] * 31
        v.prev_close = D(100)
        self.assertEqual(v.prev_close > edge.previous_sma(v), False)

    def test_exit_confirmation_preserves_bearish_vote_and_safety_priority(self):
        views = views_at()
        v = views[30]
        v.closes = [D(100)] * 400 + [D(101), D(99)]
        v.close, v.prev_close, v.sma, v.bull = D(99), D(101), D(100), False
        v.note_entry(100, 100)
        views[50].bull = views[50].enter = False
        owned = {30: D(1), 40: D(0), 50: D(0)}
        kw = dict(views=views, owned=owned, snapshot=snap(btc='1', mark='99'), positions={30: position()})
        with policy('exit-confirm') as p:
            result = decide(p, **kw)
            self.assertEqual(result['sleeves']['30']['action'], 'hold')
            self.assertFalse(views[30].bull)
            self.assertFalse(p.journal[-1]['bullish_votes']['30'])
            # One true bullish voter receives only its own original flat-sleeve share.
            self.assertEqual(D(result['orders'][0]['quoteOrderQty']), D(500))
            for safety in ('adverse', 'extended', 'protection', 'native'):
                altered = copy.deepcopy(views)
                if safety == 'protection':
                    altered[30].true_ranges.clear()  # Preserve breach without ATR overriding it.
                    altered[30].protection = 'breached'
                elif safety != 'native':
                    setattr(altered[30], safety, True)
                result = decide(p, **dict(kw, views=altered), owners={'1': protection(stop='99')} if safety == 'native' else {})
                self.assertTrue(any(o['side'] == 'SELL' for o in result['orders']), safety)
                self.assertFalse(any(o['side'] == 'BUY' for o in result['orders']), safety)
            v.repair = True
            self.assertEqual(decide(p, **kw)['sleeves']['30']['action'], 'hold')
            v.repair = False
            v.closes = [D(100)] * 30
            self.assertEqual(decide(p, **kw)['sleeves']['30']['action'], 'exit')
            v.closes = [D(100)] * 31
            v.prev_close = D(100)
            self.assertEqual(decide(p, **kw)['sleeves']['30']['action'], 'exit')

    def test_stop_risk_aggregates_allocated_sleeves_and_rounding_dust(self):
        owned = {30: D(1), 40: D(2), 50: D(0)}
        owners = {'1': protection((30, 40), '3', '90')}
        owners['1']['weights'] = {'30': '1', '40': '2'}
        self.assertEqual(edge.existing_risk(owned, snap(btc='3'), {30: position(), 40: position('2')}, owners), D(30))
        for bad in ('missing', 'terminal', 'quantity', 'weights', 'ownership', 'nonfinite'):
            altered = copy.deepcopy(owners)
            if bad == 'missing':
                altered = {}
            elif bad == 'terminal':
                altered['1']['native_status'] = 'CANCELED'
            elif bad == 'quantity':
                altered['1']['order']['quantity'] = '2'
            elif bad == 'weights':
                altered['1']['weights'] = {'30': '1'}
            elif bad == 'ownership':
                altered['1']['signal_ms'] = ORIGIN
            else:
                altered['1']['order']['stopPrice'] = 'NaN'
            with self.subTest(bad=bad), self.assertRaises((ValueError, KeyError)):
                edge.existing_risk(owned, snap(btc='3'), {30: position(), 40: position('2')}, altered)
        with self.assertRaises(ValueError):
            edge.existing_risk(owned, snap(btc='3.1'), {30: position(), 40: position('2')}, owners)
        with self.assertRaises(ValueError):
            edge.existing_risk({30: D('.000001'), 40: D(0), 50: D(0)}, snap(btc='.000001'),
                               {30: dict(position('.000001'), dust=True, sell_applied={'2': '1'})}, {})
        owners['1']['order']['quantity'] = '2.99999'
        self.assertGreater(edge.existing_risk(owned, snap(btc='3'), {30: position(), 40: position('2')}, owners), D('29.9999'))

    def test_stop_budget_caps_original_proposal_accounts_fees_and_blocks_missing(self):
        views = views_at()
        # High completed ATR forces 30% stop; remaining budget is then binding.
        for v in views.values():
            v.true_ranges = [(v.last - i * DAY, D(20)) for i in range(14)]
        owned = {30: D(1), 40: D(0), 50: D(0)}
        views[30].note_entry(100, 100)
        with policy('stop-budget') as p:
            result = decide(p, views, owned, snap(btc='1'), {30: position()}, {'1': protection()})
            diagnostic = next(d for d in p.journal[-1]['diagnostics'] if d['mechanism'] == 'stop-budget')
            self.assertEqual(D(diagnostic['budget']), D(132))
            self.assertEqual(D(diagnostic['existing_risk']), D(10))
            self.assertGreater(D(diagnostic['proposed_loss_per_quote']), D('.3035'))
            quote = D(result['orders'][0]['quoteOrderQty'])
            self.assertLess(quote, D(500))
            self.assertLessEqual(D(10) + quote * D(diagnostic['proposed_loss_per_quote']), D(132))
            self.assertNotIn(30, result['orders'][0]['sleeves'])
            self.assertEqual(result['protections'][0]['stopPrice'], '90.00')
            p.venue.fee = D('.0015')
            stressed = decide(p, views, owned, snap(btc='1'), {30: position()}, {'1': protection()})
            self.assertLess(D(stressed['orders'][0]['quoteOrderQty']), quote)
            blocked = decide(p, views, owned, snap(btc='1'), {30: position()}, {})
            self.assertFalse(any(o['side'] == 'BUY' for o in blocked['orders']))
            views[30].adverse = True
            exit_result = decide(p, views, owned, snap(btc='1'), {30: position()}, {})
            self.assertEqual(exit_result['orders'][0]['side'], 'SELL')
        with policy('stop-budget') as p:
            result = decide(p, snapshot=snap(cash='5'))
            self.assertEqual(result['orders'], [])

    def test_crowding_strict_actual_clock_momentum_and_missing_safety(self):
        # Unit reader records use the real availability lookup, without fabricating an artifact.
        book = FeatureBook.__new__(FeatureBook)
        now = ORIGIN + 402 * DAY + 120000
        book.sha256 = 'a' * 64
        book.source = dict(raw_sha256='b' * 64, market_identities={})
        book.times = {'funding': [now], 'basis': [now]}
        book.records = {'funding': [(now - 28800000, now, D('.0004'), None)],
                        'basis': [(now - 60000, now, D('.02'), None)]}
        views = views_at()
        for v in views.values():
            v.closes = [D(101)] * 400 + [D(100), D(100)]
        with policy('crowding-interaction', features=book, now=now - 1) as p:
            missing = decide(p, views=views)
            self.assertEqual(missing['orders'], [])
            inputs = p.journal[-1]['diagnostics'][0]['inputs']
            self.assertEqual(inputs[0]['cause'], 'not_yet_available')
            p.venue.now_ms = now
            result = decide(p, views=views)
            self.assertEqual(D(result['orders'][0]['quoteOrderQty']), D('499.99'))
            self.assertEqual(book.last_lookup['now_ms'], now)
            views[30].last += DAY
            self.assertEqual(decide(p, views=views)['orders'], [])
            self.assertFalse(p.journal[-1]['diagnostics'][0]['momentum']['causal_completed'])
            views[30].last -= DAY
            views[30].closes[-6] = D(99)
            self.assertEqual(D(decide(p, views=views)['orders'][0]['quoteOrderQty']), D('999.99'))
            p.venue.now_ms = now + 28800000
            self.assertEqual(decide(p, views=views)['orders'], [])
            self.assertEqual(p.journal[-1]['diagnostics'][0]['inputs'][0]['cause'], 'stale_funding')
            views[30].note_entry(100, 100)
            views[30].adverse = True
            self.assertEqual(decide(p, views, {30: D(1), 40: D(0), 50: D(0)}, snap(btc='1'), {30: position()})['orders'][0]['side'], 'SELL')

    def test_unity_baseline_all_six_original_groups_and_budget(self):
        rows, starts = bars(), [ORIGIN + i * DAY for i in (401, 402, 403)]
        direct = adoption_spot.measure('base', rows, starts, lambda t: D(7), limit=3)
        noop = edge.measure('atr-stop', 'base', rows, starts, lambda t: D(7), limit=3)
        self.assertEqual(evidence_fingerprints(direct, 'spot'), evidence_fingerprints(noop, 'spot'))
        self.assertEqual(direct['research_identity'], noop['research_identity'])
        self.assertEqual(noop['candidate'], 'atr-stop')
        smaller = edge.measure('atr-stop', 'base', rows, starts, lambda t: D(7), limit=3, initial_cny=D(2500))
        self.assertEqual(smaller['initial_cny'], '2500')
        self.assertTrue(smaller['audit']['passed'])
        self.assertLess(D(smaller['fills'][0]['quote']), D(noop['fills'][0]['quote']))

    def test_real_cash_and_two_protected_controls_fees_stop_no_same_day_reentry(self):
        rows = bars([D(100)] * 410)
        t = ORIGIN + 403 * DAY
        rows[403] = (t, D(100), D(100), D(60), D(100), D(100000))
        starts = [ORIGIN + 401 * DAY, ORIGIN + 402 * DAY, t + 2 * DAY // 3 + 60000,
                  t + DAY, t + 2 * DAY]
        cash = edge.measure('cash', 'base', rows, starts, lambda stamp: D(7), limit=5)
        self.assertEqual(cash['fills'], [])
        self.assertEqual(cash['btc'], '0')
        for candidate, fraction in [('protected-participation-25', D('.25')), ('protected-participation-100', D(1))]:
            with self.subTest(candidate=candidate):
                result = edge.measure(candidate, 'base', rows, starts, lambda stamp: D(7), limit=5)
                self.assertTrue(result['audit']['passed'])
                self.assertFalse(result['complete'])
                self.assertIsNone(result['cagr'])
                self.assertEqual(result['pending_intents'], [])
                self.assertEqual(result['execution_unresolved_sessions'], 0)
                self.assertTrue(all(s['archive_verified'] for s in result['sessions']))
                buys = [f for f in result['fills'] if f['buyer']]
                self.assertEqual(len(buys), 2)
                self.assertAlmostEqual(float(D(buys[0]['quote']) / (D(10000) / 7 * D('.999'))), float(fraction), places=4)
                self.assertGreater(D(result['audit']['fees_usdt']), 0)
                self.assertGreater(buys[1]['time'], t + 2 * DAY)
                owners = execution.allocation_owners((json.loads(r[1]), json.loads(r[3])) for r in result['allocations'])
                stops = [o for o in owners.values() if o['order']['type'] == 'STOP_LOSS']
                self.assertTrue(any(o['native_status'] == 'FILLED' for o in stops))
                self.assertTrue(any(o['native_status'] == 'NEW' for o in stops))
                first_stop = next(o for o in stops if o['native_status'] == 'FILLED')
                self.assertEqual(D(first_stop['order']['stopPrice']), D('72.03'))

    def test_calibration_all_profiles_strict_fields_kind_boundaries_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'risk.json'
            data = risk_document()
            path.write_text(json.dumps(data))
            self.assertEqual(edge.calibration(path, 'stop-budget')['scale'], '.4')
            for corrupt in ('extra', 'foreign', 'nonfinite', 'cutoff', 'kind', 'hash', 'name', 'baseline', 'missing'):
                changed = copy.deepcopy(data)
                if corrupt == 'extra':
                    changed['profiles']['exit-confirm']['unknown'] = 'bad'
                elif corrupt == 'foreign':
                    changed['profiles']['perp'] = changed['profiles']['exit-confirm']
                elif corrupt == 'nonfinite':
                    changed['profiles']['exit-confirm']['scale'] = 'NaN'
                elif corrupt == 'cutoff':
                    changed['profiles']['exit-confirm']['calibration_end_ms'] += 1
                elif corrupt == 'kind':
                    changed['project_kind'] = 'perp'
                elif corrupt == 'hash':
                    changed['profiles']['exit-confirm']['base_bundle_sha256'] = 'x' * 64
                elif corrupt == 'name':
                    changed['profiles']['exit-confirm']['candidate'] = 'stop-budget'
                elif corrupt == 'baseline':
                    changed['profiles']['atr-stop']['scale'] = '.9'
                else:
                    del changed['profiles']['stop-budget']
                path.write_text(json.dumps(changed))
                with self.subTest(corrupt=corrupt), self.assertRaises(ValueError):
                    edge.calibration(path, 'stop-budget')
            path.write_text(json.dumps(data).replace('"format": 1', '"format": 1, "format": 1'))
            with self.assertRaises(ValueError):
                edge.calibration(path, 'stop-budget')

    def test_risk_cutoff_and_held_protections_untouched(self):
        risk = dict(scale='0', sha256='a' * 64)
        for now, expected in [(edge.CUTOFF - 1, True), (edge.CUTOFF, False)]:
            with policy('exit-confirm', risk=risk, now=now) as p:
                self.assertEqual(bool(decide(p)['orders']), expected)
                views = views_at()
                views[30].note_entry(100, 100)
                result = decide(p, views, {30: D(1), 40: D(0), 50: D(0)}, snap(btc='1'), {30: position()})
                self.assertEqual(result['sleeves']['30']['action'], 'hold')
                self.assertEqual(result['protections'][0]['quantity'], '1.00000')
                self.assertEqual(D(result['protections'][0]['stopPrice']), D(90))
        with policy('exit-confirm') as p:
            views = views_at()
            views[30]._owned_dust = True
            result = decide(p, views, {30: D('.00001'), 40: D(0), 50: D(0)}, snap(btc='.00001'))
            self.assertFalse(any(30 in o['sleeves'] for o in result['orders'] if o['side'] == 'BUY'))

    def test_state_rule_checkpoint_profile_reject_before_recover_spy(self):
        for corruption in ('unbound', 'rule', 'profile', 'checkpoint', 'source'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                venue = P4Venue([(ORIGIN, D(100), D(100), D(100))])
                cfg = Config('1', directory, 2, 1, 'demo', '1000')
                p = edge.Policy('exit-confirm', venue)
                with edge.configured(p), State(directory, cfg.scope) as state:
                    views = views_at()
                    # Convert genuine canonical model checkpoints into valid bound wrappers.
                    wrapped = {}
                    for w, view in views.items():
                        body = dict(model=view.checkpoint(), identity=p.identity)
                        wrapped[str(w)] = dict(body, sha256=edge.digest(json.dumps(body, sort_keys=True).encode()))
                    identity = copy.deepcopy(p.identity)
                    if corruption == 'profile':
                        identity['risk_calibration'] = dict(scale='.5', sha256=None)
                    if corruption == 'source':
                        identity['execution_source_sha256'] = 'f' * 64
                    if corruption == 'checkpoint':
                        wrapped['30']['identity'] = dict(p.identity, candidate='stop-budget')
                        body = {k: wrapped['30'][k] for k in ('model', 'identity')}
                        wrapped['30']['sha256'] = edge.digest(json.dumps(body, sort_keys=True).encode())
                    state.set_many(dict(models=wrapped, rule='old' if corruption == 'rule' else session.RULE,
                                        positions={str(w): None for w in SLEEVES}, follows={str(w): None for w in SLEEVES},
                                        entries_after=views[30].last, **({} if corruption == 'unbound' else dict(edge_identity=identity))))
                    before = list(state.db.iterdump())
                    with patch.object(execution.Lifecycle, 'recover') as recover:
                        with self.assertRaises(Blocked):
                            session.cycle(venue, state, cfg, execute=True)
                        recover.assert_not_called()
                    self.assertEqual(list(state.db.iterdump()), before)
                    self.assertEqual(venue.sent, [])

    def test_active_substep_partial_fill_does_not_authorize_a_topup(self):
        views = views_at()
        owned = {30: D('.000001'), 40: D(0), 50: D(0)}
        for candidate in ('exit-confirm', 'protected-participation-25'):
            with self.subTest(candidate=candidate), policy(candidate) as p:
                for flag in (None, False):
                    active = position('.000001')
                    if flag is not None:
                        active['dust'] = flag
                    result = decide(p, views, owned, snap(btc='.000001'), {30: active})
                    self.assertFalse(any(30 in o['sleeves'] for o in result['orders'] if o['side'] == 'BUY'))
                    self.assertEqual(p.journal[-1]['diagnostics'][-1]['blocked_reason'], 'held_sleeve_no_topup')
                # Flag alone has no applied-close proof and cannot authorize a new campaign.
                flagged = dict(position('.000001'), dust=True)
                views[30]._owned_dust = True
                result = decide(p, views, owned, snap(btc='.000001'), {30: flagged})
                self.assertFalse(any(o['side'] == 'BUY' for o in result['orders']))
                del views[30]._owned_dust

    def test_hooks_restored_on_decision_and_measurement_failure(self):
        original = (session.portfolio, session.State, session.Model, session.RULE, session._guard_state,
                    model.Model, follow.Model, execution.decision_view, preview.decision_view, preview._position_decision,
                    complete_spot.Policy, complete_spot.configured)
        with self.assertRaises(RuntimeError):
            with policy('protected-participation-25') as p, patch.object(preview, 'decision', side_effect=RuntimeError('decision failure')):
                decide(p)
        self.assertEqual(original, (session.portfolio, session.State, session.Model, session.RULE, session._guard_state,
                                    model.Model, follow.Model, execution.decision_view, preview.decision_view, preview._position_decision,
                                    complete_spot.Policy, complete_spot.configured))
        with patch.object(complete_spot, 'measure', side_effect=RuntimeError('meter failure')), self.assertRaises(RuntimeError):
            edge.measure('exit-confirm', 'base', [], [], lambda t: D(7), limit=1)
        self.assertIs(complete_spot.Policy, original[-2])
        self.assertIs(complete_spot.configured, original[-1])


if __name__ == '__main__':
    unittest.main()
