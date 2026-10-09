"""Bounded session recovery with an in-memory account; no exchange access."""
from dataclasses import replace
from decimal import Decimal as D
import json
import tempfile
from unittest import TestCase
from unittest.mock import patch

from spotquant.binance import Binance
from spotquant.config import Config
from spotquant.session import _guard_state, _recovery_risk_state, _risk_state, cycle, run
from spotquant.state import State
from spotquant.types import Blocked, NotSent, Unknown
import test_execution as execution


def active_stops(venue):
    return [row for row in venue.orders.values()
            if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW']


class SessionRecoveryTests(TestCase):
    def arm(self, directory, seconds=600, poll=60):
        config = Config('1', directory, 1, 1, 'demo', '1000')
        venue = execution.venue_before_entry()
        execution.run_day(config, venue)
        execution.add_day(venue, '101')
        return replace(config, session_seconds=seconds, poll_seconds=poll), venue

    def limit_first_stop(self, venue, retry_at, *, elapsed_before_reply=0, sent=False):
        submit, query, snapshot = venue.submit, venue.query, venue.snapshot
        failures, requests = [], []

        def limited(identity, payload, **kwargs):
            if payload['type'] == 'STOP_LOSS' and not failures:
                failures.append(identity)
                venue.wait(elapsed_before_reply)
                venue._retry_after_at = retry_at
                error = Unknown if sent else NotSent
                raise error('Binance rate limit HTTP 429; retry after the active backoff')
            requests.append(venue.monotonic())
            return submit(identity, payload, **kwargs)

        def guarded(method, *args):
            if venue.monotonic() < getattr(venue, '_retry_after_at', 0):
                raise Unknown('Binance rate limit backoff is still active; no request sent')
            requests.append(venue.monotonic())
            return method(*args)

        venue.submit = limited
        venue.query = lambda identity: guarded(query, identity)
        venue.snapshot = lambda uid: guarded(snapshot, uid)
        return failures, requests

    def test_stop_during_long_backoff_latches_before_the_sleep_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.arm(directory)
            start = venue.monotonic()
            failures, requests = self.limit_first_stop(venue, start + 120)
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait,
                         stopping=lambda: venue.monotonic() >= start + 1)
            self.assertEqual(report['stop_reason'], 'requested')
            self.assertLessEqual(report['elapsed_seconds'], 2)
            self.assertEqual(venue._deadline_at, start + 31)
            self.assertTrue(report['manual_takeover'])
            self.assertIn('exceeds the protective closeout', report['reason'])
            self.assertEqual(active_stops(venue), [])
            self.assertEqual(len(failures), 1)
            self.assertTrue(all(stamp == start for stamp in requests))

    def test_short_backoff_after_stop_still_protects_without_another_buy(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.arm(directory)
            start = venue.monotonic()
            failures, requests = self.limit_first_stop(venue, start + 20)
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait,
                         stopping=lambda: venue.monotonic() >= start + 1)
            self.assertEqual(report['stop_reason'], 'requested')
            self.assertEqual(venue._deadline_at, start + 31)
            self.assertLessEqual(report['elapsed_seconds'], 31)
            self.assertFalse(report['manual_takeover'])
            self.assertEqual([row['clientOrderId'] for row in active_stops(venue)], failures)
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
            self.assertTrue(all(stamp == start or stamp >= start + 20 for stamp in requests))

    def test_stop_during_normal_poll_is_observed_before_the_full_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution.ExecutionTests().entered(directory)
            config = replace(config, session_seconds=600, poll_seconds=60)
            start = venue.monotonic()
            before = list(venue.sent)
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait,
                         stopping=lambda: venue.monotonic() >= start + 2)
            self.assertEqual(report['stop_reason'], 'requested')
            self.assertLessEqual(report['elapsed_seconds'], 3)
            self.assertEqual(venue._deadline_at, start + 32)
            self.assertFalse(report['manual_takeover'])
            self.assertEqual(venue.sent, before)

    def test_stop_can_shorten_a_closeout_started_early_for_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.arm(directory, seconds=60, poll=5)
            start = venue.monotonic()
            self.limit_first_stop(venue, start + 65)
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait,
                         stopping=lambda: venue.monotonic() >= start + 2)
            self.assertEqual(report['stop_reason'], 'requested')
            self.assertEqual(venue._deadline_at, start + 32)
            self.assertLessEqual(report['elapsed_seconds'], 3)
            self.assertTrue(report['manual_takeover'])
            self.assertEqual(active_stops(venue), [])

    def test_early_closeout_uses_original_trading_deadline_plus_thirty(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.arm(directory, seconds=10, poll=1)
            start = venue.monotonic()
            self.limit_first_stop(venue, start + 34, elapsed_before_reply=1, sent=True)
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            self.assertEqual(venue._deadline_at, start + 40)
            self.assertTrue(report['closeout_attempted'])
            self.assertFalse(report['manual_takeover'])
            self.assertLess(report['elapsed_seconds'], 40)
            self.assertEqual(len(active_stops(venue)), 1)
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)

    def test_late_closeout_cannot_extend_the_original_protection_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.arm(directory, seconds=10, poll=1)
            start = venue.monotonic()
            self.limit_first_stop(venue, start + 45, elapsed_before_reply=38, sent=True)
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            self.assertEqual(venue._deadline_at, start + 40)
            self.assertEqual(report['elapsed_seconds'], 38)
            self.assertTrue(report['manual_takeover'])
            self.assertIn('exceeds the protective closeout', report['reason'])
            self.assertEqual(active_stops(venue), [])

    def test_transport_reads_latch_a_stop_before_any_buy_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.arm(directory)
            start = venue.monotonic()
            completed = venue.completed_daily
            deadlines = []
            venue._stop = None

            def slow_observation(after):
                venue.wait(2)
                venue._stop()
                deadlines.append(venue._deadline_at)
                return completed(after)

            venue.completed_daily = slow_observation
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait,
                         stopping=lambda: venue.monotonic() >= start + 2)
            self.assertEqual(report['stop_reason'], 'requested')
            self.assertTrue(deadlines)
            self.assertTrue(all(deadline == start + 32 for deadline in deadlines))
            self.assertEqual(venue.sent, [])

    def test_restart_binds_backoff_before_initial_exchange_clock_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, 1, 1, 'demo', '100')
            now, requests = 1700000000000, []
            with State(directory, config.scope) as state:
                state.set('rate_limits', {'demo-api.binance.com': now + 120000})

            def transport(*args):
                requests.append(args)
                raise AssertionError('active persisted backoff must prohibit every request')

            venue = Binance(key='test-key', secret='test-secret', environment='demo',
                            capital_limit=config.capital_limit, demo_execution_uid=config.account_uid,
                            clock=lambda: now / 1000, opener=transport)
            report = run(config, venue, execute=True, monotonic=lambda: 0, wait=lambda _seconds: None)
            self.assertIsNone(venue._offset_ms)
            self.assertEqual(requests, [])
            self.assertTrue(report['manual_takeover'])
            self.assertFalse(report['write_attempted'])
            self.assertEqual(report['session_started_at_ms'], now)
            self.assertEqual(report['session_started_clock'], 'local_utc')

    def test_absence_recovery_report_keeps_original_confirmation_separate_from_coverage(self):
        from test_review_state import risk_snapshot, stop
        with tempfile.TemporaryDirectory() as directory, State(directory, 'demo') as state:
            order = stop(state)
            payload = json.dumps({'order': {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS'}})
            result = {'absent': True, 'status': 'ABSENT'}
            state.db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?)',
                             ('sq-original', 'p4', payload, 'settled', json.dumps(result), 0))
            state.db.commit()
            snapshot = risk_snapshot(orders=[order])
            venue = execution.venue_before_entry()
            risk = _risk_state(state, snapshot, venue)
            self.assertFalse(risk['manual_takeover'])
            self.assertFalse(risk['order_confirmation_complete'])
            self.assertEqual(risk['awaiting_original_client_ids'], ['sq-original'])
            self.assertEqual(risk['recall_pending_client_ids'], [])
            state.db.execute('UPDATE intents SET result=? WHERE id=?',
                             (json.dumps(dict(result, recall_pending=True)), 'sq-original'))
            state.db.commit()
            risk = _risk_state(state, snapshot, venue)
            self.assertTrue(risk['manual_takeover'])
            self.assertEqual(risk['direction'], 'unknown')
            self.assertIsNone(risk['unprotected_btc'])
            self.assertEqual(risk['recall_pending_client_ids'], ['sq-original'])

    def test_absence_successor_cannot_change_its_generation_or_parent_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution.ExecutionTests().entered(directory)
            original = active_stops(venue)[0]
            original['status'] = 'EXPIRED'
            query = venue.query
            venue.query = lambda identity: (None if identity in (
                original['clientOrderId'], original['orderId']) else query(identity))
            with State(directory, config.scope) as state:
                with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                    cycle(venue, state, config, execute=True)
                venue.wait(1)
                cycle(venue, state, config, execute=True)
                successor = active_stops(venue)[0]['clientOrderId']
                encoded = state.db.execute('SELECT payload FROM intents WHERE id=?', (successor,)).fetchone()[0]
                payload = json.loads(encoded)
                buy_id = next(row['clientOrderId'] for row in venue.orders.values() if row['side'] == 'BUY')
                _guard_state(state)
                for changes in ({'absence_generation': True}, {'absence_generation': 2},
                                {'absence_parent': successor}, {'absence_parent': buy_id}):
                    with self.subTest(changes=changes):
                        state.db.execute('UPDATE intents SET payload=? WHERE id=?',
                                         (json.dumps(dict(payload, **changes)), successor))
                        state.db.commit()
                        with self.assertRaisesRegex(Blocked, 'incompatible durable pending allocation'):
                            _guard_state(state)
                state.db.execute('UPDATE intents SET payload=? WHERE id=?', (encoded, successor))
                state.db.commit()

    def test_visible_protection_cannot_hide_an_unreconciled_cash_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution.ExecutionTests().entered(directory)
            before = list(venue.sent)
            venue.cash += D(1)
            report = execution.run_day(config, venue)
            self.assertEqual(report['status'], 'unknown')
            self.assertTrue(report['manual_takeover'])
            self.assertFalse(report['risk_state']['account_reconciled'])
            self.assertEqual(report['risk_state']['direction'], 'unknown')
            self.assertFalse(report['risk_state']['observation_current'])
            self.assertGreater(report['risk_state']['covered_btc'], 0)
            self.assertEqual(venue.sent, before)
            self.assertEqual(len(active_stops(venue)), 1)

    def test_stop_from_another_position_is_not_reported_as_owned_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution.ExecutionTests().entered(directory)
            with State(directory, config.scope) as state:
                identity = active_stops(venue)[0]['clientOrderId']
                encoded = state.db.execute('SELECT payload FROM intents WHERE id=?', (identity,)).fetchone()[0]
                payload = json.loads(encoded)
                payload['position_first_ms']['40'] -= 86400000
                state.db.execute('UPDATE intents SET payload=? WHERE id=?', (json.dumps(payload), identity))
                state.db.commit()
                risk = _risk_state(state, venue.snapshot(config.account_uid), venue)
                self.assertEqual(risk['covered_btc'], 0)
                self.assertTrue(risk['manual_takeover'])
                self.assertEqual(risk['direction'], 'unknown')

    def test_protected_account_reconciliation_does_not_depend_on_public_entry_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution.ExecutionTests().entered(directory)
            venue.crowding_features = lambda: (_ for _ in ()).throw(Unknown('public entry inputs unavailable'))
            with State(directory, config.scope) as state:
                risk = _recovery_risk_state(state, venue, config)
                self.assertTrue(risk['account_reconciled'])
                self.assertFalse(risk['manual_takeover'])
                self.assertEqual(risk['direction'], 'long')

    def test_settled_absence_recovery_is_validated_before_any_native_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution.ExecutionTests().entered(directory)
            with State(directory, config.scope) as state:
                identity = active_stops(venue)[0]['clientOrderId']
                encoded, result = state.db.execute('SELECT payload,result FROM intents WHERE id=?',
                                                   (identity,)).fetchone()
                payload, native = json.loads(encoded), json.loads(result)
                native.update(absent=True, status='ABSENT', absence_observed_ms=venue.now_ms - 1000,
                              absence_confirmed_ms=venue.now_ms)
                native['executedQty'] = '0'
                calls = []

                def forbidden(*args, **kwargs):
                    calls.append((args, kwargs))
                    raise AssertionError('malformed recovery state must fail before native requests')

                venue.query = venue.cancel = venue.snapshot = forbidden
                for bad_payload, bad_result in (
                        (payload, dict(native, absent='true')),
                        (payload, dict(native, absence_confirmed_ms=True)),
                        (dict(payload, order=dict(payload['order'], side='BUY')), native)):
                    with self.subTest(payload=bad_payload, result=bad_result):
                        state.db.execute('UPDATE intents SET payload=?,status=?,result=? WHERE id=?',
                                         (json.dumps(bad_payload), 'settled', json.dumps(bad_result), identity))
                        state.db.commit()
                        with self.assertRaisesRegex(Blocked, 'incompatible durable pending allocation'):
                            cycle(venue, state, config, execute=True)
                        self.assertEqual(calls, [])
            report = execution.run_day(config, venue)
            self.assertTrue(report['manual_takeover'])
            self.assertEqual(calls, [])

    def test_report_file_failure_does_not_skip_protective_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.arm(directory, seconds=30, poll=1)
            submit, failed = venue.submit, []

            def interrupted_protection(identity, payload, **kwargs):
                if payload['type'] == 'STOP_LOSS' and not failed:
                    failed.append(identity)
                    raise NotSent('protection preflight observation unavailable')
                return submit(identity, payload, **kwargs)

            venue.submit = interrupted_protection
            with patch.object(State, 'report', side_effect=OSError('report file is not writable')) as reports:
                report = execution.run_day(config, venue)
            self.assertEqual(reports.call_count, 2)
            self.assertEqual(report['stop_reason'], 'report_failure')
            self.assertEqual(report['status'], 'unknown')
            self.assertTrue(report['report_persistence_failed'])
            self.assertIn('report persistence failed', report['reason'])
            self.assertFalse(report['manual_takeover'])
            self.assertEqual(report['pending_intents'], 0)
            self.assertLessEqual(report['elapsed_seconds'], 30)
            self.assertEqual([row['clientOrderId'] for row in active_stops(venue)], failed)
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
            with State(directory, config.scope) as state:
                self.assertEqual(state.pending(), [])
