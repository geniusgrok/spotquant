from contextlib import contextmanager
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research import alpha_spot as alpha, complete_spot
from research.session_account import HistoricalVenue, audit
from spotquant import execution, follow, model, session
from spotquant.config import Config
from spotquant.model import DAY, ORIGIN, Model
from spotquant.state import State
from spotquant.types import Blocked, Unknown


def bars(prices=None):
    prices = prices or ([D(100)] * 400 + [D(98), D(101), D(102), D(103), D(104), D(105)])
    return [(ORIGIN + i * DAY, p, p, p, p, D(100000)) for i, p in enumerate(prices)]


def snapshot(cash='400', btc='6', price='100'):
    return dict(btc=btc, usdt_free=cash, usdt_locked='0', avg_price=price, open_orders=0, orders=[])


def views_at(close=D(100)):
    views = {}
    for w in alpha.TACTICAL:
        m = Model(w)
        for i, p in enumerate([D(90)] * 400 + [close, close]):
            m.update(ORIGIN + i * DAY, p, p, p)
        views[w] = m
    return views


@contextmanager
def policy_state(candidate, views, owned, snap, *, risk=None):
    venue = HistoricalVenue(bars(), ORIGIN + 402 * DAY, D(1000), lambda t: D(7))
    p = alpha.Policy(candidate, venue, risk=risk)
    with tempfile.TemporaryDirectory() as directory, State(directory, 'test') as state:
        p.state = state
        positions = {str(w): None if owned.get(w, 0) == 0 else dict(
            qty=str(owned[w]), first_ms=ORIGIN + 400 * DAY, entry_fill='100', peak='100',
            repair=False, repair_peak=None, adverse=False, through=views[w].last,
            protection='resting', entry_open_ms=ORIGIN + 400 * DAY) for w in views}
        state.set('positions', positions)
        state._execution_owners = {}
        yield p, state


class AlphaSpotTests(unittest.TestCase):
    def test_calibration_rejects_unbound_future_and_nonfinite_profiles(self):
        profile = dict(scale='.4', effective_from_ms=alpha.CUTOFF, calibration_end_ms=alpha.CUTOFF,
                       training_end_day_exclusive='2022-01-01', base_bundle_sha256='a' * 64,
                       baseline_candidate='consensus')
        data = dict(format=1, cutoff_ms=alpha.CUTOFF, spec_sha256=alpha.digest(alpha.SPEC.read_bytes()),
                    profiles={'atr-close': profile})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'risk.json'
            path.write_text(json.dumps(data))
            self.assertEqual(alpha.calibration(path, 'atr-close')['scale'], '.4')
            for key, bad in [('scale', 'NaN'), ('scale', '-.1'), ('scale', '1.1'),
                             ('calibration_end_ms', alpha.CUTOFF + DAY),
                             ('base_bundle_sha256', 'missing'), ('baseline_candidate', 'default')]:
                changed = json.loads(json.dumps(data))
                changed['profiles']['atr-close'][key] = bad
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    alpha.calibration(path, 'atr-close')
            with self.assertRaises(ValueError):
                alpha.calibration(path, 'core-slow')

    def test_components_are_finite_and_core_modes_exclusive(self):
        self.assertEqual(alpha.components_for('combo', ('atr-stop', 'trend-reentry')), ('trend-reentry', 'atr-stop'))
        for parts in [('core-permanent', 'core-slow'), ('atr-stop', 'atr-stop'), ('unknown',), ()]:
            with self.assertRaises(ValueError):
                alpha.components_for('combo', parts)

    def test_subpools_conserve_btc_usdt_fees_and_dust_without_transfers(self):
        owners = {'1': {'sleeves': [200]}, '2': {'sleeves': [30, 40, 50]}, '3': {'sleeves': [200]}}
        fills = [dict(order_id=1, buyer=True, qty=D(2), quote=D(200), commission=D('.002'), commission_asset='BTC'),
                 dict(order_id=2, buyer=True, qty=D(3), quote=D(300), commission=D('.3'), commission_asset='USDT'),
                 dict(order_id=3, buyer=False, qty=D('1.99'), quote=D('298.5'), commission=D('.2985'), commission_asset='USDT')]
        pools = alpha.subpools({'cash': '1000', 'btc': '0'}, fills, owners, D('.2'), snapshot('797.9015', '3.008'))
        self.assertEqual(pools['core'], {'cash': D('298.2015'), 'btc': D('.008')})
        self.assertEqual(pools['tactical']['cash'], D('499.7'))
        for bad_owners, bad_fills, bad_snapshot in [
            ({**owners, '1': {'sleeves': [30, 200]}}, fills, snapshot('797.9015', '3.008')),
            (owners, [dict(fills[0], quote=D(201))], snapshot('799', '1.998')),
            (owners, fills, snapshot('797.91', '3.008'))]:
            with self.assertRaises(Unknown):
                alpha.subpools({'cash': '1000', 'btc': '0'}, bad_fills, bad_owners, D('.2'), bad_snapshot)

    def test_reentry_actual_exit_reclaim_two_closes_allowance_and_reset(self):
        p = alpha.Policy('trend-reentry', HistoricalVenue(bars(), ORIGIN + 402 * DAY, D(1000), lambda t: D(7)))
        exit_time = ORIGIN + 400 * DAY + 3600000
        view = SimpleNamespace(bull=True, streak=20, crash_ok=True, extended=False,
                               last=ORIGIN + 400 * DAY, close=D(101))
        stop = {'owner': {'sleeves': [30], 'order': {'side': 'SELL', 'type': 'STOP_LOSS'}},
                'qty': D(2), 'quote': D(200), 'last_ms': exit_time, 'meta': {}}
        self.assertFalse(p.recovery(view, 30, [stop], 2, {}))
        view.last += DAY  # Two completed close timestamps after the actual intraday fill.
        self.assertTrue(p.recovery(view, 30, [stop], 2, {}))
        self.assertFalse(p.recovery(view, 30, [stop], 1, {}))
        self.assertFalse(p.recovery(view, 30, [stop], 2, {'30': view.last}))
        view.close = D(99)
        self.assertFalse(p.recovery(view, 30, [stop], 2, {}))
        view.close = D(101)
        for reason, expected in [('extended', True), ('adverse', False), ('sma', False)]:
            sale = dict(stop, owner={'sleeves': [30], 'order': {'side': 'SELL', 'type': 'MARKET'}},
                        meta={'exit_types': {'30': reason}})
            self.assertEqual(p.recovery(view, 30, [sale], 2, {}), expected)
        bought = dict(stop, owner={'sleeves': [30], 'order': {'side': 'BUY', 'type': 'MARKET'}},
                      qty=D('.01'), meta={'mechanism': 'trend-reentry'}, last_ms=view.last + DAY)
        self.assertFalse(p.recovery(view, 30, [stop, bought], 2, {}))
        view.last += 2 * DAY
        view.streak = 2  # A reset means the old exit does not authorize the new episode.
        self.assertFalse(p.recovery(view, 30, [stop], 2, {}))

    def test_participation_adds_to_held_bulls_only_and_confirms_once(self):
        views, owned = views_at(), {30: D(3), 40: D(3), 50: D(0)}
        for w in (30, 40):
            views[w].note_entry(100, 100)
        views[50].enter = False
        snap = snapshot()
        with policy_state('target-participation', views, owned, snap) as (p, state):
            decision = p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))
            buy = decision['orders'][0]
            self.assertEqual(buy['sleeves'], [30, 40])
            self.assertEqual(D(buy['quoteOrderQty']), D(300))
            record = {'owner': {'signal_ms': views[30].last}, 'meta': {'mechanism': 'target-participation'}}
            with patch.object(p, 'records', return_value=[record]):
                self.assertEqual(p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))['orders'], [])
            views[40].adverse = True
            decision = p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))
            self.assertTrue(any(o['side'] == 'SELL' for o in decision['orders']))
            self.assertFalse(any(o['side'] == 'BUY' for o in decision['orders']))

    def test_participation_never_arms_empty_second_bull(self):
        views, owned, snap = views_at(), {30: D(3), 40: D(0), 50: D(0)}, snapshot('700', '3')
        views[30].note_entry(100, 100)
        views[40].enter = views[50].enter = False
        views[50].bull = False
        with policy_state('target-participation', views, owned, snap) as (p, _):
            decision = p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))
            self.assertEqual(decision['orders'][0]['sleeves'], [30])
            self.assertEqual(D(decision['orders'][0]['quoteOrderQty']), D(600))

    def test_atr_uses_only_completed_ranges_and_close_variant_preserves_repair(self):
        rows = bars()
        venue = HistoricalVenue(rows, ORIGIN + 402 * DAY, D(1000), lambda t: D(7))
        p = alpha.Policy('atr-close', venue)
        last = ORIGIN + 400 * DAY
        expected = p.atr(last)
        rows[401] = (rows[401][0], D(1), D(1000000), D(1), D(200), D(100))
        p.atr_cache.clear()
        self.assertEqual(p.atr(last), expected)
        views, owned, snap = views_at(D(95)), {30: D(3), 40: D(3), 50: D(0)}, snapshot(price='95')
        views[50].enter = False
        for w in (30, 40):
            views[w].note_entry(100, 100)
            views[w].adverse = True
        with policy_state('atr-close', views, owned, snap) as (p, _), patch.object(p, 'atr', return_value=D(4)):
            decision = p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))
            self.assertFalse(any(o['side'] == 'SELL' for o in decision['orders']))
            views[30].repair = True
            views[30].bull = False
            with patch.object(p, 'atr', return_value=D('.1')):
                decision = p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))
            self.assertEqual(decision['sleeves']['30']['action'], 'hold')
        self.assertTrue(views[40].adverse)  # Policy overrides never poison model checkpoints.

    def test_atr_stop_never_loosens_and_crossed_mark_uses_real_sell(self):
        views, owned, snap = views_at(), {30: D(3), 40: D(3), 50: D(0)}, snapshot()
        views[50].enter = False
        for w in (30, 40):
            views[w].note_entry(100, 100)
        with policy_state('atr-stop', views, owned, snap) as (p, state), patch.object(p, 'atr', return_value=D(7)):
            state._execution_owners = {'1': {'sleeves': [30, 40], 'signal_ms': ORIGIN + 400 * DAY,
                'order': {'side': 'SELL', 'type': 'STOP_LOSS', 'stopPrice': '85'}, 'native_status': 'NEW'}}
            decision = p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))
            self.assertTrue(all(D(o['stopPrice']) == 85 for o in decision['protections']))
            decision = p(views, owned, snapshot(price='80'), entries_enabled=True, capital_limit=D(10000))
            self.assertTrue(all(o['side'] == 'SELL' and o['type'] == 'MARKET' for o in decision['orders']))
            self.assertEqual(decision['protections'], [])

    def test_risk_scale_changes_only_new_buy_after_cutoff(self):
        views, owned, snap = views_at(), {30: D(0), 40: D(0), 50: D(0)}, snapshot('1000', '0')
        with policy_state('consensus', views, owned, snap, risk={'scale': '.5', 'sha256': 'x'}) as (p, _):
            pre = p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))
            p.venue.now_ms = alpha.CUTOFF
            post = p(views, owned, snap, entries_enabled=True, capital_limit=D(10000))
            self.assertEqual(D(post['orders'][0]['quoteOrderQty']), alpha.floor_step(D(pre['orders'][0]['quoteOrderQty']) / 2, alpha.QUOTE_STEP))

    def test_scoped_hooks_restore_after_exception_and_checkpoint_mode_mismatch(self):
        refs = (model.SLEEVES, session.SLEEVES, execution.SLEEVES, model.Model, session.Model,
                follow.Model, session.RULE, session.RECORDED_LIMITS, session.State, session.portfolio)
        venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        p = alpha.Policy('core-permanent', venue)
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            with alpha.configured(p):
                m = model.Model(200)
                for row in bars()[:401]:
                    m.update(row[0], row[2], row[3], row[4])
                saved = m.checkpoint()
                restored = model.Model.restore(saved)
                self.assertEqual(restored.bull, restored.close > restored.sma)
                raise RuntimeError('injected')
        self.assertEqual(refs, (model.SLEEVES, session.SLEEVES, execution.SLEEVES, model.Model,
                               session.Model, follow.Model, session.RULE, session.RECORDED_LIMITS,
                               session.State, session.portfolio))
        with alpha.configured(alpha.Policy('core-slow', venue)):
            with self.assertRaises(Blocked):
                model.Model.restore(saved)

    def test_real_sessions_core_separate_fills_protection_and_cash(self):
        for candidate in ('core-permanent', 'core-slow'):
            venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
            p = alpha.Policy(candidate, venue)
            with tempfile.TemporaryDirectory() as directory, alpha.configured(p):
                config = Config('1', directory, 30, 5, 'demo', '5000000')
                for index, day in enumerate((401, 402, 403)):
                    venue.advance(ORIGIN + day * DAY)
                    result = session.run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
                    self.assertTrue(all('session deadline' in e['reason'] for e in result['errors']), result['errors'])
                    if index == 0:
                        self.assertEqual(venue.fills, [])
                self.assertTrue(audit(venue)['passed'])
                with State(directory, config.scope) as state:
                    owners = execution.Lifecycle(state, venue, config).owners()
                    pools = alpha.subpools(state.get('execution_anchor'), alpha.cached_fills(state), owners, D('.2'), snapshot(str(venue.cash), str(venue.btc)))
                    self.assertGreater(pools['core']['btc'], 0)
                    self.assertGreater(pools['tactical']['btc'], 0)
                    self.assertLess(pools['core']['cash'], 1)
                    self.assertTrue(all(o['sleeves'] == [200] or 200 not in o['sleeves'] for o in owners.values()))
                    self.assertEqual(set(state.get('positions')), {'30', '40', '50', '200'})

    def test_real_core_stop_does_not_reenter_until_new_completed_day(self):
        rows = bars([D(100)] * 400 + [D(101)] * 8)
        venue = HistoricalVenue(rows, ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        p = alpha.Policy('core-permanent', venue)
        with tempfile.TemporaryDirectory() as directory, alpha.configured(p):
            config = Config('1', directory, 30, 5, 'demo', '5000000')
            def run():
                result = session.run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
                self.assertTrue(all('session deadline' in e['reason'] for e in result['errors']), result['errors'])
            run()
            venue.advance(ORIGIN + 402 * DAY)
            run()
            venue.trigger('70')
            fills = len(venue.fills)
            run()
            self.assertEqual(len(venue.fills), fills)
            venue.advance(ORIGIN + 403 * DAY)
            run()
            self.assertGreater(len(venue.fills), fills)

    def test_core_checkpoint_identity_rejects_even_flat_account_mismatch(self):
        venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 10, 5, 'demo', '5000000')
            with alpha.configured(alpha.Policy('core-permanent', venue)):
                session.run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            with alpha.configured(alpha.Policy('core-slow', venue)):
                with self.assertRaises(Blocked):
                    session.run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)

    def test_partial_fills_move_only_actual_money_and_unknown_intents_do_not_consume(self):
        owner = {'sleeves': [200], 'order': {'side': 'BUY', 'type': 'MARKET'}, 'signal_ms': ORIGIN}
        fills = [dict(order_id=1, buyer=True, qty=D('.5'), quote=D(50), commission=D('.0005'),
                      commission_asset='BTC', time=ORIGIN + DAY, id=1, price=D(100))]
        pools = alpha.subpools({'cash': '1000', 'btc': '0'}, fills, {'1': owner}, D('.2'), snapshot('950', '.4995'))
        self.assertEqual(pools['core']['cash'], 150)
        p = alpha.Policy('trend-reentry', HistoricalVenue(bars(), ORIGIN + 402 * DAY, D(1000), lambda t: D(7)))
        signals = {alpha.signal_key('BUY', ORIGIN, [200]): {'mechanism': 'trend-reentry'}}
        self.assertEqual(p.records([], {'1': owner}, signals), [])
        self.assertEqual(p.records(fills, {'1': owner}, signals)[0]['qty'], D('.5'))

    def test_failed_follow_keeps_research_and_model_commit_atomic(self):
        venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        with tempfile.TemporaryDirectory() as directory, alpha.configured(alpha.Policy('core-permanent', venue)):
            config = Config('1', directory, 10, 5, 'demo', '5000000')
            with session.State(directory, config.scope) as state, patch.object(session, '_follow_after', side_effect=Unknown('injected after policy')):
                with self.assertRaises(Unknown):
                    session.cycle(venue, state, config, execute=True)
                self.assertIsNone(state.get('models'))
                self.assertIsNone(state.get('alpha_identity'))
                self.assertIsNone(state.get('alpha_signals'))

    def test_atr_partial_exit_remainder_retains_tightened_stop(self):
        views, owned, snap = views_at(), {30: D(3), 40: D(0), 50: D(0)}, snapshot('700', '3')
        views[30].note_entry(100, 100)
        with policy_state('atr-stop', views, owned, snap) as (p, state), patch.object(p, 'atr', return_value=D(7)):
            state._execution_owners = {'1': {'sleeves': [30], 'signal_ms': ORIGIN + 400 * DAY,
                'order': {'side': 'SELL', 'type': 'STOP_LOSS', 'stopPrice': '85'}, 'native_status': 'CANCELED'}}
            with alpha.configured(p):
                protection = execution._protection(views[30], D(1), snap)
                self.assertEqual(D(protection['stopPrice']), D(85))

    def test_protection_peak_after_real_entry_excludes_prefill_high(self):
        rows = bars()
        t, o, h, low, c, q = rows[402]
        rows[402] = (t, o, D(110), low, c, q)
        start = t + DAY // 2
        venue = HistoricalVenue(rows, ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        with tempfile.TemporaryDirectory() as directory, alpha.configured(alpha.Policy('core-permanent', venue)):
            config = Config('1', directory, 30, 5, 'demo', '5000000')
            session.run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            venue.advance(start)
            session.run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            venue.advance(t + DAY)
            session.run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            with State(directory, config.scope) as state:
                core = state.get('positions')['200']
                self.assertLess(D(core['peak']), D(110))
                self.assertEqual(D(core['peak']), D(core['entry_fill']))

    def test_three_real_session_noop_equal_to_current_consensus(self):
        starts = [ORIGIN + day * DAY for day in (401, 402, 403)]
        baseline = complete_spot.measure('consensus', 'base', bars(), starts, lambda t: D(7), {}, limit=3)
        result = alpha.measure('core0', 'base', bars(), starts, lambda t: D(7), limit=3)
        for key in ('final_cny', 'final_usdt', 'mdd', 'audit', 'cash_usdt', 'btc', 'fills', 'daily',
                    'positions', 'allocations', 'client_events', 'pending_intents'):
            self.assertEqual(baseline[key], result[key], key)
        for a, b in zip(baseline['sessions'], result['sessions']):
            self.assertEqual({k: v for k, v in a.items() if k != 'archive_backup_sha256'},
                             {k: v for k, v in b.items() if k != 'archive_backup_sha256'})


if __name__ == '__main__':
    unittest.main()
