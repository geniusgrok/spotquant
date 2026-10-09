"""Absence confirmation and rate-limit closeout. Venues are in-memory."""
import json
import tempfile
from decimal import Decimal as D

from spotquant.config import Config
from spotquant.execution import Lifecycle
from spotquant.session import PREVIOUS_RULE, cycle, run
from spotquant.state import State
from spotquant.types import Blocked, NotFound, Unknown
from test_execution import add_day, run_day, venue_before_entry
import test_execution as execution_fixture
from unittest import TestCase


def _new_stops(venue):
    return [row for row in venue.orders.values() if row.get('type') == 'STOP_LOSS' and row.get('status') == 'NEW']


class AbsenceTests(TestCase):
    def test_missing_stop_is_replaced_only_after_a_later_confirming_read(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution_fixture.ExecutionTests().entered(directory)
            original = next(row for row in _new_stops(venue))
            original_id, order_id = original['clientOrderId'], original['orderId']
            before_btc, before_sent = venue.btc, list(venue.sent)
            original['status'] = 'EXPIRED'
            query = venue.query

            def missing(identity):
                if identity in (original_id, order_id):
                    raise NotFound('HTTP 400 code -2013')
                return query(identity)

            venue.query = missing
            with State(directory, config.scope) as state:
                with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                    cycle(venue, state, config, execute=True)
                status, result = state.db.execute(
                    'SELECT status,result FROM intents WHERE id=?', (original_id,)).fetchone()
                self.assertEqual(status, 'unknown')
                self.assertEqual(json.loads(result).get('absent'), None)
                self.assertEqual(_new_stops(venue), [])
                venue.wait(1)
                report = cycle(venue, state, config, execute=True)
            successor = _new_stops(venue)
            self.assertEqual(len(successor), 1)
            self.assertNotEqual(successor[0]['clientOrderId'], original_id)
            self.assertNotIn(original_id, venue.sent[len(before_sent):])
            self.assertEqual(venue.btc, before_btc)
            self.assertEqual(report['status'], 'demo_execution')
            self.assertFalse(report['risk_state']['manual_takeover'])
            with State(directory, config.scope) as state:
                parent = json.loads(state.db.execute(
                    'SELECT result FROM intents WHERE id=?', (original_id,)).fetchone()[0])
                payload = json.loads(state.db.execute(
                    'SELECT payload FROM intents WHERE id=?', (successor[0]['clientOrderId'],)).fetchone()[0])
                self.assertTrue(parent['absent'])
                self.assertEqual(payload['replaced_stop'], original_id)
                self.assertEqual(payload['absence_generation'], 1)

    def test_same_millisecond_reread_does_not_arm_a_successor(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution_fixture.ExecutionTests().entered(directory)
            original = next(row for row in _new_stops(venue))
            venue.orders.pop(original['clientOrderId'])
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                before_sent = len(venue.sent)
                for _ in range(2):
                    with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                        lifecycle.recover()
                status = state.db.execute(
                    'SELECT status FROM intents WHERE id=?', (original['clientOrderId'],)).fetchone()[0]
                self.assertEqual(status, 'unknown')
                self.assertEqual(len(venue.sent), before_sent)

    def test_visible_order_and_durable_fill_are_not_treated_as_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution_fixture.ExecutionTests().entered(directory)
            original = next(row for row in _new_stops(venue))
            query = venue.query

            def missing(_identity):
                raise NotFound('HTTP 400 code -2013')

            venue.query = missing
            with State(directory, config.scope) as state:
                with self.assertRaisesRegex(Unknown, 'visible'):
                    cycle(venue, state, config, execute=True)
                result = json.loads(state.db.execute(
                    'SELECT result FROM intents WHERE id=?', (original['clientOrderId'],)).fetchone()[0])
                self.assertNotIn('absence_observed_ms', result)
            venue.query = query
            original['status'] = 'EXPIRED'
            venue.query = missing
            with State(directory, config.scope) as state:
                state.db.execute('INSERT INTO fills VALUES (?,?,?,?)',
                                 (99, venue.now_ms, original['orderId'], '{}'))
                state.db.commit()
                venue.wait(2)
                with self.assertRaisesRegex(Unknown, 'durable fill'):
                    cycle(venue, state, config, execute=True)
                self.assertEqual(_new_stops(venue), [])

    def test_unknown_buy_is_never_closed_as_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution_fixture.ExecutionTests().entered(directory)
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                identity, payload, _, _ = next(row for row in lifecycle.rows()
                                               if row[1]['order']['side'] == 'BUY')
                lifecycle.save(identity, payload, 'unknown', {})
                original_query = venue.query

                def missing_buy(order):
                    if order == identity:
                        return None
                    return original_query(order)

                venue.query = missing_buy
                venue.wait(5)
                with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                    lifecycle.recover()
                status, result = state.db.execute(
                    'SELECT status,result FROM intents WHERE id=?', (identity,)).fetchone()
                self.assertEqual(status, 'unknown')
                self.assertNotIn('absence_observed_ms', json.loads(result))

    def test_reappeared_stop_is_reclaimed_and_its_successor_is_canceled(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution_fixture.ExecutionTests().entered(directory)
            live = next(row for row in _new_stops(venue))
            original = dict(live)
            live['status'] = 'EXPIRED'
            query = venue.query

            def missing(identity):
                if identity in (original['clientOrderId'], original['orderId']):
                    raise NotFound('HTTP 400 code -2013')
                return query(identity)

            venue.query = missing
            with State(directory, config.scope) as state:
                with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                    cycle(venue, state, config, execute=True)
                venue.wait(1)
                cycle(venue, state, config, execute=True)
            successor = next(row for row in _new_stops(venue))
            self.assertNotEqual(successor['clientOrderId'], original['clientOrderId'])
            venue.orders[original['clientOrderId']] = dict(original, status='NEW', executedQty='0')
            venue.query = query
            report = run_day(config, venue)
            active = _new_stops(venue)
            self.assertEqual([row['clientOrderId'] for row in active], [original['clientOrderId']])
            self.assertEqual(successor['status'], 'CANCELED')
            self.assertFalse(report['manual_takeover'])
            self.assertEqual(report['errors'], [])

    def test_previous_safety_rule_keeps_its_peak_when_upgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = execution_fixture.ExecutionTests().entered(directory)
            with State(directory, config.scope) as state:
                positions = state.get('positions')
                positions['40']['peak'] = '110'
                state.set_many({'rule': PREVIOUS_RULE, 'positions': positions})
                with self.assertRaisesRegex(Blocked, 'owned account reconciliation'):
                    cycle(venue, state, config)
                self.assertEqual(state.get('rule'), PREVIOUS_RULE)
                cycle(venue, state, config, execute=True)
                self.assertEqual(state.get('rule'), '2026-10-09-absent-order-closeout-v3')
                self.assertEqual(state.get('positions')['40']['peak'], '110')


class RateLimitCloseoutTests(TestCase):
    def _arm(self, directory, session, poll):
        config = Config('1', directory, session, poll, 'demo', '1000')
        venue = venue_before_entry()
        run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
        add_day(venue, '101')
        return config, venue

    def test_backoff_inside_the_session_continues_and_places_the_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._arm(directory, 30, 1)
            original, query, failed = venue.submit, venue.query, []

            def limited(identity, payload, **kwargs):
                if payload.get('type') == 'STOP_LOSS' and not failed:
                    failed.append(identity)
                    venue._retry_after_at = venue.monotonic() + 2
                    raise Unknown('Binance rate limit HTTP 429; retry after 2 seconds')
                return original(identity, payload, **kwargs)

            def backoff(identity):
                if venue.monotonic() < getattr(venue, '_retry_after_at', 0):
                    raise Unknown('Binance rate limit backoff is still active; no request sent')
                return query(identity)

            venue.submit, venue.query = limited, backoff
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            self.assertEqual(len(_new_stops(venue)), 1)
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
            self.assertFalse(report.get('closeout_attempted', False))
            self.assertEqual(report['stop_reason'], 'deadline')
            self.assertFalse(report['manual_takeover'])
            self.assertEqual(len(failed), 1)
            self.assertNotEqual(_new_stops(venue)[0]['clientOrderId'], failed[0])

    def test_backoff_past_the_deadline_uses_closeout_without_another_buy(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._arm(directory, 1, 1)
            original, query = venue.submit, venue.query

            def arm_backoff(identity, payload, **kwargs):
                if payload.get('type') == 'STOP_LOSS' and not getattr(venue, '_held', False):
                    venue._held = True
                    venue._retry_after_at = venue.monotonic() + 5
                    venue._hold = venue._retry_after_at
                    raise Unknown('Binance rate limit HTTP 429; retry after 5 seconds')
                return original(identity, payload, **kwargs)

            def backoff(identity):
                if venue.monotonic() < getattr(venue, '_retry_after_at', 0):
                    raise Unknown('Binance rate limit backoff is still active; no request sent')
                return query(identity)

            venue.submit, venue.query = arm_backoff, backoff
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            self.assertTrue(report['closeout_attempted'])
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
            self.assertEqual(len(_new_stops(venue)), 1)
            self.assertFalse(report['manual_takeover'])

    def test_backoff_longer_than_closeout_does_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._arm(directory, 1, 1)
            original = venue.submit
            sent = []

            def limited(identity, payload, **kwargs):
                if payload.get('type') == 'STOP_LOSS':
                    sent.append(identity)
                    venue._retry_after_at = venue.monotonic() + 120
                    raise Unknown('Binance rate limit HTTP 429; retry after 120 seconds')
                return original(identity, payload, **kwargs)

            query = venue.query

            def backoff(identity):
                if venue.monotonic() < getattr(venue, '_retry_after_at', 0):
                    raise Unknown('Binance rate limit backoff is still active; no request sent')
                return query(identity)

            venue.submit = limited
            venue.query = backoff
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            self.assertTrue(report['manual_takeover'])
            self.assertEqual(_new_stops(venue), [])
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
            self.assertEqual(len(sent), 1)
            self.assertIn('exceeds the protective closeout', report['reason'])
