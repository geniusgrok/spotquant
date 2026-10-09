import tempfile
import json
from urllib.parse import urlsplit, parse_qs
from decimal import Decimal as D
from unittest import TestCase

from spotquant.binance import Binance
from spotquant.config import Config
from spotquant.model import DAY, ORIGIN
from venue_fixture import TestVenue
from spotquant.session import run
from spotquant.state import State
from spotquant.types import Unknown


def run_day(config, venue):
    return run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)


def venue_before_entry():
    prices = [D(100)] * 400 + [D(98)]
    from venue_fixture import KnownFeatures
    venue = TestVenue([(ORIGIN + i * DAY, p, p, p) for i, p in enumerate(prices)])
    venue.now_ms += 60000
    venue.crowding_features = KnownFeatures
    return venue


def add_day(venue, close, high=None):
    close = D(close)
    venue.bars.append((venue.bars[-1][0] + DAY, D(high or close), close, close))
    venue.now_ms = venue.bars[-1][0] + DAY + 60000
    venue.price = close


class OrderAdapter(Binance):
    """Real signed order methods over the in-memory account, without network I/O."""
    def __init__(self, account, fail_after):
        self.account, self.fail_after, self.fault_used, self.fail_query = account, fail_after, False, False
        super().__init__(key='test-key', secret='test-secret', environment='demo',
                         capital_limit=account.capital_limit, demo_execution_uid=account.uid,
                         clock=account.clock, opener=self._open)

    def _open(self, method, url, _headers):
        parsed = urlsplit(url)
        params = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        if parsed.path == '/api/v3/time':
            return 200, json.dumps({'serverTime': self.account.now_ms}).encode()
        if parsed.path != '/api/v3/order':
            raise AssertionError(parsed.path)
        identity = params.get('newClientOrderId') if method == 'POST' else params['origClientOrderId']
        if method == 'GET' and self.fail_query:
            self.fail_query = False
            return 504, b'{"code":-1007,"msg":"backend timeout"}'
        if method == 'POST':
            fields = ('symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice')
            order = {field: params[field] for field in fields if field in params}
            row = self.account.submit(identity, order)
            fault = order['side'] == 'BUY'
        elif method == 'DELETE':
            row = self.account.cancel(identity)
            fault = True
        else:
            row = self.account.query(identity)
            fault = False
        if not self.fault_used and fault and method == self.fail_after:
            self.fault_used = self.fail_query = True
            self.account.now_ms += 1200  # The first query now crosses the entry deadline.
        native = dict(row, origQty=row.get('quantity', '0'),
                      origQuoteOrderQty=row.get('quoteOrderQty', '0'))
        return 200, json.dumps(native, default=str).encode()

    def snapshot(self, uid):
        return self.account.snapshot(uid)

    def completed_daily(self, after):
        return self.account.completed_daily(after)

    def daily_open(self, open_ms):
        return self.account.daily_open(open_ms)

    def trades(self, since):
        return self.account.trades(since)

    def crowding_features(self):
        return self.account.crowding_features()

    def monotonic(self):
        return self.account.monotonic()

    def wait(self, seconds):
        self.account.wait(seconds)


class ExecutionTests(TestCase):
    def test_stop_ownership_is_durable_and_uses_native_creation_time_only(self):
        from spotquant.execution import Lifecycle, allocation_owners
        from spotquant.types import Blocked
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            first = venue.now_ms
            positions = {'40': {'qty': '1', 'first_ms': first}}
            stop = dict(symbol='BTCUSDT', side='SELL', type='STOP_LOSS',
                        quantity='1', stopPrice='72', sleeves=[40])
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                identity = lifecycle.prepare(stop, venue.bars[-1][0], positions, {'40': None})
                payload = lifecycle.rows()[0][1]
                self.assertEqual(payload['position_first_ms'], {'40': first})
                with self.assertRaisesRegex(Blocked, 'position ownership'):
                    lifecycle.prepare(stop, venue.bars[-1][0],
                                      {'40': {'qty': '1', 'first_ms': first + 1}}, {'40': None})
                lifecycle.save(identity, payload, 'resting',
                               dict(orderId=7, status='NEW', executedQty='0', time=first + 10,
                                    updateTime=first + 20))
            with State(directory, config.scope) as state:
                owner = Lifecycle(state, venue, config).owners()['7']
                self.assertEqual(owner['position_first_ms'], {'40': first})
                self.assertEqual(owner['native_created_ms'], first + 10)
            owners = allocation_owners((({}, dict(orderId=8, transactTime=first + 30)),
                                         ({}, dict(orderId=9, updateTime=first + 40, workingTime=first + 50))))
            self.assertEqual(owners['8']['native_created_ms'], first + 30)
            self.assertIsNone(owners['9']['native_created_ms'])
            self.assertEqual(venue.sent, [])

    def test_adapter_buy_readback_crosses_deadline_and_closeout_protects(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            account = venue_before_entry()
            run_day(config, account)
            add_day(account, '101')
            adapter = OrderAdapter(account, 'POST')
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertTrue(report['closeout_attempted'])
            self.assertFalse(report['pending_intents'])
            self.assertTrue(any(row['type'] == 'STOP_LOSS' and row['status'] == 'NEW'
                                for row in account.orders.values()))
            self.assertEqual(len([row for row in account.orders.values() if row['side'] == 'BUY']), 1)

    def test_adapter_cancel_readback_crosses_deadline_and_closeout_replaces(self):
        with tempfile.TemporaryDirectory() as directory:
            config, account = self.entered(directory)
            add_day(account, '103', '110')
            run_day(config, account)
            add_day(account, '104', '112')
            adapter = OrderAdapter(account, 'DELETE')
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertTrue(report['closeout_attempted'])
            self.assertFalse(report['pending_intents'])
            self.assertTrue(any(row['type'] == 'STOP_LOSS' and row['status'] == 'NEW'
                                and D(row['stopPrice']) > D('73.44') for row in account.orders.values()))

    def test_bnb_fee_on_partial_sell_keeps_remainder_protectable(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            add_day(venue, '97')
            venue.fraction = D('.5')
            original = venue._fill
            def bnb_fee(row, qty, quote):
                original(row, qty, quote)
                if row['side'] == 'SELL':
                    venue.cash += quote * venue.fee
                    venue.fills[-1]['commission_asset'] = 'BNB'
                    venue.fills[-1]['commission'] = D('.001')
            venue._fill = bnb_fee
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertIn('BNB', report['risk_state']['unvalued_fee_assets'])
            self.assertFalse(report['risk_state']['fee_valuation_complete'])
            self.assertTrue(any(row['type'] == 'STOP_LOSS' and row['status'] == 'NEW'
                                for row in venue.orders.values()))

    def test_unknown_fee_quote_does_not_block_existing_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            add_day(venue, '103', '110')
            run_day(config, venue)
            add_day(venue, '104', '112')
            original = venue.snapshot
            def missing_fee(uid):
                return dict(original(uid), fee_mode='unknown', fee_rate=None, fee_status='unavailable')
            venue.snapshot = missing_fee
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertTrue(any(row['type'] == 'STOP_LOSS' and row['status'] == 'NEW'
                                and D(row['stopPrice']) > D('73.44') for row in venue.orders.values()))

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
            venue.reject_stop = True
            for _ in range(2):
                self.assertEqual(run_day(config, venue)['status'], 'unknown')
            self.assertEqual(len([row for row in venue.orders.values() if row['type'] == 'MARKET']), 1)

    def test_known_stop_rejection_reduces_unprotected_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            add_day(venue, '101')
            venue.reject_stop_known = True
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertLess(venue.btc * venue.price, D('5'))
            self.assertEqual(report['risk_state']['unprotected_btc'], venue.btc)

    def test_buy_below_net_protection_minimum_is_blocked(self):
        from spotquant.execution import Lifecycle
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '5.01')
            venue = venue_before_entry()
            venue.capital_limit = config.capital_limit
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, venue, config)
                decision = {'orders': [dict(symbol='BTCUSDT', side='BUY', type='MARKET',
                                            quoteOrderQty='5.00', sleeves=[40])],
                            'protections': [], 'sleeves': {}}
                from spotquant.types import Blocked
                with self.assertRaisesRegex(Blocked, 'net buy'):
                    lifecycle.act(decision, venue.bars[-1][0], venue.snapshot('1'))
            self.assertEqual(venue.sent, [])

    def test_crossed_last_price_forces_exit_even_if_average_is_higher(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            add_day(venue, '103', '110')
            snapshot = venue.snapshot
            def divergent(uid):
                row = snapshot(uid)
                row['avg_price'] = D('105')
                row['last_price'] = D('70')
                return row
            venue.snapshot = divergent
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertTrue(any(row['side'] == 'SELL' and row['type'] == 'MARKET'
                                for row in venue.orders.values()))
            self.assertFalse(any(row['status'] == 'NEW' and D(row['stopPrice']) > D('70')
                                 for row in venue.orders.values() if row['type'] == 'STOP_LOSS'))

    def test_price_crossing_during_replacement_reports_unprotected_btc(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            add_day(venue, '103', '110')
            run_day(config, venue)
            add_day(venue, '104', '112')
            original = venue.cancel
            def cancel(identity):
                original(identity)
                venue.price = D('70')
            venue.cancel = cancel
            observed = venue.snapshot
            def divergent(uid):
                row = observed(uid)
                row['avg_price'] = D('105')
                return row
            venue.snapshot = divergent
            report = run_day(config, venue)
            self.assertEqual(report['status'], 'demo_execution')
            self.assertTrue(any('last price crossed after stop cancellation' in row['reason']
                                for row in report['errors']))
            self.assertTrue(report['closeout_attempted'])
            self.assertLess(report['risk_state']['unprotected_btc'] * D('85'), D('5'))
            self.assertTrue(report['manual_takeover'])

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
            self.assertEqual(len(stops), 1)
            # Remainder keeps the 28% trail under the 101 fill. 101 * 0.72 = 72.72.
            self.assertTrue(all(D(row['stopPrice']) == D('72.72') for row in stops))
            self.assertLess(abs(sum(D(row['quantity']) for row in stops) - venue.btc), D('.00004'))
            before = len(venue.sent)
            run_day(config, venue)
            self.assertEqual(len(venue.sent), before)

    def test_the_single_sleeve_stop_covers_the_position(self):
        with tempfile.TemporaryDirectory() as directory:
            _, venue = self.entered(directory)
            stops = [row for row in venue.orders.values()
                     if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW']
            self.assertEqual(len(stops), 1)
            self.assertLess(abs(D(stops[0]['quantity']) - venue.btc), D('.00004'))

    def test_partial_entry_restart_stop_amend_and_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            self.assertEqual(run_day(config, venue)['errors'], [])
            add_day(venue, '101')
            venue.fraction, venue.lose_ack = D('.5'), True
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertGreater(venue.btc, 0)
            sent = len(venue.sent)
            run_day(config, venue)
            self.assertEqual(len(venue.sent), sent)
            add_day(venue, '102', '110')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            active = [row for row in venue.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            # The fill is one minute into the next daily bar, so that high is
            # not yet the post-fill peak. 101 * 0.72 = 72.72.
            self.assertEqual(D(active[0]['stopPrice']), D('72.72'))
            add_day(venue, '103', '110')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            active = [row for row in venue.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            self.assertEqual(D(active[0]['stopPrice']), D('79.20'))
            venue.now_ms += 2000
            venue.trigger('70')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertLess(venue.btc * venue.price, 5)
            with State(directory, config.scope) as state:
                self.assertFalse(state.pending())

    def test_touch_exit_permission_survives_restart_and_waits_for_a_new_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            venue.price = D('100.4')
            cancel = venue.cancel
            def crash_after_cancel(identity):
                cancel(identity)
                raise KeyboardInterrupt
            venue.cancel = crash_after_cancel
            self.assertEqual(run_day(config, venue)['stop_reason'], 'interrupted')
            with State(directory, config.scope) as state:
                payload = json.loads(state.db.execute(
                    "SELECT payload FROM intents WHERE status='prepared'"
                ).fetchone()[0])
                self.assertEqual(payload['rearm'], {'40': True})
            venue.cancel = cancel
            venue.price = D('102')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            sales = [row for row in venue.orders.values()
                     if row['type'] == 'MARKET' and row['side'] == 'SELL']
            self.assertEqual(len(sales), 1)
            with State(directory, config.scope) as state:
                payload = json.loads(state.db.execute('SELECT payload FROM intents WHERE id=?',
                                                       (sales[0]['clientOrderId'],)).fetchone()[0])
                self.assertEqual(payload['rearm'], {'40': True})
                self.assertEqual(state.get('exit_through')['40'], venue.bars[-1][0])
            venue.price = D('102')
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
            add_day(venue, '103')
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 2)
            sent = len(venue.sent)
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len(venue.sent), sent)

    def test_native_stop_cannot_rejoin_the_old_long_shadow_on_a_later_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            venue.now_ms += 2000
            venue.trigger('70')
            self.assertEqual(run_day(config, venue)['errors'], [])
            for close in ('103', '104'):
                add_day(venue, close)
                self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len([row for row in venue.orders.values() if row['side'] == 'BUY']), 1)
            with State(directory, config.scope) as state:
                self.assertTrue(state.get('models')['40']['body']['shadow_in'])
                self.assertIsNotNone(state.get('models')['40']['body']['shadow_blocked'])
                self.assertIsNone(state.get('follows')['40'])

    def test_higher_second_fill_of_one_buy_raises_native_stop_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            self.assertEqual(run_day(config, venue)['errors'], [])
            add_day(venue, '101')
            original = venue.submit
            def two_fills(identity, payload):
                if payload['side'] != 'BUY':
                    return original(identity, payload)
                venue.fraction = D('.5')
                row = original(identity, payload)
                venue.fraction = D(1)
                venue.now_ms += 1
                venue.price = D('120')
                quote = D(payload['quoteOrderQty']) - D(row['quote'])
                quantity = quote / venue.price
                venue.cash -= quote
                venue.btc += quantity * (1 - venue.fee)
                venue._fill(row, quantity, quote)
                row.update(status='FILLED', executedQty=str(venue.btc), quote=payload['quoteOrderQty'])
                return row
            venue.submit = two_fills
            self.assertEqual(run_day(config, venue)['errors'], [])
            active = [row for row in venue.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            self.assertEqual(D(active[0]['stopPrice']), D('86.40'))
            with State(directory, config.scope) as state:
                position = state.get('positions')['40']
                self.assertEqual(D(position['peak']), D('120'))
                self.assertEqual(D(position['qty']), venue.btc)
                self.assertEqual(D(position['entry_fill']),
                                 D('1000') / sum(trade['qty'] for trade in venue.fills if trade['buyer']))
            sent = len(venue.sent)
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len(venue.sent), sent)
            with State(directory, config.scope) as state:
                self.assertEqual(state.get('positions')['40'], position)

    def test_crash_reversal_buy_keeps_actual_repair_allocation_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            prices = [D(100)] * 400 + [D(50), D(46)]
            from venue_fixture import KnownFeatures
            venue = TestVenue([(ORIGIN + i * DAY, p, p, p) for i, p in enumerate(prices)])
            venue.now_ms += 60_000
            venue.crowding_features = KnownFeatures
            config = Config('1', directory, 1, 1, 'demo', '1000')
            self.assertEqual(run_day(config, venue)['errors'], [])
            add_day(venue, '48.76')
            self.assertEqual(run_day(config, venue)['errors'], [])
            buys = [row for row in venue.orders.values() if row['side'] == 'BUY']
            self.assertEqual(len(buys), 1)
            self.assertFalse(any(row['type'] == 'MARKET' and row['side'] == 'SELL'
                                 for row in venue.orders.values()))
            with State(directory, config.scope) as state:
                self.assertTrue(state.get('positions')['40']['repair'])
                payload = json.loads(state.db.execute('SELECT payload FROM intents WHERE id=?',
                                                       (buys[0]['clientOrderId'],)).fetchone()[0])
                self.assertEqual(payload['repair'], {'40': True})
            self.assertEqual(run_day(config, venue)['errors'], [])
            add_day(venue, '47')
            self.assertEqual(run_day(config, venue)['errors'], [])
            with State(directory, config.scope) as state:
                self.assertTrue(state.get('positions')['40']['repair'])
            self.assertFalse(any(row['type'] == 'MARKET' and row['side'] == 'SELL'
                                 for row in venue.orders.values()))

    def test_catchup_partial_buy_replays_repair_handoff_in_fill_order(self):
        from spotquant.execution import Lifecycle
        from spotquant.model import Model
        from spotquant.session import RULE, cycle
        from venue_fixture import KnownFeatures
        with tempfile.TemporaryDirectory() as directory:
            prices = [D(100)] * 400 + [D(50), D(46), D('48.76'), D(47), D(50), D(100)]
            venue = TestVenue([(ORIGIN + i * DAY, p, p, p) for i, p in enumerate(prices)])
            venue.now_ms += 60_000
            venue.crowding_features = KnownFeatures
            signal = ORIGIN + 402 * DAY
            first = signal + DAY + 60_000
            model = Model()
            for open_ms, open_price, high, low, close in venue.completed_daily(None):
                if open_ms > signal:
                    break
                model.advance_open(open_ms, open_price)
                model.update(open_ms, high, low, close)
            model.advance_open(signal + DAY, D(47))
            venue.fills = [dict(id=index, order_id=7, time=first + offset * DAY,
                                qty=D(1), quote=price, price=price, buyer=True,
                                commission=D(0), commission_asset='BTC')
                           for index, offset, price in ((1, 0, D('48.76')), (2, 1, D(200)))]
            venue.btc, venue.cash = D(2), D('751.24')
            config = Config('1', directory, 1, 1, 'demo', '1000')
            with State(directory, config.scope) as state:
                state.set_many({'rule': RULE, 'models': {'40': model.checkpoint()},
                                'positions': {'40': None},
                                'follows': {'40': {'signal_ms': signal, 'repair': True}},
                                'entries_after': signal, 'accounted_ids': [], 'exit_through': {}})
                payload = {'order': {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET',
                                     'quoteOrderQty': '248.76'}, 'sleeves': [40],
                           'weights': {'40': '1'}, 'signal_ms': signal, 'repair': {'40': True}}
                Lifecycle(state, venue, config).save('recorded-buy', payload, 'settled',
                                                     {'orderId': 7, 'status': 'FILLED', 'executedQty': '2'})
                report = cycle(venue, state, config, execute=False)
                self.assertEqual(report['status'], 'read_only')
                position = state.get('positions')['40']
                self.assertEqual(D(position['entry_fill']), D('124.38'))
                self.assertEqual(D(position['qty']), D(2))
                self.assertEqual(D(position['peak']), D(200))
                self.assertFalse(position['repair'])
                self.assertIsNone(position['repair_peak'])
                self.assertTrue(position['adverse'])
                self.assertEqual(position['through'], ORIGIN + 405 * DAY)
            with State(directory, config.scope) as state:
                self.assertEqual(cycle(venue, state, config, execute=False)['status'], 'read_only')
                self.assertEqual(state.get('positions')['40'], position)
            self.assertEqual(venue.sent, [])

    def test_query_outage_after_send_recovers_with_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            add_day(venue, '101')
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
