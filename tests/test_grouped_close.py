"""A completed rounded group close retains real coins without fabricating a hold."""
import json
import tempfile
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from spotquant.config import Config
from spotquant.execution import Lifecycle
from spotquant.follow import apply_day
from spotquant.model import DAY, ORIGIN, Model, SLEEVES
from spotquant.preview import portfolio
from spotquant.session import RULE, _view, cycle
from spotquant.state import State
from spotquant.types import Unknown


class GroupedCloseTests(TestCase):
    def fixtures(self, status='FILLED', intended='0.00004', executed='0.00004'):
        models = {w: Model(w) for w in SLEEVES}
        for model in models.values():
            for i in range(60):
                model.update(ORIGIN + i * DAY, D(60000), D(60000), D(60000))
        quantities = {30: '0.000029', 40: '0.000019', 50: '0.000019'}
        positions = {w: dict(qty=q, entry_fill='60000', first_ms=ORIGIN + 55 * DAY,
                             entry_open_ms=ORIGIN + 55 * DAY, peak='100000', repair=False,
                             repair_peak=None, adverse=False, through=ORIGIN + 59 * DAY)
                     for w, q in quantities.items()}
        owner = dict(sleeves=list(SLEEVES), weights={str(w): q for w, q in quantities.items()},
                     order=dict(symbol='BTCUSDT', side='SELL', type='MARKET', quantity=intended),
                     signal_ms=ORIGIN + 59 * DAY, repair={str(w): False for w in SLEEVES}, native_status=status,
                     native_executed_qty=executed)
        return models, positions, {w: None for w in SLEEVES}, {'17': owner}

    def trade(self, identity, day, qty, buyer=False):
        qty = D(qty)
        return dict(id=identity, order_id=17 if not buyer else 18,
                    time=ORIGIN + day * DAY + 1000, qty=qty, quote=qty * D(60000),
                    price=D(60000), buyer=buyer, commission=D(0), commission_asset='USDT')

    def apply(self, models, positions, follows, owners, trade, accounted=None):
        return apply_day(models, positions, follows, accounted or set(),
                         trade['time'] // DAY * DAY, [trade], lambda: [], owners=owners)

    def test_future_final_readback_cannot_close_an_earlier_partial_fill(self):
        models, positions, follows, owners = self.fixtures()
        positions, follows, accounted, closed = self.apply(
            models, positions, follows, owners, self.trade(1, 60, '0.00002'))
        self.assertEqual(closed, [])
        self.assertTrue(all(not p.get('dust') for p in positions.values()))
        # A restart between fill days must retain the amount already applied.
        positions = {int(w): p for w, p in json.loads(json.dumps(positions)).items()}
        positions, follows, _, closed = self.apply(
            models, positions, follows, owners, self.trade(2, 61, '0.00002'), accounted)
        self.assertEqual(set(closed), set(SLEEVES))
        self.assertTrue(all(p['dust'] for p in positions.values()))
        self.assertGreaterEqual(D(positions[30]['qty']), D('.00001'))
        self.assertLess(abs(sum((D(p['qty']) for p in positions.values()), D(0)) - D('.000027')), D('1e-24'))

    def test_expired_partial_and_intentional_half_reduction_remain_positions(self):
        for status, intended, executed, fill in (
                ('EXPIRED', '.00004', '.000038', '.000038'),
                ('FILLED', '.00002', '.00002', '.00002')):
            with self.subTest(status=status, intended=intended):
                models, positions, follows, owners = self.fixtures(status, intended, executed)
                positions, _, _, _ = self.apply(models, positions, follows, owners, self.trade(1, 60, fill))
                self.assertFalse(positions[30].get('dust'))

    def test_missing_readback_or_incomplete_legacy_application_never_widens_dust(self):
        for legacy in (False, True):
            models, positions, follows, owners = self.fixtures()
            if legacy:
                # Original weights no longer equal the restored position, with no applied counter.
                for p in positions.values():
                    p['qty'] = str(D(p['qty']) - D('.000001'))
            else:
                owners['17'].pop('native_status')
                owners['17'].pop('native_executed_qty')
            if legacy:
                positions, _, _, _ = self.apply(models, positions, follows, owners, self.trade(1, 60, '.00004'))
                self.assertFalse(positions[30].get('dust'))
            else:
                with self.assertRaises(Unknown):
                    self.apply(models, positions, follows, owners, self.trade(1, 60, '.00004'))

    def test_late_terminal_readback_does_not_commit_a_permanently_unprotected_remainder(self):
        models, positions, follows, owners = self.fixtures('NEW', '.00004', '0')
        original = json.loads(json.dumps(positions))
        trade = self.trade(1, 60, '.00004')
        with self.assertRaisesRegex(Unknown, 'terminal native readback'):
            self.apply(models, positions, follows, owners, trade)
        self.assertEqual(json.loads(json.dumps(positions)), original)
        owners['17'].update(native_status='FILLED', native_executed_qty='.00004')
        positions, _, accounted, closed = self.apply(models, positions, follows, owners, trade)
        self.assertEqual(accounted, {1})
        self.assertEqual(set(closed), set(SLEEVES))
        self.assertTrue(all(p['dust'] for p in positions.values()))

    def test_full_close_is_strategy_flat_while_real_ownership_and_capital_are_kept(self):
        models, positions, follows, owners = self.fixtures()
        positions, _, _, _ = self.apply(models, positions, follows, owners, self.trade(1, 60, '.00004'))
        views, owned = {}, {}
        for w in SLEEVES:
            views[w], owned[w] = _view(models[w], positions[w])
        btc = sum(owned.values(), D(0))
        snapshot = dict(btc=btc, usdt_free=D(1000), usdt_locked=D(0), open_orders=0,
                        avg_price=D(60000))
        decision = portfolio(views, owned, snapshot, entries_enabled=False, capital_limit=D(1000))
        self.assertEqual(decision['action'], 'flat')
        self.assertEqual(decision['orders'], [])
        self.assertEqual(decision['protections'], [])
        self.assertGreater(owned[30], D('.00001'))
        with self.assertRaises(Unknown):
            portfolio(views, owned, dict(snapshot, btc=btc + D('.1')),
                      entries_enabled=True, capital_limit=D(1000))
        # If a future price makes the residual placeable, block instead of silently hiding it.
        views[30].close = D(600000)
        with self.assertRaises(Unknown):
            portfolio(views, owned, dict(snapshot, avg_price=D(600000)),
                      entries_enabled=True, capital_limit=D(1000))

    def test_real_reentry_merges_owned_residual_and_resets_peak(self):
        models, positions, follows, owners = self.fixtures()
        positions, follows, accounted, _ = self.apply(models, positions, follows, owners, self.trade(1, 60, '.00004'))
        owners['18'] = dict(sleeves=list(SLEEVES), weights={str(w): '1' for w in SLEEVES}, repair={})
        old_qty = sum((D(p['qty']) for p in positions.values()), D(0))
        positions, _, _, _ = self.apply(models, positions, follows, owners,
                                        self.trade(2, 61, '.003', buyer=True), accounted)
        self.assertTrue(all(not p.get('dust') for p in positions.values()))
        self.assertTrue(all(D(p['peak']) == D(60000) for p in positions.values()))
        self.assertTrue(all(p['first_ms'] == ORIGIN + 61 * DAY + 1000 for p in positions.values()))
        self.assertLess(abs(sum((D(p['qty']) for p in positions.values()), D(0)) - old_qty - D('.003')), D('1e-24'))

    def test_readonly_recovery_and_executor_use_the_same_durable_native_metadata(self):
        models, _, _, owners = self.fixtures()
        payload = dict(owners['17'])
        payload.pop('native_status')
        payload.pop('native_executed_qty')
        result = dict(orderId=17, status='FILLED', executedQty='.00004')
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 300, 5, 'demo', '1000')
            venue = SimpleNamespace(execution_authorized=True, sent=[])
            with State(directory, config.scope) as state:
                state.set_many({'rule': RULE, 'models': {str(w): m.checkpoint() for w, m in models.items()},
                                'positions': {str(w): None for w in SLEEVES},
                                'follows': {str(w): None for w in SLEEVES},
                                'entries_after': ORIGIN + 59 * DAY})
                lifecycle = Lifecycle(state, venue, config)
                lifecycle.save('owned-sale', payload, 'settled', result)
                expected = lifecycle.owners()
                self.assertEqual(expected['17']['native_status'], 'FILLED')
                self.assertEqual(D(expected['17']['native_executed_qty']), D('.00004'))
                def observed(adapter, observed_state, observed_config, **kwargs):
                    self.assertEqual(observed_state._execution_owners, expected)
                    return {}
                with patch('spotquant.session._cycle', observed):
                    self.assertEqual(cycle(venue, state, config)['status'], 'read_only')

    def test_readonly_cycle_rolls_back_earlier_sleeve_before_late_terminal_guard(self):
        models, positions, follows, owners = self.fixtures('NEW', '.00004', '0')
        # Make sleeve 30's remainder sub-step, so it is processed before sleeve
        # 40 needs the missing terminal readback. Its model is bullish and would
        # note_flat(), making model rollback part of this regression as well.
        positions[30]['qty'], positions[40]['qty'] = positions[40]['qty'], positions[30]['qty']
        owners['17']['weights'] = {str(w): p['qty'] for w, p in positions.items()}
        for model in models.values():
            model.update(ORIGIN + 60 * DAY, D(61000), D(61000), D(61000))
        trade = self.trade(1, 61, '.00004')
        snapshot = dict(account_uid='1', environment='demo', btc=D('.000027'),
                        usdt_free=D(1000), usdt_locked=D(0), open_orders=0,
                        orders=[], avg_price=D(60000))
        venue = SimpleNamespace(environment='demo', capital_limit=D(1000),
                                execution_authorized=True, sent=[],
                                clock=lambda: (trade['time'] + 1000) / 1000,
                                completed_daily=lambda after: [], snapshot=lambda uid: snapshot,
                                trades=lambda since: [trade])
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 300, 5, 'demo', '1000')
            with State(directory, config.scope) as state:
                state.set_many({'rule': RULE,
                                'models': {str(w): m.checkpoint() for w, m in models.items()},
                                'positions': {str(w): p for w, p in positions.items()},
                                'follows': {str(w): f for w, f in follows.items()},
                                'entries_after': ORIGIN + 60 * DAY,
                                'accounted_ids': [], 'exit_through': {}})
                payload = dict(owners['17'])
                payload.pop('native_status')
                payload.pop('native_executed_qty')
                lifecycle = Lifecycle(state, venue, config)
                result = dict(orderId=17, status='NEW', executedQty='0')
                lifecycle.save('owned-sale', payload, 'resting', result)
                keys = ('positions', 'models', 'accounted_ids', 'trade_cursor_ms')
                before = {key: state.get(key) for key in keys}
                with self.assertRaisesRegex(Unknown, 'terminal native readback'):
                    cycle(venue, state, config, execute=False)
                self.assertEqual({key: state.get(key) for key in keys}, before)
                result.update(status='FILLED', executedQty='.00004')
                lifecycle.save('owned-sale', payload, 'settled', result)
                report = cycle(venue, state, config, execute=False)
                self.assertEqual(report['status'], 'read_only')
                self.assertTrue(all(p['dust'] for p in state.get('positions').values()))
                self.assertEqual(sum((D(p['qty']) for p in state.get('positions').values()), D(0)),
                                 snapshot['btc'])
                self.assertEqual(state.get('accounted_ids'), [1])
                self.assertEqual(state.get('trade_cursor_ms'), trade['time'])
                self.assertEqual(venue.sent, [])
