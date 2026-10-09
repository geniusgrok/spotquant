"""Real lifecycle recovery against an in-memory account; no exchange transport."""
import json
import tempfile
from decimal import Decimal as D
from unittest import TestCase

from spotquant.execution import Lifecycle
from spotquant.session import cycle
from spotquant.state import State
from spotquant.types import Blocked, Unknown, NotSent
import test_execution as fixture


def active_stops(venue):
    return [row for row in venue.orders.values()
            if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW']


def hide_stop(venue):
    original = active_stops(venue)[0]
    original['status'] = 'EXPIRED'
    hidden = {original['clientOrderId'], original['orderId']}
    query = venue.query
    venue.query = lambda identity: None if identity in hidden else query(identity)
    return original, query, hidden


def saved(state, identity):
    payload, status, result = state.db.execute(
        'SELECT payload,status,result FROM intents WHERE id=?', (identity,)).fetchone()
    return json.loads(payload), status, json.loads(result)


class OrderRecoveryTests(TestCase):
    def close_absence(self, config, venue, state):
        with self.assertRaisesRegex(Unknown, 'never resubmitted'):
            cycle(venue, state, config, execute=True)
        venue.wait(1)
        return cycle(venue, state, config, execute=True)

    def test_slow_snapshot_cannot_count_as_the_gap_between_missing_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            original, _, _ = hide_stop(venue)
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                    lifecycle.recover()
                venue.wait(.1)
                snapshot = venue.snapshot

                def slow(uid):
                    venue.wait(2)
                    return snapshot(uid)

                venue.snapshot = slow
                with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                    lifecycle.recover()
                self.assertNotIn('absent', saved(state, original['clientOrderId'])[2])
                self.assertEqual(active_stops(venue), [])

    def test_visible_native_id_outage_and_balance_disagreement_reset_first_observation(self):
        for fault in ('native_id', 'client_id', 'query_outage', 'balance'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                config, venue = fixture.ExecutionTests().entered(directory)
                original, query, hidden = hide_stop(venue)
                identity = original['clientOrderId']
                with State(directory, config.scope) as state:
                    lifecycle = Lifecycle(state, venue, config)
                    with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                        lifecycle.recover()
                    venue.wait(2)
                    missing = venue.query
                    if fault in ('native_id', 'client_id'):
                        original['status'] = 'NEW'
                        if fault == 'native_id':
                            original['clientOrderId'] = 'unrecognized-alias'
                    elif fault == 'query_outage':
                        venue.query = lambda _identity: (_ for _ in ()).throw(Unknown('query timeout'))
                    else:
                        venue.cash += 1
                    with self.assertRaises(Unknown):
                        lifecycle.recover()
                    self.assertNotIn('absence_observed_ms', saved(state, identity)[2])
                    venue.query = missing
                    original.update(status='EXPIRED', clientOrderId=identity)
                    if fault == 'balance':
                        venue.cash -= 1
                    with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                        lifecycle.recover()
                    result = saved(state, identity)[2]
                    self.assertEqual(result['absence_observed_ms'], venue.now_ms)
                    self.assertNotIn('absent', result)
                    self.assertEqual(active_stops(venue), [])

    def test_changed_price_and_new_day_cannot_reset_the_successor_limit(self):
        for change in ('price', 'day'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                config, venue = fixture.ExecutionTests().entered(directory)
                original, query, hidden = hide_stop(venue)
                with State(directory, config.scope) as state:
                    with self.assertRaises(Unknown):
                        cycle(venue, state, config, execute=True)
                    if change == 'price':
                        venue.price = D(150)
                        venue.wait(1)
                    else:
                        fixture.add_day(venue, '103')
                    cycle(venue, state, config, execute=True)
                    successor = active_stops(venue)[0]
                    payload, _, _ = saved(state, successor['clientOrderId'])
                    self.assertEqual(payload['absence_parent'], original['clientOrderId'])
                    self.assertEqual(payload['absence_generation'], 1)
                    self.assertNotEqual(successor['stopPrice'], original['stopPrice'])
                    successor['status'] = 'EXPIRED'
                    hidden.update((successor['clientOrderId'], successor['orderId']))
                    before = list(venue.sent)
                    with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                        cycle(venue, state, config, execute=True)
                    venue.wait(1)
                    with self.assertRaisesRegex(Unknown, 'owner review'):
                        cycle(venue, state, config, execute=True)
                fixture.add_day(venue, '104')
                with State(directory, config.scope) as state:
                    with self.assertRaisesRegex(Unknown, 'owner review'):
                        cycle(venue, state, config, execute=True)
                    self.assertEqual(saved(state, successor['clientOrderId'])[1], 'unknown')
                self.assertEqual(venue.sent, before)

    def test_prepared_target_inherits_absence_allowance_at_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            original = active_stops(venue)[0]
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                _, payload, _, _ = next(row for row in lifecycle.rows() if row[0] == original['clientOrderId'])
                target = lifecycle.prepare(dict(payload['order'], stopPrice='90.00', sleeves=[40]),
                                           payload['signal_ms'], state.get('positions'), state.get('follows'))
                original, _, _ = hide_stop(venue)
                self.close_absence(config, venue, state)
                self.assertEqual(active_stops(venue)[0]['clientOrderId'], target)
                prepared, status, _ = saved(state, target)
                self.assertEqual(status, 'resting')
                self.assertEqual(prepared['absence_parent'], original['clientOrderId'])
                self.assertEqual(prepared['absence_generation'], 1)

    def test_absence_keeps_a_previously_confirmed_stop_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                identity, payload, _, _ = next(row for row in lifecycle.rows() if row[2] == 'resting')
                lifecycle.cancel(identity)
                higher = lifecycle.prepare(dict(payload['order'], stopPrice='90.00', sleeves=[40]),
                                           payload['signal_ms'], state.get('positions'), state.get('follows'))
                lifecycle.send(higher)
                original, _, _ = hide_stop(venue)
                self.close_absence(config, venue, state)
                self.assertEqual(original['clientOrderId'], higher)
                self.assertGreaterEqual(D(active_stops(venue)[0]['stopPrice']), D('90'))

    def test_reappearance_cannot_change_native_order_id_or_terms(self):
        for changes in ({'orderId': 999}, {'side': 'BUY'}, {'quantity': '0.01'}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                config, venue = fixture.ExecutionTests().entered(directory)
                original, query, _ = hide_stop(venue)
                original_id, native_id = original['clientOrderId'], original['orderId']
                with State(directory, config.scope) as state:
                    self.close_absence(config, venue, state)
                    successor = active_stops(venue)[0]
                    original.update(changes, status='NEW')
                    venue.query = query
                    with self.assertRaises(Unknown):
                        cycle(venue, state, config, execute=True)
                    self.assertEqual(saved(state, original_id)[2]['orderId'], native_id)
                    self.assertTrue(saved(state, original_id)[2]['absent'])
                    self.assertEqual(successor['status'], 'NEW')

    def test_late_partial_original_fill_reprotects_only_the_confirmed_remainder(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            original, query, _ = hide_stop(venue)
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                identity, payload, _, _ = next(row for row in lifecycle.rows()
                                               if row[0] == original['clientOrderId'])
                lifecycle.save(identity, payload, 'unknown', {})  # The original ACK was lost.
                self.close_absence(config, venue, state)
            successor = active_stops(venue)[0]
            quantity = D(original['quantity']) / 2
            quote = quantity * venue.price
            original.update(status='EXPIRED', executedQty=str(quantity), quote=str(quote))
            venue.btc -= quantity
            venue.cash += quote * (1 - venue.fee)
            venue._fill(original, quantity, quote)
            venue.query = query
            with State(directory, config.scope) as state:
                report = cycle(venue, state, config, execute=True)
                result = saved(state, original['clientOrderId'])[2]
                self.assertEqual(result['orderId'], original['orderId'])
                self.assertEqual(D(result['executedQty']), quantity)
                self.assertNotIn('absent', result)
            self.assertEqual(successor['status'], 'CANCELED')
            self.assertEqual(len(active_stops(venue)), 1)
            self.assertLessEqual(D(active_stops(venue)[0]['quantity']), venue.btc)
            self.assertLess(venue.btc - D(active_stops(venue)[0]['quantity']), D('0.00001'))
            self.assertFalse(report['risk_state']['manual_takeover'])

    def test_late_filled_original_is_attributed_and_cancels_oversized_successor(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            original, query, _ = hide_stop(venue)
            with State(directory, config.scope) as state:
                self.close_absence(config, venue, state)
            successor = active_stops(venue)[0]
            venue.wait(1)
            quantity = D(original['quantity'])
            quote = quantity * venue.price
            original.update(status='FILLED', executedQty=str(quantity), quote=str(quote))
            venue.btc -= quantity
            venue.cash += quote * (1 - venue.fee)
            venue._fill(original, quantity, quote)
            venue.query = query
            with State(directory, config.scope) as state:
                report = cycle(venue, state, config, execute=True)
                _, status, result = saved(state, original['clientOrderId'])
                self.assertEqual((status, result['status']), ('settled', 'FILLED'))
                self.assertNotIn('absent', result)
                self.assertNotIn('recall_pending', result)
                self.assertTrue(state.get('positions')['40']['dust'])
            self.assertEqual(successor['status'], 'CANCELED')
            self.assertEqual(active_stops(venue), [])
            self.assertFalse(report['risk_state']['manual_takeover'])

    def test_late_zero_fill_terminal_keeps_the_confirmed_successor(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            original, query, _ = hide_stop(venue)
            with State(directory, config.scope) as state:
                self.close_absence(config, venue, state)
            successor = active_stops(venue)[0]
            before = list(venue.sent)
            venue.query = query
            with State(directory, config.scope) as state:
                report = cycle(venue, state, config, execute=True)
                result = saved(state, original['clientOrderId'])[2]
                self.assertEqual(result['status'], 'EXPIRED')
                self.assertNotIn('absent', result)
                self.assertIn('absence_confirmed_ms', result)
            self.assertEqual(active_stops(venue), [successor])
            self.assertEqual(venue.sent, before)
            self.assertTrue(report['risk_state']['order_confirmation_complete'])

    def test_reappeared_original_recall_survives_crash_before_or_after_cancel(self):
        for when in ('before', 'after'):
            with self.subTest(when=when), tempfile.TemporaryDirectory() as directory:
                config, venue = fixture.ExecutionTests().entered(directory)
                original, query, _ = hide_stop(venue)
                with State(directory, config.scope) as state:
                    self.close_absence(config, venue, state)
                successor = active_stops(venue)[0]
                original['status'] = 'NEW'
                venue.query = query
                cancel, attempts = venue.cancel, []

                def crash(identity, **kwargs):
                    attempts.append(identity)
                    if when == 'after':
                        cancel(identity, **kwargs)
                    raise fixture.SimulatedCrash

                venue.cancel = crash
                with State(directory, config.scope) as state:
                    with self.assertRaises(fixture.SimulatedCrash):
                        cycle(venue, state, config, execute=True)
                    self.assertTrue(saved(state, original['clientOrderId'])[2]['recall_pending'])
                venue.cancel = cancel
                with State(directory, config.scope) as state:
                    report = cycle(venue, state, config, execute=True)
                    result = saved(state, original['clientOrderId'])[2]
                    self.assertNotIn('recall_pending', result)
                    self.assertNotIn('absent', result)
                    self.assertIn('absence_confirmed_ms', result)
                self.assertEqual(active_stops(venue), [original])
                self.assertEqual(successor['status'], 'CANCELED')
                self.assertFalse(report['risk_state']['manual_takeover'])

    def test_successor_cancel_readback_must_keep_its_native_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            original, query, _ = hide_stop(venue)
            with State(directory, config.scope) as state:
                self.close_absence(config, venue, state)
            successor = active_stops(venue)[0]
            successor_id = successor['clientOrderId']
            original['status'] = 'NEW'

            def wrong_terminal(identity):
                row = query(identity)
                if row and row['orderId'] == successor['orderId'] and row['status'] == 'CANCELED':
                    return dict(row, clientOrderId='some-other-order')
                return row

            venue.query = wrong_terminal
            with State(directory, config.scope) as state:
                with self.assertRaisesRegex(Unknown, 'native order identity differs'):
                    cycle(venue, state, config, execute=True)
                self.assertEqual(saved(state, successor_id)[1], 'canceling')
                self.assertTrue(saved(state, original['clientOrderId'])[2]['recall_pending'])
            venue.query = query
            with State(directory, config.scope) as state:
                cycle(venue, state, config, execute=True)
            self.assertEqual(active_stops(venue), [original])

    def test_absent_market_sell_protects_without_becoming_a_native_rejection_retry(self):
        for initial in ('unknown', 'resting'):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as directory:
                config, venue = fixture.ExecutionTests().entered(directory)
                submit, attempts = venue.submit, []

                def pending_market(identity, payload, **kwargs):
                    if payload['type'] == 'MARKET' and payload['side'] == 'SELL':
                        kwargs['preflight'](venue.snapshot(venue.uid))
                        attempts.append(identity)
                        if initial == 'unknown':
                            raise Unknown('market request outcome unknown')
                        row = dict(payload, orderId=len(venue.orders) + 1, clientOrderId=identity,
                                   status='NEW', executedQty='0', cummulativeQuoteQty='0')
                        venue.orders[identity] = row
                        return row
                    return submit(identity, payload, **kwargs)

                venue.submit, venue.price = pending_market, D('100.4')
                with State(directory, config.scope) as state:
                    lifecycle = Lifecycle(state, venue, config)
                    stop_id, stop, _, _ = next(row for row in lifecycle.rows() if row[2] == 'resting')
                    lifecycle.cancel(stop_id)
                    market = lifecycle.prepare(dict(symbol='BTCUSDT', side='SELL', type='MARKET',
                                                    quantity=stop['order']['quantity'], sleeves=[40]),
                                               stop['signal_ms'], state.get('positions'), state.get('follows'))
                    if initial == 'unknown':
                        with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                            lifecycle.send(market)
                    else:
                        lifecycle.send(market)
                        self.assertEqual(saved(state, market)[2]['status'], 'NEW')
                        native = venue.orders[market]
                        native['status'] = 'EXPIRED'
                        query = venue.query
                        venue.query = lambda identity: None if identity in (market, native['orderId']) else query(identity)
                    self.assertEqual(saved(state, market)[1], initial)
                    self.close_absence(config, venue, state)
                    successor = active_stops(venue)[0]
                    payload = saved(state, successor['clientOrderId'])[0]
                    self.assertEqual(payload['absence_parent'], market)
                    self.assertEqual(payload['absence_generation'], 1)
                    self.assertEqual(saved(state, market)[2]['status'], 'ABSENT')
                    venue.price = D('70')
                    cycle(venue, state, config, execute=True)
                    self.assertEqual(attempts, [market])

    def test_unresolved_original_blocks_a_new_buy_at_final_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            original, _, _ = hide_stop(venue)
            with State(directory, config.scope) as state:
                self.close_absence(config, venue, state)
                lifecycle = Lifecycle(state, venue, config)
                payload = next(row[1] for row in lifecycle.rows() if row[1]['order']['side'] == 'BUY')
                before = list(venue.sent)
                with self.assertRaisesRegex(NotSent, 'native terminal confirmation'):
                    lifecycle._preflight(payload, venue.snapshot(config.account_uid))
                self.assertTrue(saved(state, original['clientOrderId'])[2]['absent'])
                self.assertEqual(venue.sent, before)

    def test_same_original_cannot_receive_another_absence_allowance_after_reappearance(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = fixture.ExecutionTests().entered(directory)
            original, query, hidden = hide_stop(venue)
            with State(directory, config.scope) as state:
                self.close_absence(config, venue, state)
                original['status'] = 'NEW'
                venue.query = query
                cycle(venue, state, config, execute=True)
                self.assertNotIn('absent', saved(state, original['clientOrderId'])[2])
                original['status'] = 'EXPIRED'
                venue.query = lambda identity: None if identity in hidden else query(identity)
                before = list(venue.sent)
                with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                    cycle(venue, state, config, execute=True)
                venue.wait(1)
                with self.assertRaisesRegex(Unknown, 'owner review'):
                    cycle(venue, state, config, execute=True)
                self.assertEqual(venue.sent, before)
