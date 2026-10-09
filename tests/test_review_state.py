"""Account and recovery regressions from the PR 26 review; all venues are local."""
from copy import deepcopy
from decimal import Decimal as D
import json
import tempfile
import unittest
from unittest.mock import patch

from spotquant.config import Config
from spotquant.model import DAY
from spotquant.session import _guard_state, _risk_state, cycle, run
from spotquant.state import State
from spotquant.types import Blocked, Unknown


def risk_snapshot(btc='0.01', orders=()):
    return dict(account_uid='1', environment='demo', btc=D(btc),
                usdt_free=D(0), usdt_locked=D(0), last_price=D(60000),
                avg_price=D(60000), min_notional=D(5), orders=list(orders))


def stop(state, quantity='0.01'):
    order = dict(symbol='BTCUSDT', side='SELL', type='STOP_LOSS',
                 quantity=quantity, stopPrice='50000')
    state.db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?)',
                     ('sq-owned', 'p4', json.dumps({'order': order}), 'resting',
                      json.dumps({'orderId': 1}), 0))
    state.db.commit()
    return dict(order_id=1, client_id='sq-owned', side='SELL', type='STOP_LOSS',
                orig_qty=quantity, executed_qty='0', stop_price='50000', status='NEW')


class RiskReportTests(unittest.TestCase):
    class Clock:
        def clock(self):
            return 1780000000

    def test_replaceable_gap_is_not_dust_just_because_its_notional_is_small(self):
        with tempfile.TemporaryDirectory() as directory, State(directory, 'demo') as state:
            order = stop(state, '0.00998')
            risk = _risk_state(state, risk_snapshot(orders=[order]), self.Clock())
            self.assertEqual(risk['unprotected_btc'], D('0.00002'))
            self.assertTrue(risk['manual_takeover'])
            self.assertEqual(risk['residual_btc'], 0)

    def test_fully_covered_lot_and_genuinely_untradeable_residuals_need_no_takeover(self):
        with tempfile.TemporaryDirectory() as directory, State(directory, 'demo') as state:
            order = stop(state)
            risk = _risk_state(state, risk_snapshot('0.0100005', [order]), self.Clock())
            self.assertFalse(risk['manual_takeover'])
            self.assertEqual(risk['residual_btc'], D('0.0000005'))
        for quantity in ('0.000001', '0.00005'):
            with self.subTest(quantity=quantity), tempfile.TemporaryDirectory() as directory:
                with State(directory, 'demo') as state:
                    risk = _risk_state(state, risk_snapshot(quantity), self.Clock())
                    self.assertFalse(risk['manual_takeover'])
                    self.assertEqual(risk['residual_btc'], D(quantity))

    def test_mismatched_native_stop_never_counts_as_confirmed_coverage(self):
        changes = ({'client_id': 'manual-order'}, {'side': 'BUY'}, {'type': 'LIMIT'},
                   {'orig_qty': '0.02'}, {'stop_price': '1'}, {'stop_price': None},
                   {'executed_qty': '0.02'}, {'executed_qty': '0.001'}, {'status': 'PENDING_CANCEL'})
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                with State(directory, 'demo') as state:
                    order = dict(stop(state), **change)
                    risk = _risk_state(state, risk_snapshot(orders=[order]), self.Clock())
                    self.assertEqual(risk['covered_btc'], 0)
                    self.assertTrue(risk['manual_takeover'])
                    self.assertEqual(risk['unconfirmed_order_ids'], [1])

    def test_external_open_buy_requires_takeover_even_when_current_account_is_flat(self):
        order = dict(order_id=99, client_id='manual-buy', side='BUY', type='LIMIT',
                     orig_qty='0.01', executed_qty='0', status='NEW')
        with tempfile.TemporaryDirectory() as directory, State(directory, 'demo') as state:
            risk = _risk_state(state, risk_snapshot('0', [order]), self.Clock())
            self.assertEqual(risk['last_observed_direction'], 'flat')
            self.assertEqual(risk['direction'], 'unknown')
            self.assertTrue(risk['manual_takeover'])
            self.assertIsNone(risk['unprotected_btc'])


class FillGapTests(unittest.TestCase):
    def test_exactly_eighty_days_preserves_time_cursor_and_larger_gap_uses_id(self):
        from test_state import Venue, fill
        for days, expected_id in ((80, None), (81, 1)):
            with self.subTest(days=days), tempfile.TemporaryDirectory() as directory:
                with State(directory, 'demo') as state:
                    venue = Venue()
                    state.trades(venue, 0)
                    venue.now += days * DAY
                    venue.fills.append(fill(2, venue.now))
                    rows = state.trades(venue, 0)
                    self.assertEqual([row['id'] for row in rows], [1, 2])
                    self.assertEqual(venue.from_ids[-1], expected_id)

    def test_long_gap_rejects_a_fill_before_the_id_anchor_without_advancing_watermark(self):
        from test_state import Venue, fill
        with tempfile.TemporaryDirectory() as directory, State(directory, 'demo') as state:
            venue = Venue()
            venue.fills = [fill(10, venue.now)]
            state.trades(venue, 0)
            before = state.get('fill_watermark')
            venue.now += 81 * DAY
            venue.trades = lambda *_args, **_kwargs: [fill(11, venue.now), fill(9, venue.now - 100)]
            with self.assertRaisesRegex(Unknown, 'outside the requested observation'):
                state.trades(venue, 0)
            self.assertEqual(state.get('fill_watermark'), before)
            self.assertEqual(state.db.execute('SELECT id FROM fills').fetchall(), [(10,)])

    def test_long_gap_owned_stop_fill_reconciles_fees_and_preserves_account_anchor(self):
        from spotquant.execution import Lifecycle
        from test_execution import ExecutionTests, add_day
        with tempfile.TemporaryDirectory() as directory:
            config, venue = ExecutionTests().entered(directory)
            with State(directory, config.scope) as state:
                anchor = state.get('execution_anchor')
                resume_id = state.db.execute('SELECT MAX(id) FROM fills').fetchone()[0]
            for _ in range(81):
                add_day(venue, '102')
            venue.trigger('70')
            requested_ids = []
            trades = venue.trades
            def observe(since, from_id=None):
                requested_ids.append(from_id)
                return trades(since, from_id=from_id)
            venue.trades = observe
            with State(directory, config.scope) as state:
                result = cycle(venue, state, config, execute=True)
                self.assertEqual(requested_ids[0], resume_id)
                self.assertEqual(state.get('execution_anchor'), anchor)
                self.assertEqual(result['actual']['btc'], venue.btc)
                self.assertEqual(result['actual']['usdt_free'], venue.cash)
                self.assertTrue(state.get('positions')['40']['dust'])
                self.assertFalse(result['risk_state']['manual_takeover'])
                self.assertEqual(state.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 2)
                venue.cash += D('0.01')
                with self.assertRaisesRegex(Unknown, 'balances differ from durable fills'):
                    Lifecycle(state, venue, config).verify(venue.snapshot(config.account_uid))
                self.assertEqual(state.get('execution_anchor'), anchor)


class SessionAccountTests(unittest.TestCase):
    def test_read_only_manual_fill_drops_follow_without_adopting_position(self):
        from test_execution import venue_before_entry, add_day
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            with State(directory, config.scope) as state:
                cycle(venue, state, config)
                add_day(venue, '101')
                cycle(venue, state, config)
                self.assertIsNotNone(state.get('follows')['40'])
                venue.fills.append(dict(id=1, order_id=99, time=venue.now_ms, qty=D('0.1'),
                                        quote=D('10.1'), price=D(101), buyer=True,
                                        commission=D(0), commission_asset='BTC'))
                venue.btc, venue.cash = D('0.1'), D('989.9')
                with self.assertRaisesRegex(Unknown, 'no durable order allocation'):
                    cycle(venue, state, config)
                self.assertIsNone(state.get('positions')['40'])
                self.assertIsNone(state.get('follows')['40'])
                self.assertEqual(venue.sent, [])

    def test_live_session_reports_the_actual_execution_environment(self):
        from test_execution import venue_before_entry
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'live', '1000')
            venue = venue_before_entry()
            venue.environment = 'live'
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            self.assertEqual(report['status'], 'live_execution')
            self.assertNotIn('live blocked', report['recorded_limits']['execution'])
            self.assertEqual(report['environment'], 'live')
            self.assertEqual(venue.sent, [])

    def test_malformed_dust_checkpoint_cannot_hide_a_tradable_position(self):
        from test_execution import ExecutionTests
        with tempfile.TemporaryDirectory() as directory:
            config, _ = ExecutionTests().entered(directory)
            with State(directory, config.scope) as state:
                saved = state.get('positions')
                for dust in (True, 'false'):
                    positions = deepcopy(saved)
                    positions['40']['dust'] = dust
                    state.set('positions', positions)
                    with self.assertRaisesRegex(Blocked, 'malformed position checkpoint'):
                        _guard_state(state)

    def test_authorized_safety_upgrade_preserves_peaks_and_rejects_downgrade(self):
        from spotquant.crowding import RULE
        from spotquant.execution import Lifecycle
        from spotquant.session import SAFETY_PREDECESSOR
        from test_execution import ExecutionTests
        with tempfile.TemporaryDirectory() as directory:
            config, venue = ExecutionTests().entered(directory)
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                identity, payload, _, _ = next(row for row in lifecycle.rows()
                                               if row[1]['order']['type'] == 'STOP_LOSS' and row[2] == 'resting')
                lifecycle.cancel(identity)
                replacement = lifecycle.prepare(dict(payload['order'], sleeves=[40]), payload['signal_ms'],
                                                state.get('positions'), state.get('follows'))
                self.assertNotEqual(identity, replacement)
                self.assertEqual(next(row[1]['replaced_stop'] for row in lifecycle.rows()
                                      if row[0] == replacement), identity)
                bar = state.get('models')['40']['body']['last']
                unsent = lifecycle.prepare(dict(symbol='BTCUSDT', side='BUY', type='MARKET',
                                               quoteOrderQty='10', sleeves=[40]), bar,
                                           state.get('positions'), state.get('follows'))
                positions = state.get('positions')
                positions['40'].update(peak='110', quote_through_ms=venue.now_ms)
                state.set_many({'rule': SAFETY_PREDECESSOR, 'positions': positions})
                _guard_state(state)
                with self.assertRaisesRegex(Blocked, 'owned account reconciliation'):
                    cycle(venue, state, config)
                self.assertEqual(state.get('rule'), SAFETY_PREDECESSOR)
                anchor = state.get('execution_anchor')
                cycle(venue, state, config, execute=True)
                self.assertEqual(state.get('rule'), RULE)
                self.assertEqual(state.get('positions')['40']['peak'], '110')
                self.assertEqual(state.get('execution_anchor'), anchor)
                status, result = state.db.execute('SELECT status,result FROM intents WHERE id=?', (unsent,)).fetchone()
                self.assertEqual(status, 'settled')
                self.assertTrue(json.loads(result)['not_sent'])
                with patch('spotquant.session.RULE', SAFETY_PREDECESSOR):
                    with self.assertRaisesRegex(Blocked, 'another rule'):
                        _guard_state(state)


class GracefulStopTests(unittest.TestCase):
    def test_interrupt_after_durable_buy_before_book_fold_still_installs_stop(self):
        from spotquant.execution import Lifecycle
        from test_execution import venue_before_entry, add_day, run_day
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            add_day(venue, '101')
            save = Lifecycle.save
            interrupted = []
            def interrupt_after_buy(lifecycle, identity, payload, status, result):
                save(lifecycle, identity, payload, status, result)
                if status == 'settled' and payload['order']['side'] == 'BUY' and not interrupted:
                    interrupted.append((identity, len(lifecycle.state.pending())))
                    raise KeyboardInterrupt
            with patch.object(Lifecycle, 'save', interrupt_after_buy):
                report = run_day(config, venue)
            self.assertEqual(interrupted[0][1], 0)
            self.assertEqual(report['stop_reason'], 'interrupted')
            self.assertTrue(report['closeout_attempted'])
            self.assertFalse(report['manual_takeover'])
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
            self.assertEqual(len([row for row in venue.orders.values()
                                  if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW']), 1)

    def test_interrupt_after_cancellation_reuses_the_prepared_stop(self):
        from spotquant.execution import Lifecycle
        from test_execution import ExecutionTests, add_day, run_day
        with tempfile.TemporaryDirectory() as directory:
            config, venue = ExecutionTests().entered(directory)
            add_day(venue, '103')
            cancel, save = venue.cancel, Lifecycle.save
            prepared = []
            def record(lifecycle, identity, payload, status, result):
                save(lifecycle, identity, payload, status, result)
                if status == 'prepared' and payload['order']['type'] == 'STOP_LOSS':
                    prepared.append(identity)
            def interrupt(identity, **kwargs):
                cancel(identity, **kwargs)
                raise KeyboardInterrupt
            venue.cancel = interrupt
            with patch.object(Lifecycle, 'save', record):
                report = run_day(config, venue)
            active = [row for row in venue.orders.values()
                      if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW']
            self.assertEqual(report['stop_reason'], 'interrupted')
            self.assertFalse(report['manual_takeover'])
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]['clientOrderId'], prepared[0])
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)

    def test_requested_stop_is_latched_and_cannot_start_a_buy_during_closeout(self):
        from test_execution import venue_before_entry, add_day, run_day
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            add_day(venue, '101')
            answers = iter((True, False))
            report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait,
                         stopping=lambda: next(answers, False))
            self.assertEqual(report['stop_reason'], 'requested')
            self.assertTrue(report['closeout_attempted'])
            self.assertEqual(venue.sent, [])
            self.assertFalse(report['manual_takeover'])

    def test_stop_latched_inside_active_cycle_immediately_limits_transport_deadline(self):
        from test_execution import venue_before_entry, add_day
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 600, 5, 'demo', '1000')
            venue = venue_before_entry()
            with State(directory, config.scope) as state:
                cycle(venue, state, config)
            add_day(venue, '101')
            answers = iter((False, True))
            observed_deadlines = []
            def observe_cycle(*args, **kwargs):
                result = cycle(*args, **kwargs)
                observed_deadlines.append((venue._deadline_at, venue.monotonic()))
                return result
            with patch('spotquant.session.cycle', side_effect=observe_cycle):
                report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait,
                             stopping=lambda: next(answers, False))
            self.assertEqual(observed_deadlines[0][0] - observed_deadlines[0][1], 30)
            self.assertEqual(report['stop_reason'], 'requested')
            self.assertEqual(venue.sent, [])

    def test_second_interrupt_and_unavailable_account_leave_explicit_unknown_risk(self):
        from spotquant.execution import Lifecycle
        from test_execution import venue_before_entry, add_day, run_day
        for second_interrupt in (True, False):
            with self.subTest(second_interrupt=second_interrupt), tempfile.TemporaryDirectory() as directory:
                config = Config('1', directory, 60, 10, 'demo', '1000')
                venue = venue_before_entry()
                run_day(config, venue)
                add_day(venue, '101')
                save, snapshot = Lifecycle.save, venue.snapshot
                interrupted = []
                def interrupt_after_buy(lifecycle, identity, payload, status, result):
                    save(lifecycle, identity, payload, status, result)
                    if status == 'settled' and payload['order']['side'] == 'BUY' and not interrupted:
                        interrupted.append(venue.now_ms)
                        raise KeyboardInterrupt
                def unavailable(uid):
                    if interrupted:
                        if second_interrupt:
                            raise KeyboardInterrupt
                        raise Unknown('account unavailable')
                    return snapshot(uid)
                venue.snapshot = unavailable
                with patch.object(Lifecycle, 'save', interrupt_after_buy):
                    report = run_day(config, venue)
                self.assertTrue(report['manual_takeover'])
                self.assertEqual(report['risk_state']['direction'], 'unknown')
                self.assertIsNone(report['risk_state']['unprotected_btc'])
                self.assertLessEqual(venue.now_ms - interrupted[0], 30000)
                self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
                if second_interrupt:
                    self.assertTrue(report['closeout_interrupted'])

    def test_triggered_partial_stop_remains_pending_until_its_remainder_is_resolved(self):
        from spotquant.execution import Lifecycle
        from spotquant.types import floor_step
        from test_execution import ExecutionTests
        with tempfile.TemporaryDirectory() as directory:
            config, venue = ExecutionTests().entered(directory)
            native = next(row for row in venue.orders.values()
                          if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW')
            venue.price = D(70)
            quantity = floor_step(D(native['quantity']) / 2, D('0.00001'))
            quote = quantity * venue.price
            venue.btc -= quantity
            venue.cash += quote * (1 - venue.fee)
            venue._fill(native, quantity, quote)
            native.update(status='PARTIALLY_FILLED', executedQty=str(quantity), quote=str(quote))
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                lifecycle.recover()
                self.assertEqual([item['id'] for item in state.pending()], [native['clientOrderId']])
                risk = _risk_state(state, venue.snapshot(config.account_uid), venue)
                self.assertEqual(risk['covered_btc'], 0)
                self.assertTrue(risk['manual_takeover'])
                result = cycle(venue, state, config, execute=True)
                self.assertFalse(state.pending())
                self.assertFalse(result['risk_state']['manual_takeover'])
                self.assertLess(venue.btc, D('0.00001'))


if __name__ == '__main__':
    unittest.main()
