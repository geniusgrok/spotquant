"""Offline Demo verification, reconciliation and mainnet refusal."""
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from spotquant.binance import Binance, normalize_order
from spotquant.cli import main
from spotquant.config import Config
from spotquant.demo_guard import (
    DEMO_ORIGIN, assert_demo_config, install_demo_guard, refuse_non_demo_url,
)
from spotquant.demo_verify import (
    demo_check_argv, dispatch_order, execute_verification, interrupt_child, judge_graceful,
    protection_price, select_scenarios, simulated_exit_decision, verification_identity,
)
from spotquant.execution import Lifecycle
from spotquant.model import Model
from spotquant.preview import _position_decision
from spotquant.reconcile import compare_books, execute_reconcile
from spotquant.state import State
from spotquant.types import Blocked, Unknown


SECRET = 'test-secret'
KEY = 'demo-key'


def _filters():
    return {
        'symbols': [{
            'symbol': 'BTCUSDT',
            'status': 'TRADING',
            'baseAsset': 'BTC',
            'quoteAsset': 'USDT',
            'isSpotTradingAllowed': True,
            'orderTypes': ['MARKET', 'STOP_LOSS', 'LIMIT'],
            'filters': [
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.01000000'},
                {'filterType': 'LOT_SIZE', 'stepSize': '0.00001000', 'minQty': '0.00001000'},
                {'filterType': 'NOTIONAL', 'minNotional': '5.00000000'},
            ],
        }],
    }


class SpotScript:
    """In-memory Demo host. Prices stay at 100 so the 28% stop is 72.00."""

    def __init__(self):
        self.now_ms = 1_700_000_000_000
        self.price = D('100')
        self.fee = D('0.001')
        self.usdt = D('100')
        self.btc_free = D('0')
        self.btc_locked = D('0')
        self.orders = {}
        self.trades = []
        self.posts = []
        self.next_id = 1
        self.next_trade = 1
        self.fail_post = False

    def clock(self):
        return self.now_ms / 1000

    def __call__(self, method, url, headers):
        if SECRET in url or SECRET in json.dumps(headers):
            raise AssertionError('secret leaked into the request')
        parts = urlsplit(url)
        if parts.hostname != 'demo-api.binance.com':
            raise AssertionError(parts.hostname)
        params = {key: values[0] for key, values in parse_qs(parts.query).items()}
        if 'signature' in params and abs(int(params['timestamp']) - self.now_ms) > 5000:
            return 400, json.dumps({'code': -1021, 'msg': 'Timestamp outside recvWindow'}).encode()
        path = parts.path
        if path == '/api/v3/time':
            return 200, json.dumps({'serverTime': self.now_ms}).encode()
        if path == '/api/v3/exchangeInfo':
            return 200, json.dumps(_filters()).encode()
        if path == '/api/v3/avgPrice':
            return 200, json.dumps({'mins': 5, 'price': '100.00'}).encode()
        if path == '/api/v3/ticker/price':
            return 200, json.dumps({'symbol': 'BTCUSDT', 'price': '100.00'}).encode()
        if path == '/api/v3/account':
            return 200, json.dumps(self._account()).encode()
        if path == '/api/v3/account/commission':
            return 200, json.dumps({
                'symbol': 'BTCUSDT',
                'standardCommission': {'taker': '0.001', 'buyer': '0'},
                'specialCommission': {'taker': '0', 'buyer': '0'},
                'taxCommission': {'taker': '0', 'buyer': '0'},
                'discount': {'enabledForAccount': False, 'enabledForSymbol': False},
            }).encode()
        if path == '/api/v3/openOrders':
            return 200, json.dumps([row for row in self.orders.values() if row['status'] == 'NEW']).encode()
        if path == '/api/v3/myTrades':
            start = int(params.get('startTime', '0'))
            return 200, json.dumps([row for row in self.trades if row['time'] >= start]).encode()
        if path == '/api/v3/allOrders':
            start = int(params['startTime']) if 'startTime' in params else 0
            floor = int(params['orderId']) if 'orderId' in params else 0
            rows = [row for row in self.orders.values()
                    if row['time'] >= start and row['orderId'] >= floor]
            rows.sort(key=lambda row: row['orderId'])
            return 200, json.dumps(rows).encode()
        if path != '/api/v3/order':
            raise AssertionError(path)
        if method == 'POST':
            return self._post(params)
        if method == 'DELETE':
            return self._delete(params)
        found = self._find(params)
        if found is None:
            return 400, b'{"code":-2013,"msg":"Order does not exist."}'
        return 200, json.dumps(found).encode()

    def _account(self):
        return {
            'uid': '10001',
            'canTrade': True,
            'balances': [
                {'asset': 'BTC', 'free': format(self.btc_free, 'f'), 'locked': format(self.btc_locked, 'f')},
                {'asset': 'USDT', 'free': format(self.usdt, 'f'), 'locked': '0'},
            ],
        }

    def _find(self, params):
        if 'orderId' in params:
            return next((row for row in self.orders.values() if row['orderId'] == int(params['orderId'])), None)
        client = params.get('origClientOrderId')
        return next((row for row in self.orders.values()
                     if row['clientOrderId'] == client or row.get('origClientOrderId') == client), None)

    def _stamp(self):
        stamp = self.now_ms
        self.now_ms += 1000
        return stamp

    def _post(self, params):
        identity = params['newClientOrderId']
        self.posts.append(identity)
        if self.fail_post:
            raise Unknown('lost before a usable response')
        if any(row['clientOrderId'] == identity or row.get('origClientOrderId') == identity
               for row in self.orders.values()):
            return 400, b'{"code":-2010,"msg":"Duplicate order sent."}'
        stamp = self._stamp()
        order_id = self.next_id
        self.next_id += 1
        side = params['side']
        order_type = params['type']
        if order_type == 'STOP_LOSS':
            qty = D(params['quantity'])
            if qty > self.btc_free:
                return 400, b'{"code":-2010,"msg":"insufficient balance"}'
            self.btc_free -= qty
            self.btc_locked += qty
            row = self._row(order_id, identity, side, order_type, qty, D('0'), '0', 'NEW', stamp,
                            stop=params['stopPrice'], quote_order=None)
        else:
            buy = side == 'BUY'
            if buy:
                quote = D(params['quoteOrderQty'])
                gross = quote / self.price
                self.usdt -= quote
                self.btc_free += gross - gross * self.fee
                self._trade(order_id, stamp, gross, quote, True, gross * self.fee, 'BTC')
                row = self._row(order_id, identity, side, order_type, gross, gross, format(quote, 'f'),
                                'FILLED', stamp, stop=None, quote_order=params['quoteOrderQty'])
            else:
                qty = D(params['quantity'])
                if qty > self.btc_free:
                    return 400, b'{"code":-2010,"msg":"insufficient balance"}'
                quote = qty * self.price
                self.btc_free -= qty
                self.usdt += quote - quote * self.fee
                self._trade(order_id, stamp, qty, quote, False, quote * self.fee, 'USDT')
                row = self._row(order_id, identity, side, order_type, qty, qty, format(quote, 'f'),
                                'FILLED', stamp, stop=None, quote_order=None)
        self.orders[order_id] = row
        return 200, json.dumps(row).encode()

    def _delete(self, params):
        row = self._find(params)
        if row is None:
            return 400, b'{"code":-2011,"msg":"Unknown order sent."}'
        if row['status'] == 'NEW':
            qty = D(row['origQty']) - D(row['executedQty'])
            self.btc_locked -= qty
            self.btc_free += qty
            row['status'] = 'CANCELED'
            row['origClientOrderId'] = row['clientOrderId']
            row['clientOrderId'] = params['newClientOrderId']
            row['updateTime'] = self._stamp()
        return 200, json.dumps(row).encode()

    def _trade(self, order_id, stamp, qty, quote, buyer, commission, asset):
        self.trades.append({
            'symbol': 'BTCUSDT',
            'id': self.next_trade,
            'orderId': order_id,
            'time': stamp,
            'qty': format(qty, 'f'),
            'quoteQty': format(quote, 'f'),
            'price': format(quote / qty, 'f'),
            'isBuyer': buyer,
            'commission': format(commission, 'f'),
            'commissionAsset': asset,
        })
        self.next_trade += 1

    @staticmethod
    def _row(order_id, identity, side, order_type, original, executed, quote, status, stamp, *,
             stop, quote_order):
        return {
            'symbol': 'BTCUSDT',
            'orderId': order_id,
            'clientOrderId': identity,
            'side': side,
            'type': order_type,
            'status': status,
            'origQty': format(original, 'f'),
            'executedQty': format(executed, 'f'),
            'cummulativeQuoteQty': quote,
            'origQuoteOrderQty': quote_order,
            'stopPrice': stop or '0.00000000',
            'time': stamp,
            'updateTime': stamp,
            'transactTime': stamp,
        }


def _config(directory):
    return Config('10001', directory, environment='demo', capital_limit_usdt='100')


def _venue(script):
    venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=script, clock=script.clock,
                    capital_limit=D('100'), demo_execution_uid='10001')
    install_demo_guard(venue)
    return venue


class GuardTests(unittest.TestCase):
    def test_live_config_and_host_are_refused_before_the_network(self):
        with self.assertRaisesRegex(Blocked, 'environment is demo'):
            assert_demo_config(Config('10001', '/tmp/unused', environment='live', capital_limit_usdt='100'),
                               capital=True)
        with self.assertRaisesRegex(Blocked, 'at most 100'):
            assert_demo_config(Config('10001', '/tmp/unused', environment='demo', capital_limit_usdt='100.01'),
                               capital=True)
        assert_demo_config(Config('10001', '/tmp/unused', environment='demo', capital_limit_usdt='100'),
                           capital=True)
        refuse_non_demo_url(DEMO_ORIGIN + '/api/v3/order?symbol=BTCUSDT')
        for host in ('https://api.binance.com/api', 'https://api1.binance.com/api',
                     'https://testnet.binance.vision/api', 'http://demo-api.binance.com/api',
                     'https://user:pw@demo-api.binance.com/api'):
            with self.assertRaisesRegex(Blocked, 'only sends to'):
                refuse_non_demo_url(host)
        called = []
        venue = Binance(key=KEY, secret=SECRET, environment='demo',
                        opener=lambda *args: called.append(args), capital_limit=D('100'),
                        demo_execution_uid='10001')
        venue.base = 'https://api.binance.com'
        with self.assertRaisesRegex(Blocked, 'only sends to'):
            install_demo_guard(venue)
        self.assertEqual(called, [])

    def test_cli_rejects_live_and_missing_authorization_before_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': '10001', 'state_dir': directory,
                'environment': 'live', 'capital_limit_usdt': '100',
            }))
            with unittest.mock.patch('spotquant.cli.connect', side_effect=AssertionError('connect')):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(['demo-verify', '--config', str(path), '--execute', '--authorize-uid', '10001'])
            self.assertEqual(code, 2)
            self.assertIn('environment is demo', json.loads(stdout.getvalue())['reason'])
            path.write_text(json.dumps({
                'account_uid': '10001', 'state_dir': directory,
                'environment': 'demo', 'capital_limit_usdt': '100',
            }))
            with unittest.mock.patch('spotquant.cli.connect', side_effect=AssertionError('connect')):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(['demo-verify', '--config', str(path), '--execute'])
            self.assertEqual(code, 2)
            self.assertIn('matching --authorize-uid', json.loads(stdout.getvalue())['reason'])
            path.write_text(json.dumps({
                'account_uid': '10001', 'state_dir': directory, 'environment': 'live',
            }))
            with unittest.mock.patch('spotquant.cli.connect', side_effect=AssertionError('connect')):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(['demo-reconcile', '--config', str(path)])
            self.assertEqual(code, 2)
            self.assertIn('environment is demo', json.loads(stdout.getvalue())['reason'])

    def test_fault_names_require_the_flag(self):
        self.assertNotIn('kill-restart', select_scenarios(None, False))
        self.assertIn('kill-restart', select_scenarios(None, True))
        with self.assertRaisesRegex(Blocked, '--faults'):
            select_scenarios(['kill-restart'], False)


class DecisionTests(unittest.TestCase):
    def test_stop_uses_the_fill_unless_a_later_quote_is_higher(self):
        self.assertEqual(protection_price('100', '150', quote_after_fill=False), D('72'))
        self.assertEqual(protection_price('100', '150', quote_after_fill=True), D('108'))
        self.assertEqual(protection_price('100.03', '100.03', quote_after_fill=True), D('72.02'))
        self.assertEqual(Model(40).trail, D('0.28'))

    def test_forced_exits_use_the_real_rules_and_are_flagged(self):
        snapshot = {'last_price': D('100'), 'avg_price': D('100'), 'min_notional': D('5')}
        adverse, evidence = simulated_exit_decision('adverse', D('0.1'), snapshot)
        self.assertEqual(adverse['action'], 'exit')
        self.assertIn('4%', adverse['reason'])
        self.assertTrue(evidence['simulated'])
        self.assertTrue(evidence['adverse'])
        touch, evidence = simulated_exit_decision('sma', D('0.1'), snapshot)
        self.assertEqual(touch['action'], 'exit')
        self.assertTrue(touch['rearm'])
        self.assertIn('0.5%', touch['reason'])
        self.assertTrue(evidence['simulated'])
        held = Model(40)
        held.shadow_in = True
        held.bull = True
        held.sma = D('100')
        held.entry = D('100')
        held.position_peak = D('100')
        held.close = D('100')
        quiet = dict(snapshot, last_price=D('120'))
        self.assertEqual(_position_decision(held, quiet, D('0.1'))['action'], 'hold')


class DispatchTests(unittest.TestCase):
    def test_unknown_buy_is_not_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            script = SpotScript()
            script.fail_post = True
            venue = _venue(script)
            config = _config(directory)
            with State(directory, config.scope) as state:
                venue.bind_state(state)
                lifecycle = Lifecycle(state, venue, config)
                identity = verification_identity(config.scope, 1, 'unknown-buy')
                order = {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': '15.00'}
                with self.assertRaisesRegex(Unknown, 'not resent'):
                    dispatch_order(lifecycle, identity, order, signal_ms=1)
                self.assertEqual(script.posts, [identity])
                with self.assertRaisesRegex(Unknown, 'not resent'):
                    dispatch_order(lifecycle, identity, order, signal_ms=1)
                self.assertEqual(script.posts, [identity])
                row = next(item for item in lifecycle.rows() if item[0] == identity)
                self.assertEqual(row[2], 'unknown')
                self.assertNotIn('orderId', row[3])


class WalkTests(unittest.TestCase):
    def test_order_walk_matches_the_exchange_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            script = SpotScript()
            venue = _venue(script)
            config = _config(directory)
            report = execute_verification(
                config, venue, execute=True, scenarios=[
                    'market-buy', 'stop-place', 'stop-replace', 'adverse-exit', 'sma-exit',
                ])
            self.assertEqual(report['status'], 'pass', report)
            self.assertIs(report['native_execution_verified'], False)
            by_name = {row['scenario']: row for row in report['scenarios']}
            self.assertEqual(by_name['market-buy']['fill_price'], '100')
            self.assertFalse(by_name['market-buy']['resent'])
            self.assertEqual(by_name['stop-place']['stop_price'], '72.00')
            self.assertEqual(by_name['stop-place']['exchange_stop_price'], '72.00')
            self.assertTrue(by_name['stop-replace']['forced_rereplace'])
            self.assertGreaterEqual(by_name['stop-replace']['exchange_unprotected_window_ms'], 1000)
            self.assertGreaterEqual(by_name['stop-replace']['unprotected_window_ms'], 0)
            self.assertTrue(by_name['adverse-exit']['simulated_trigger'])
            self.assertIn('4%', by_name['adverse-exit']['decision_reason'])
            self.assertTrue(by_name['sma-exit']['simulated_trigger'])
            self.assertIn('0.5%', by_name['sma-exit']['decision_reason'])
            self.assertEqual(len(script.posts), len(set(script.posts)))
            self.assertEqual(script.btc_free, D('0'))
            self.assertEqual(script.btc_locked, D('0'))
            text = Path(report['results_dir'], 'demo-verification.json').read_text()
            self.assertNotIn(SECRET, text)
            self.assertNotIn(KEY, text)
            self.assertIn('native_execution_verified', text)
            reconciled = execute_reconcile(config, venue)
            self.assertTrue(reconciled['passed'], reconciled['mismatches'])
            self.assertIn('通过', Path(reconciled['results_dir'], 'reconcile.md').read_text())


class FaultTests(unittest.TestCase):
    def _run(self, directory, script, name):
        venue = _venue(script)
        return execute_verification(
            _config(directory), venue, execute=True, faults=True, scenarios=[name]), venue

    def test_clock_skew_retries_one_get(self):
        with tempfile.TemporaryDirectory() as directory:
            report, _ = self._run(directory, SpotScript(), 'clock-skew')
            row = report['scenarios'][0]
            self.assertEqual(row['status'], 'pass', row)
            self.assertEqual(row['codes'].count(-1021), 1)
            self.assertFalse(row['resent_post'])
            self.assertTrue(row['forced_clock_offset'])

    def test_rate_limit_blocks_until_retry_after(self):
        now = {'mono': 1_000.0, 'wall': 1_700_000_000.0}

        def monotonic():
            return now['mono']

        def sleep(seconds):
            now['mono'] += seconds
            now['wall'] += seconds

        script = SpotScript()
        script.clock = lambda: now['wall']
        with tempfile.TemporaryDirectory() as directory:
            venue = _venue(script)
            venue.clock = script.clock
            report = execute_verification(
                _config(directory), venue, execute=True, faults=True, scenarios=['rate-limit'],
                sleep=sleep, monotonic=monotonic)
            row = report['scenarios'][0]
            self.assertEqual(row['status'], 'pass', row)
            self.assertEqual(row['requests_during_backoff'], 0)
            self.assertTrue(row['simulated_transport'])
            self.assertIn('demo-api.binance.com', row['persisted_hosts'])

    def test_lost_response_is_queried_once(self):
        with tempfile.TemporaryDirectory() as directory:
            script = SpotScript()
            report, _ = self._run(directory, script, 'network-loss')
            row = report['scenarios'][0]
            self.assertEqual(row['status'], 'pass', row)
            self.assertEqual(row['post_count'], 1)
            self.assertTrue(row['response_lost'])
            self.assertTrue(row['recovered_by_query'])
            self.assertFalse(row['resent'])
            self.assertEqual(script.btc_free, D('0'))
            self.assertEqual(len([item for item in script.posts if item == row['client_id']]), 1)

    def test_sigkill_reopens_sqlite_without_a_second_order(self):
        with tempfile.TemporaryDirectory() as directory:
            script = SpotScript()
            report, venue = self._run(directory, script, 'kill-restart')
            row = report['scenarios'][0]
            self.assertEqual(row['status'], 'pass', row)
            self.assertTrue(row['still_unknown'])
            self.assertTrue(row['intent_ids_unchanged'])
            self.assertEqual(script.posts, [])
            reconciled = execute_reconcile(_config(directory), venue)
            self.assertFalse(reconciled['passed'])
            self.assertTrue(any(item['kind'] == 'unconfirmed_local_order' for item in reconciled['mismatches']))


class ReconcileTests(unittest.TestCase):
    def test_compare_reports_quantity_fill_and_secret_mismatches(self):
        local_orders = [{
            'id': 'sq-buy', 'status': 'settled', 'cancel_id': None, 'order_id': 7, 'updated_ms': 10,
            'order': {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': '15.00'},
            'result': {'orderId': 7, 'status': 'FILLED', 'executedQty': '0.15'},
        }, {
            'id': 'sq-prepared', 'status': 'prepared', 'cancel_id': None, 'order_id': None, 'updated_ms': 10,
            'order': {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': '15.00'},
            'result': {'not_sent': True},
        }]
        exchange = [normalize_order({
            'symbol': 'BTCUSDT', 'orderId': 7, 'clientOrderId': 'sq-buy', 'side': 'BUY', 'type': 'MARKET',
            'status': 'FILLED', 'origQty': '0.15', 'executedQty': '0.15', 'origQuoteOrderQty': '15.00',
            'cummulativeQuoteQty': '15', 'stopPrice': '0', 'time': 1_000, 'updateTime': 1_000,
        })]
        fills = [{'id': 1, 'order_id': 7, 'time': 1_000, 'qty': '0.15', 'quote': '15', 'price': '100',
                  'buyer': True, 'commission': '0.00015', 'commission_asset': 'BTC'}]
        trades = [dict(fills[0], qty=D('0.15'), quote=D('15'), price=D('100'), commission=D('0.00015'))]
        latest = {'environment': 'demo', 'account_uid': '10001'}
        report = compare_books(local_orders, fills, exchange, trades, latest, account_uid='10001')
        self.assertTrue(report['passed'], report['mismatches'])
        self.assertIs(report['native_execution_verified'], False)
        exchange[0]['executedQty'] = '0.10'
        report = compare_books(local_orders, fills, exchange, trades, latest, account_uid='10001')
        self.assertFalse(report['passed'])
        self.assertTrue(any(item['kind'] == 'executed_quantity' for item in report['mismatches']))
        exchange[0]['executedQty'] = '0.15'
        leaked = {'environment': 'live', 'account_uid': '10001', 'secret': 'nope'}
        report = compare_books(local_orders, fills, exchange, [], leaked, account_uid='10001')
        kinds = {item['kind'] for item in report['mismatches']}
        self.assertIn('latest_environment', kinds)
        self.assertIn('latest_secret', kinds)
        self.assertIn('missing_exchange_fill', kinds)
        unknown = dict(local_orders[1], id='sq-unknown', status='unknown', result={})
        report = compare_books([unknown], [], [], [], latest, account_uid='10001')
        self.assertEqual([item['kind'] for item in report['mismatches']], ['unconfirmed_local_order'])

    def test_history_pages_and_rejects_a_rewind(self):
        def row(order_id):
            return {
                'symbol': 'BTCUSDT', 'orderId': order_id, 'clientOrderId': f'sq-{order_id}',
                'side': 'BUY', 'type': 'MARKET', 'status': 'FILLED', 'origQty': '0.1',
                'executedQty': '0.1', 'time': 1_700_000_000_000, 'updateTime': 1_700_000_000_000,
            }

        def venue(pages):
            calls = {'n': 0}

            def opener(method, url, headers):
                if '/api/v3/time' in url:
                    return 200, b'{"serverTime":1700000000000}'
                self.assertEqual(method, 'GET')
                calls['n'] += 1
                params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
                if calls['n'] > 1:
                    self.assertEqual(params.get('orderId'), '1001')
                return 200, json.dumps(pages[calls['n'] - 1]).encode()

            client = Binance(key=KEY, secret=SECRET, environment='demo', opener=opener,
                             clock=lambda: 1_700_000_000)
            return client

        found = venue([[row(i) for i in range(1, 1001)], [row(1001)]]).all_orders(1_700_000_000_000)
        self.assertEqual([item['orderId'] for item in found], list(range(1, 1002)))
        with self.assertRaisesRegex(Unknown, 'precedes'):
            venue([[row(i) for i in range(1, 1001)], [row(1000)]]).all_orders(1_700_000_000_000)


class StopSignalTests(unittest.TestCase):
    def test_sigint_reaches_a_child_and_graceful_judge_reads_the_report(self):
        argv = demo_check_argv('demo-verify.json', '10001')
        self.assertEqual(argv[1:5], ['-m', 'spotquant', 'demo-check', '--config'])
        self.assertIn('--execute', argv)
        self.assertIn('10001', argv)
        outcome = interrupt_child(
            [sys.executable, '-c', 'import time\ntry:\n time.sleep(30)\nexcept KeyboardInterrupt:\n raise SystemExit(1)\n'],
            interrupt_after=0.2, timeout=5)
        self.assertFalse(outcome['timed_out'])
        self.assertNotEqual(outcome['returncode'], 0)
        self.assertEqual(judge_graceful({'stop_reason': 'interrupted', 'closeout_attempted': True},
                                        report_updated=True)['status'], 'pass')
        self.assertEqual(judge_graceful({'stop_reason': 'deadline'}, report_updated=True)['status'], 'fail')
        self.assertEqual(judge_graceful({}, report_updated=False)['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
