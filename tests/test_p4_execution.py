import tempfile
from decimal import Decimal as D
from unittest import TestCase

from spotquant.config import Config
from spotquant.model import DAY, ORIGIN
from spotquant.offline import P4Venue
from spotquant.session import run
from spotquant.state import State
from spotquant.types import Unknown


def run_day(config, venue):
    return run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)


def venue_before_entry():
    prices = [D(100)] * 400 + [D(98)]
    from crowding_fixtures import KnownFeatures
    venue = P4Venue([(ORIGIN + i * DAY, p, p, p) for i, p in enumerate(prices)])
    venue.now_ms += 60000
    venue.crowding_features = KnownFeatures
    return venue


def add_day(venue, close, high=None):
    close = D(close)
    venue.bars.append((venue.bars[-1][0] + DAY, D(high or close), close, close))
    venue.now_ms = venue.bars[-1][0] + DAY + 60000
    venue.price = close


class P4ExecutionTests(TestCase):
    def test_floored_exit_retains_fractional_ownership_and_reentry_resets_peak(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            venue.trigger('70')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            with State(directory, config.scope) as state:
                dust = state.get('positions')
                quantity = sum((D(p['qty']) for p in dust.values() if p), D(0))
                self.assertEqual(quantity, venue.btc)
                self.assertGreater(quantity, 0)
                self.assertTrue(all(p['dust'] for p in dust.values() if p))
                first_times = {w: p['first_ms'] for w, p in dust.items() if p}
            # The persisted dust remains owned after another session; a deposit
            # cannot be folded into it to bypass the complete fill reconciliation.
            self.assertEqual(run_day(config, venue)['errors'], [])
            for close in ('98', '105', '106'):
                add_day(venue, close)
                report = run_day(config, venue)
                self.assertEqual(report['errors'], [])
            with State(directory, config.scope) as state:
                positions = state.get('positions')
                self.assertEqual(sum((D(p['qty']) for p in positions.values() if p), D(0)), venue.btc)
                for window, p in positions.items():
                    self.assertFalse(p.get('dust'))
                    self.assertGreater(p['first_ms'], first_times[window])
                    self.assertEqual(D(p['peak']), D(106))
            submitted = list(venue.sent)
            venue.btc += D('.1')
            rejected = run_day(config, venue)
            self.assertEqual(rejected['status'], 'unknown')
            self.assertEqual(venue.sent, submitted)

    def test_stop_fill_between_decision_and_write_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            snapshot = venue.snapshot
            reads = 0
            def changed(uid):
                nonlocal reads
                reads += 1
                if reads == 2:
                    venue.now_ms += 1
                    venue.trigger('70')
                return snapshot(uid)
            venue.snapshot = changed
            sent = len(venue.sent)
            report = run_day(config, venue)
            self.assertEqual(report['status'], 'unknown')
            self.assertIn('account changed after decision', report['errors'][0]['reason'])
            self.assertEqual(len(venue.sent), sent)
            venue.snapshot = snapshot
            self.assertEqual(run_day(config, venue)['errors'], [])

    def test_prepared_sale_recovers_after_a_new_daily_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            add_day(venue, '97')
            cancel = venue.cancel
            def crash(identity):
                cancel(identity)
                raise KeyboardInterrupt
            venue.cancel = crash
            run_day(config, venue)
            with State(directory, config.scope) as state:
                prepared = state.db.execute("SELECT id FROM intents WHERE status='prepared' AND payload LIKE '%MARKET%'").fetchone()[0]
            add_day(venue, '96')
            venue.cancel = cancel
            result = run_day(config, venue)
            self.assertEqual(result['errors'], [])
            self.assertIn(prepared, venue.orders)
            with State(directory, config.scope) as state:
                self.assertFalse(state.pending())

    def test_prepared_stop_replacement_recovers_after_a_new_daily_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            add_day(venue, '103', '110')
            cancel = venue.cancel
            def crash(identity):
                cancel(identity)
                raise KeyboardInterrupt
            venue.cancel = crash
            run_day(config, venue)
            add_day(venue, '104', '112')
            venue.cancel = cancel
            result = run_day(config, venue)
            self.assertEqual(result['errors'], [])
            self.assertTrue(any(row['status'] == 'NEW' for row in venue.orders.values()))
            with State(directory, config.scope) as state:
                self.assertFalse(state.pending())

    def test_unconfirmed_protection_stays_unknown_on_subsequent_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            add_day(venue, '101')
            run_day(config, venue)
            add_day(venue, '102')
            venue.reject_stop = True
            for _ in range(2):
                self.assertEqual(run_day(config, venue)['status'], 'unknown')
            self.assertEqual(len([row for row in venue.orders.values() if row['type'] == 'MARKET']), 1)

    def entered(self, directory):
        config = Config('1', directory, 1, 1, 'demo', '1000')
        venue = venue_before_entry()
        run_day(config, venue)
        add_day(venue, '101')
        run_day(config, venue)
        add_day(venue, '102')
        run_day(config, venue)
        return config, venue

    def test_crash_after_cancel_resumes_sale_and_protects_partial_remainder(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            add_day(venue, '97')
            venue.fraction = D('.5')
            cancel = venue.cancel
            def crash(identity):
                cancel(identity)
                raise KeyboardInterrupt
            venue.cancel = crash
            report = run_day(config, venue)
            self.assertEqual(report['stop_reason'], 'interrupted')
            venue.cancel = cancel
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            stops = [row for row in venue.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(stops), 3)
            # Remainder uses the canonical 10% decision distance and native floor.
            self.assertTrue(all(D(row['stopPrice']) == D('91.80') for row in stops))
            self.assertLess(abs(sum(D(row['quantity']) for row in stops) - venue.btc), D('.00004'))
            before = len(venue.sent)
            run_day(config, venue)
            self.assertEqual(len(venue.sent), before)

    def test_equal_size_groups_have_distinct_order_identity_and_sell_attribution(self):
        from spotquant.execution import Lifecycle
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                for identity, _, status, _ in lifecycle.rows():
                    if status == 'resting':
                        lifecycle.cancel(identity)
                positions, follows = state.get('positions'), state.get('follows')
                ids = []
                for window, price in ((30, '70'), (40, '72'), (50, '74')):
                    order = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                             'quantity': str(D(positions[str(window)]['qty']).quantize(D('.00001'))),
                             'stopPrice': price, 'sleeves': [window]}
                    identity = lifecycle.prepare(order, venue.bars[-1][0], positions, follows)
                    lifecycle.send(identity)
                    ids.append(identity)
                self.assertEqual(len(set(ids)), 3)
                venue.now_ms += 2000
                venue.trigger('73')  # Only the sleeve-50 stop triggers.
                lifecycle.recover()
                from spotquant.follow import apply_day, day_open
                from spotquant.model import Model, SLEEVES
                models = {w: Model.restore(state.get('models')[str(w)]) for w in SLEEVES}
                old_ids = {trade['id'] for trade in venue.fills if trade['buyer']}
                result, _, _, _ = apply_day(models, {w: positions[str(w)] for w in SLEEVES},
                    {w: None for w in SLEEVES}, old_ids, day_open(venue.now_ms), venue.fills,
                    lambda: venue.completed_daily(None), owners=lifecycle.owners())
                self.assertEqual(result[30]['qty'], positions['30']['qty'])
                self.assertEqual(result[40]['qty'], positions['40']['qty'])
                self.assertLess(D(result[50]['qty']) if result[50] else D(0), D('.00001'))

    def test_real_session_partial_entry_restart_stop_amend_and_offline_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            self.assertEqual(run_day(config, venue)['errors'], [])
            add_day(venue, '101')
            run_day(config, venue)
            add_day(venue, '102')
            venue.fraction, venue.lose_ack = D('.5'), True
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertGreater(venue.btc, 0)
            sent = len(venue.sent)
            run_day(config, venue)
            self.assertEqual(len(venue.sent), sent)
            add_day(venue, '103', '110')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            active = [row for row in venue.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            self.assertEqual(D(active[0]['stopPrice']), D('99.00'))
            venue.now_ms += 2000
            venue.trigger('70')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertLess(venue.btc * venue.price, 5)
            with State(directory, config.scope) as state:
                self.assertFalse(state.pending())

    def test_query_outage_after_send_recovers_with_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            add_day(venue, '101')
            run_day(config, venue)
            add_day(venue, '102')
            original = venue.submit
            def submit(identity, payload):
                row = original(identity, payload)
                venue.unknown_query = True
                return row
            venue.submit = submit
            report = run_day(config, venue)
            self.assertEqual(report['status'], 'unknown')
            self.assertEqual(len(venue.sent), 1)
            venue.submit, venue.unknown_query = original, False
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertEqual(len([row for row in venue.orders.values() if row['type'] == 'MARKET']), 1)

    def test_external_cash_is_unknown_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            venue.cash += 1
            self.assertEqual(run_day(config, venue)['status'], 'unknown')
            self.assertEqual(venue.sent, [])
