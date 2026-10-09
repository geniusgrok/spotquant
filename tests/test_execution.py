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
from spotquant.types import Blocked, Unknown


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
        self.before_post_snapshot, self.dispatching = None, False
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
        identity = params.get('newClientOrderId') if method == 'POST' else (
            int(params['orderId']) if 'orderId' in params else params['origClientOrderId'])
        if method == 'GET' and self.fail_query:
            self.fail_query = False
            return 504, b'{"code":-1007,"msg":"backend timeout"}'
        if method == 'POST':
            fields = ('symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice')
            order = {field: params[field] for field in fields if field in params}
            row = self.account.submit(identity, order)
            fault = order['side'] == 'BUY'
        elif method == 'DELETE':
            original = self.account.query(identity)
            row = self.account.cancel(original['clientOrderId'], order_id=original['orderId'],
                                      cancel_id=params.get('newClientOrderId'))
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
        if self.dispatching and self.before_post_snapshot is not None:
            hook, self.before_post_snapshot = self.before_post_snapshot, None
            hook()
        return self.account.snapshot(uid)

    def submit(self, identity, payload, **kwargs):
        self.dispatching = True
        try:
            return super().submit(identity, payload, **kwargs)
        finally:
            self.dispatching = False

    def completed_daily(self, after):
        return self.account.completed_daily(after)

    def daily_open(self, open_ms):
        return self.account.daily_open(open_ms)

    def trades(self, since, from_id=None):
        return self.account.trades(since, from_id=from_id)

    def crowding_features(self):
        return self.account.crowding_features()

    def monotonic(self):
        return self.account.monotonic()

    def wait(self, seconds):
        self.account.wait(seconds)


class ExecutionTests(TestCase):
    def test_recovery_rejects_missing_client_id_without_cancel_alias(self):
        from spotquant.execution import Lifecycle
        for cancel_id in (None, ''):
            for missing in ('absent', None, ''):
                with self.subTest(cancel_id=cancel_id, client_id=missing), tempfile.TemporaryDirectory() as directory:
                    config, venue = self.entered(directory)
                    stop = next(row for row in venue.orders.values() if row['status'] == 'NEW')
                    query = venue.query
                    def malformed(reference):
                        row = dict(query(reference))
                        if missing == 'absent':
                            row.pop('clientOrderId')
                        else:
                            row['clientOrderId'] = missing
                        return row
                    venue.query = malformed
                    sent = list(venue.sent)
                    with State(directory, config.scope) as state:
                        lifecycle = Lifecycle(state, venue, config)
                        identity, payload, status, result = next(row for row in lifecycle.rows()
                                                                 if row[0] == stop['clientOrderId'])
                        if cancel_id is not None:
                            payload = dict(payload, cancel_id=cancel_id)
                            lifecycle.save(identity, payload, status, result)
                        before = lifecycle.rows()
                        with self.assertRaisesRegex(Unknown, 'native order identity differs'):
                            lifecycle.recover()
                        self.assertEqual(lifecycle.rows(), before)
                    self.assertEqual(venue.sent, sent)
                    self.assertEqual(stop['status'], 'NEW')

    def test_verification_rejects_missing_open_client_id_without_cancel_alias(self):
        from spotquant.execution import Lifecycle
        for cancel_id in (None, ''):
            for missing in ('absent', None, ''):
                with self.subTest(cancel_id=cancel_id, client_id=missing), tempfile.TemporaryDirectory() as directory:
                    config, venue = self.entered(directory)
                    snapshot = venue.snapshot(config.account_uid)
                    if missing == 'absent':
                        snapshot['orders'][0].pop('client_id')
                    else:
                        snapshot['orders'][0]['client_id'] = missing
                    sent = list(venue.sent)
                    with State(directory, config.scope) as state:
                        lifecycle = Lifecycle(state, venue, config)
                        identity, payload, status, result = next(row for row in lifecycle.rows()
                                                                 if row[3]['status'] == 'NEW')
                        if cancel_id is not None:
                            lifecycle.save(identity, dict(payload, cancel_id=cancel_id), status, result)
                        before = lifecycle.rows()
                        with self.assertRaisesRegex(Unknown, 'external open order blocks'):
                            lifecycle.verify(snapshot)
                        self.assertEqual(lifecycle.rows(), before)
                    self.assertEqual(venue.sent, sent)

    def test_cancel_changes_client_id_and_restart_recovers_by_native_id(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            old_stop = next(row for row in venue.orders.values() if row['status'] == 'NEW')
            old_id, native_id = old_stop['clientOrderId'], old_stop['orderId']
            add_day(venue, '103')
            original = venue.cancel
            def lost_cancel(identity, **kwargs):
                original(identity, **kwargs)
                venue.unknown_query = True
                raise Unknown('cancel acknowledged by engine; response unavailable')
            venue.cancel = lost_cancel
            report = run_day(config, venue)
            self.assertTrue(report['pending_intents'])
            self.assertNotIn(old_id, venue.orders)
            with State(directory, config.scope) as state:
                payload, status = state.db.execute('SELECT payload,status FROM intents WHERE id=?', (old_id,)).fetchone()
                self.assertEqual(status, 'canceling')
                self.assertEqual(json.loads(payload)['cancel_id'], old_stop['clientOrderId'])
            venue.cancel, venue.unknown_query = original, False
            queried = []
            query = venue.query
            def readback(reference):
                queried.append(reference)
                return query(reference)
            venue.query = readback
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertIn(native_id, queried)
            self.assertEqual(len([row for row in venue.orders.values() if row['status'] == 'NEW']), 1)
            self.assertFalse(report['pending_intents'])

    def test_partial_stop_fill_during_cancel_resizes_the_prepared_sale(self):
        from spotquant.types import floor_step
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            starting = venue.btc
            venue.price = D('100.4')
            original = venue.cancel
            stop_fill = []
            def partially_filled(identity, **kwargs):
                row = venue.query(kwargs['order_id'])
                qty = floor_step(D(row['quantity']) / 2, D('.00001'))
                venue.price = D('70')
                quote = qty * venue.price
                venue.btc -= qty
                venue.cash += quote * (1 - venue.fee)
                venue._fill(row, qty, quote)
                row.update(status='PARTIALLY_FILLED', executedQty=str(qty))
                stop_fill.append(qty)
                return original(identity, **kwargs)
            venue.cancel = partially_filled
            report = run_day(config, venue)
            sales = [row for row in venue.orders.values() if row['type'] == 'MARKET' and row['side'] == 'SELL']
            self.assertEqual(report['errors'], [])
            self.assertEqual(len(sales), 1)
            self.assertEqual(D(sales[0]['quantity']), floor_step(starting - stop_fill[0], D('.00001')))
            self.assertLess(venue.btc * venue.price, D('5'))
            self.assertFalse(report['pending_intents'])
            with State(directory, config.scope) as state:
                self.assertFalse(state.db.execute("SELECT 1 FROM intents WHERE status='rejected'").fetchone())
            before = len(venue.sent)
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len(venue.sent), before)

    def test_final_adapter_observation_blocks_external_balance_before_post(self):
        for asset in ('cash', 'btc'):
            with self.subTest(asset=asset), tempfile.TemporaryDirectory() as directory:
                config = Config('1', directory, 1, 1, 'demo', '1000')
                account = venue_before_entry()
                run_day(config, account)
                add_day(account, '101')
                adapter = OrderAdapter(account, None)
                adapter.before_post_snapshot = lambda: setattr(account, asset, getattr(account, asset) + D(1))
                report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
                self.assertEqual(account.sent, [])
                self.assertFalse(adapter.write_attempted)
                with State(directory, config.scope) as state:
                    status, result = state.db.execute('SELECT status,result FROM intents').fetchone()
                    self.assertEqual(status, 'prepared')
                    self.assertTrue(json.loads(result)['not_sent'])

    def test_final_cap_accounts_for_all_btc_at_the_latest_price(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            account = venue_before_entry()
            account.btc = D('.000006')
            run_day(config, account)
            add_day(account, '101')
            adapter = OrderAdapter(account, None)
            adapter.before_post_snapshot = lambda: setattr(account, 'price', D('2000000'))
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(account.sent, [])
            self.assertFalse(adapter.write_attempted)
            self.assertTrue(any('whole-account capital ceiling' in row['reason'] for row in report['errors']))

    def test_second_observation_can_turn_hold_into_a_touch_sale(self):
        with tempfile.TemporaryDirectory() as directory:
            config, account = self.entered(directory)
            adapter = OrderAdapter(account, None)
            count = 0
            def touch_on_act(uid):
                nonlocal count
                count += 1
                if count == 2:
                    account.price = D('100.1')
                return account.snapshot(uid)
            adapter.snapshot = touch_on_act
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertLess(account.btc * account.price, D('5'))
            self.assertEqual(len([row for row in account.orders.values()
                                  if row['type'] == 'MARKET' and row['side'] == 'SELL']), 1)

    def test_unplaceable_exit_preserves_the_confirmed_native_stop(self):
        for field, minimum in (('min_notional', '2000'), ('min_qty', '20')):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                config, venue = self.entered(directory)
                stop = next(row for row in venue.orders.values() if row['status'] == 'NEW')
                original_id, sent = stop['clientOrderId'], len(venue.sent)
                venue.price = D('100.1')
                snapshot = venue.snapshot
                def higher_minimum(uid):
                    return dict(snapshot(uid), **{field: D(minimum)})
                venue.snapshot = higher_minimum
                report = run_day(config, venue)
                self.assertEqual(report['status'], 'demo_execution')
                self.assertEqual(report['errors'], [])
                self.assertEqual(len(venue.sent), sent)
                self.assertEqual(stop['status'], 'NEW')
                self.assertEqual(stop['clientOrderId'], original_id)
                self.assertFalse(report['pending_intents'])

    def test_final_peak_keeps_prepared_protection_then_raises_it_next_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            config, account = self.entered(directory)
            add_day(account, '103')
            adapter = OrderAdapter(account, None)
            count = 0
            def peak_before_post(uid):
                nonlocal count
                count += 1
                # Two observations cancel the old stop; then two re-observe;
                # the adapter's fifth is the replacement's final preflight.
                if count == 5:
                    account.price = D('150')
                elif count > 5:
                    account.price = D('120')
                return account.snapshot(uid)
            adapter.snapshot = peak_before_post
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['pending_intents'])
            active = [row for row in account.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            self.assertEqual(D(active[0]['stopPrice']), D('74.16'))
            self.assertFalse(report.get('closeout_attempted', False))
            with State(directory, config.scope) as state:
                self.assertEqual(D(state.get('positions')['40']['peak']), D('150'))
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['pending_intents'])
            active = [row for row in account.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            self.assertEqual(D(active[0]['stopPrice']), D('108'))

    def test_slow_rising_quotes_confirm_protection_before_every_poll_and_shutdown(self):
        for already_entered in (False, True):
            with self.subTest(already_entered=already_entered), tempfile.TemporaryDirectory() as directory:
                if already_entered:
                    _, account = self.entered(directory)
                    add_day(account, '103')
                else:
                    account = venue_before_entry()
                    run_day(Config('1', directory, 1, 1, 'demo', '1000'), account)
                    add_day(account, '101')
                config = Config('1', directory, 150, 5, 'demo', '1000')
                adapter = OrderAdapter(account, None)
                polls = []
                def rising_snapshot(uid):
                    account.now_ms += 1000
                    account.price += D('.05')
                    return account.snapshot(uid)
                def protected_wait(seconds):
                    active = [row for row in account.orders.values() if row['status'] == 'NEW']
                    polls.append(active)
                    self.assertEqual(len(active), 1)
                    self.assertEqual(active[0]['type'], 'STOP_LOSS')
                    self.assertLess(account.btc - D(active[0]['quantity']), D('.00001'))
                    account.wait(seconds)
                adapter.snapshot, adapter.wait = rising_snapshot, protected_wait
                report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
                self.assertEqual(report['errors'], [])
                self.assertGreaterEqual(len(polls), 10)
                self.assertFalse(report['pending_intents'])
                active = [row for row in account.orders.values() if row['status'] == 'NEW']
                self.assertEqual(len(active), 1)
                self.assertLess(report['risk_state']['unprotected_btc'] * account.price, D('5'))
                with State(directory, config.scope) as state:
                    self.assertFalse(state.pending())

    def test_final_quote_exits_under_the_latest_peak_above_the_fixed_prepared_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            config, account = self.entered(directory)
            add_day(account, '103')
            adapter = OrderAdapter(account, None)
            stop_count = len([row for row in account.orders.values() if row['type'] == 'STOP_LOSS'])
            count = 0
            def rising_then_crossed(uid):
                nonlocal count
                count += 1
                account.price = D('150') if count == 3 else D('151') if count == 4 else D('107') if count >= 5 else D('103')
                return account.snapshot(uid)
            adapter.snapshot = rising_then_crossed
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['pending_intents'])
            self.assertLess(account.btc * account.price, D('5'))
            sales = [row for row in account.orders.values() if row['type'] == 'MARKET' and row['side'] == 'SELL']
            self.assertEqual(len(sales), 1)
            self.assertEqual(len([row for row in account.orders.values() if row['type'] == 'STOP_LOSS']), stop_count)

    def test_risk_deadline_keeps_confirmed_protection_when_only_the_peak_rises(self):
        with tempfile.TemporaryDirectory() as directory:
            config, account = self.entered(directory)
            add_day(account, '103')
            stop = next(row for row in account.orders.values() if row['status'] == 'NEW')
            original_id, sent = stop['clientOrderId'], len(account.sent)
            adapter = OrderAdapter(account, None)
            count = 0
            def peak_at_deadline(uid):
                nonlocal count
                count += 1
                if count == 2:
                    account.now_ms += 1000
                    account.price = D('150')
                return account.snapshot(uid)
            adapter.snapshot = peak_at_deadline
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['pending_intents'])
            self.assertEqual(stop['status'], 'NEW')
            self.assertEqual(stop['clientOrderId'], original_id)
            self.assertEqual(len(account.sent), sent)
            with State(directory, config.scope) as state:
                self.assertEqual(D(state.get('positions')['40']['peak']), D('150'))

    def test_deadline_keeps_native_stop_with_proven_native_position_time(self):
        from spotquant.execution import Lifecycle
        with tempfile.TemporaryDirectory() as directory:
            config, account = self.entered(directory)
            add_day(account, '103')
            stop = next(row for row in account.orders.values() if row['status'] == 'NEW')
            sent = list(account.sent)
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, account, config)
                identity, payload, status, result = next(row for row in lifecycle.rows() if row[0] == stop['clientOrderId'])
                payload.pop('position_first_ms')
                self.assertGreaterEqual(result['time'], state.get('positions')['40']['first_ms'])
                lifecycle.save(identity, payload, status, result)
            adapter, count = OrderAdapter(account, None), 0
            def deadline_snapshot(uid):
                nonlocal count
                count += 1
                if count == 2:
                    account.now_ms += 1000
                    account.price = D('150')
                return account.snapshot(uid)
            adapter.snapshot = deadline_snapshot
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['pending_intents'])
            self.assertEqual(stop['status'], 'NEW')
            self.assertEqual(account.sent, sent)

    def test_deadline_keeps_native_stop_over_prepared_raise_then_reuses_only_the_unsent_target(self):
        from spotquant.execution import Lifecycle
        with tempfile.TemporaryDirectory() as directory:
            config, account = self.entered(directory)
            add_day(account, '103.03')
            run(config, account, execute=False, monotonic=account.monotonic, wait=account.wait)
            old_stop = next(row for row in account.orders.values() if row['status'] == 'NEW')
            original_id, sent = old_stop['clientOrderId'], len(account.sent)
            with State(directory, config.scope) as state:
                lifecycle = Lifecycle(state, account, config)
                bar = state.get('models')['40']['body']['last']
                order = dict(symbol='BTCUSDT', side='SELL', type='STOP_LOSS',
                             quantity=old_stop['quantity'], stopPrice='74.18', sleeves=[40])
                identity = lifecycle.prepare(order, bar, state.get('positions'), state.get('follows'))
                prepared = next(row[1] for row in lifecycle.rows() if row[0] == identity)
            adapter, count = OrderAdapter(account, None), 0
            def deadline_snapshot(uid):
                nonlocal count
                count += 1
                if count == 2:
                    account.now_ms += 1000
                return account.snapshot(uid)
            adapter.snapshot = deadline_snapshot
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['pending_intents'])
            self.assertEqual(old_stop['status'], 'NEW')
            self.assertEqual(old_stop['clientOrderId'], original_id)
            self.assertEqual(len(account.sent), sent)
            with State(directory, config.scope) as state:
                payload, status, result = state.db.execute('SELECT payload,status,result FROM intents WHERE id=?', (identity,)).fetchone()
                self.assertEqual(json.loads(payload), prepared)
                self.assertEqual(status, 'settled')
                self.assertTrue(json.loads(result)['not_sent'])
            adapter.snapshot = account.snapshot
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['pending_intents'])
            self.assertEqual(account.sent.count(identity), 1)
            self.assertEqual(account.orders[identity]['status'], 'NEW')

    def test_final_small_peak_keeps_the_same_stop_without_an_extra_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            config, account = self.entered(directory)
            add_day(account, '103.03')
            adapter = OrderAdapter(account, None)
            adapter.before_post_snapshot = lambda: setattr(account, 'price', D('103.04'))
            attempted, submit = [], adapter.submit
            def counted(identity, payload, **kwargs):
                attempted.append(dict(payload))
                return submit(identity, payload, **kwargs)
            adapter.submit = counted
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['pending_intents'])
            self.assertEqual(len(attempted), 1)
            self.assertEqual(D(attempted[0]['stopPrice']), D('74.18'))
            active = [row for row in account.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            self.assertEqual(D(active[0]['stopPrice']), D('74.18'))
            with State(directory, config.scope) as state:
                self.assertEqual(D(state.get('positions')['40']['peak']), D('103.04'))

    def test_final_h2_veto_can_retry_the_same_unsent_buy_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            account = venue_before_entry()
            run_day(config, account)
            add_day(account, '101')
            run(config, account, execute=False, monotonic=account.monotonic, wait=account.wait)
            add_day(account, '102')
            adapter = OrderAdapter(account, None)
            adapter.before_post_snapshot = lambda: setattr(account, 'price', D('100.1'))
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(account.sent, [])
            self.assertFalse(adapter.write_attempted)
            self.assertFalse(report['pending_intents'])
            with State(directory, config.scope) as state:
                original_id = state.db.execute('SELECT id FROM intents').fetchone()[0]
            account.price = D('102')
            report = run(config, adapter, execute=True, monotonic=adapter.monotonic, wait=adapter.wait)
            self.assertEqual(report['errors'], [])
            buys = [row for row in account.orders.values() if row['side'] == 'BUY']
            self.assertEqual(len(buys), 1)
            self.assertEqual(buys[0]['clientOrderId'], original_id)

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

    def test_bnb_fee_on_partial_sell_reduces_the_confirmed_remainder(self):
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
            self.assertLess(venue.btc * venue.price, D('5'))
            self.assertFalse(report['risk_state']['manual_takeover'])
            self.assertGreater(report['risk_state']['residual_btc'], 0)

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
            def crash(identity, **kwargs):
                cancel(identity, **kwargs)
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

    def test_unconfirmed_protection_is_replaced_on_the_next_session(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            add_day(venue, '101')
            venue.reject_stop = True
            self.assertEqual(run_day(config, venue)['status'], 'unknown')
            self.assertEqual(len([row for row in venue.orders.values() if row['type'] == 'MARKET']), 1)
            venue.reject_stop = False
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            stops = [row for row in venue.orders.values() if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW']
            self.assertEqual(len(stops), 1)
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
            def cancel(identity, **kwargs):
                original(identity, **kwargs)
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
            self.assertEqual(report['errors'], [])
            self.assertLess(report['risk_state']['unprotected_btc'] * D('85'), D('5'))
            self.assertFalse(report['manual_takeover'])
            self.assertGreater(report['risk_state']['residual_btc'], 0)

    def entered(self, directory):
        config = Config('1', directory, 1, 1, 'demo', '1000')
        venue = venue_before_entry()
        run_day(config, venue)
        add_day(venue, '101')
        run_day(config, venue)
        add_day(venue, '102')
        run_day(config, venue)
        return config, venue

    def test_crash_after_cancel_resumes_sale_and_reduces_partial_remainder(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self.entered(directory)
            add_day(venue, '97')
            venue.fraction = D('.5')
            cancel = venue.cancel
            def crash(identity, **kwargs):
                cancel(identity, **kwargs)
                raise KeyboardInterrupt
            venue.cancel = crash
            report = run_day(config, venue)
            self.assertEqual(report['stop_reason'], 'interrupted')
            venue.cancel = cancel
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            stops = [row for row in venue.orders.values() if row['status'] == 'NEW']
            self.assertEqual(stops, [])
            self.assertLess(venue.btc * venue.price, D('5'))
            self.assertFalse(report['risk_state']['manual_takeover'])
            self.assertGreater(report['risk_state']['residual_btc'], 0)
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
            # Actual observed 102 raises the peak; the daily 110 wick is unused.
            self.assertEqual(D(active[0]['stopPrice']), D('73.44'))
            add_day(venue, '103', '110')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            active = [row for row in venue.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            self.assertEqual(D(active[0]['stopPrice']), D('74.16'))
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
            def crash_after_cancel(identity, **kwargs):
                cancel(identity, **kwargs)
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
            def two_fills(identity, payload, **kwargs):
                if payload['side'] != 'BUY':
                    return original(identity, payload, **kwargs)
                venue.fraction = D('.5')
                row = original(identity, payload, **kwargs)
                venue.fraction = D(1)
                venue.now_ms += 1
                venue.price = D('120')
                quote = D(payload['quoteOrderQty']) - D(row['quote'])
                quantity = quote / venue.price
                venue.cash -= quote
                venue.btc += quantity * (1 - venue.fee)
                venue._fill(row, quantity, quote)
                row.update(status='FILLED', executedQty=str(D(row['executedQty']) + quantity),
                           quote=payload['quoteOrderQty'])
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
                restored = state.get('positions')['40']
                self.assertGreaterEqual(restored.pop('quote_through_ms'), position['quote_through_ms'])
                self.assertEqual(restored, {key: value for key, value in position.items() if key != 'quote_through_ms'})

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
            def submit(identity, payload, **kwargs):
                row = original(identity, payload, **kwargs)
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


class SafetyRepairTests(TestCase):
    def test_absent_replacement_stop_is_sent_again_with_the_same_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = ExecutionTests().entered(directory)
            add_day(venue, '103')
            original = venue.submit
            def lose_replacement(identity, payload, **kwargs):
                if payload['type'] == 'STOP_LOSS':
                    raise Unknown('replacement was not stored')
                return original(identity, payload, **kwargs)
            venue.submit = lose_replacement
            venue.price = D('150')
            failed = run_day(config, venue)
            self.assertEqual(failed['status'], 'unknown')
            self.assertFalse(any(row['status'] == 'NEW' for row in venue.orders.values()))
            venue.submit = original
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            active = [row for row in venue.orders.values() if row['status'] == 'NEW']
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]['type'], 'STOP_LOSS')

    def test_rejected_sell_keeps_a_stop_and_a_crossed_price_sells_once(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = ExecutionTests().entered(directory)
            original = venue.submit
            calls = {'sells': 0}
            def reject_touch(identity, payload, **kwargs):
                if payload.get('type') == 'MARKET' and payload.get('side') == 'SELL':
                    calls['sells'] += 1
                    raise Blocked('filter rejected sell')
                return original(identity, payload, **kwargs)
            venue.submit = reject_touch
            venue.price = D('100.4')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertGreater(venue.btc * venue.price, D('5'))
            self.assertEqual(calls['sells'], 1)
            self.assertTrue(any(row['type'] == 'STOP_LOSS' and row['status'] == 'NEW'
                                for row in venue.orders.values()))
        with tempfile.TemporaryDirectory() as directory:
            config, venue = ExecutionTests().entered(directory)
            original = venue.submit
            calls = {'sells': 0}
            def reject_once(identity, payload, **kwargs):
                if payload.get('type') == 'MARKET' and payload.get('side') == 'SELL':
                    calls['sells'] += 1
                    if calls['sells'] == 1:
                        raise Blocked('filter rejected sell')
                return original(identity, payload, **kwargs)
            venue.submit = reject_once
            venue.price = D('70')
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertLess(venue.btc * venue.price, D('5'))
            self.assertEqual(calls['sells'], 2)

    def test_fee_dust_under_a_full_stop_is_not_a_takeover(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = ExecutionTests().entered(directory)
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertFalse(report['manual_takeover'])
            self.assertFalse(report['risk_state']['manual_takeover'])
            self.assertGreater(report['risk_state']['residual_btc'], 0)
            self.assertLess(report['risk_state']['residual_btc'], D('0.00001'))
