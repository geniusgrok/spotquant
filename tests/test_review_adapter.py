"""Offline transport ambiguity, backoff and live authorization regressions."""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spotquant.binance import Binance, _order
from spotquant.cli import connect, main
from spotquant.config import Config
from spotquant.types import Blocked, NotFound, NotSent, Unknown
from test_binance import KEY, SECRET, Script


class AdapterReviewTests(unittest.TestCase):
    def test_unclassified_write_errors_remain_unknown(self):
        for code in (-1000, -1001, -1006, -1007, -1008, -9999, None):
            with self.subTest(code=code):
                venue = Binance(key=KEY, secret=SECRET, environment='live', capital_limit=D(100),
                                demo_execution_uid='10001',
                                opener=lambda *args: (400, json.dumps({'code': code}).encode()))
                venue._offset_ms = 0
                with self.assertRaises(Unknown) as caught:
                    venue._get('/api/v3/order', {}, signed=True, method='POST')
                self.assertNotIsInstance(caught.exception, NotSent)
                self.assertTrue(venue.write_attempted)

    def test_rate_limit_blocks_followup_reads_and_writes_even_without_json(self):
        for status in (418, 429):
            with self.subTest(status=status):
                calls = []
                now = [10.0]
                venue = Binance(key=KEY, secret=SECRET, environment='live', capital_limit=D(100),
                                demo_execution_uid='10001')
                venue._monotonic = lambda: now[0]
                venue._opener = lambda *args: calls.append(args) or (status, b'not JSON', {'Retry-After': '30'})
                with self.assertRaisesRegex(Unknown, 'rate limit'):
                    venue._get('/api/v3/exchangeInfo', signed=False)
                with self.assertRaisesRegex(Unknown, 'backoff'):
                    venue.snapshot('10001')
                with self.assertRaises(NotSent):
                    venue.cancel('sq-stop', order_id=1, cancel_id='sq-cancel')
                self.assertEqual(len(calls), 1)
                self.assertFalse(venue.write_attempted)
                now[0] = 40.0
                venue._opener = lambda *args: calls.append(args) or (200, b'{}')
                self.assertEqual(venue._get('/api/v3/exchangeInfo', signed=False), {})
                self.assertEqual(len(calls), 2)

    def test_missing_order_code_on_other_routes_or_server_error_is_not_absence(self):
        for path, status in (('/api/v3/myTrades', 400), ('/api/v3/order', 503)):
            with self.subTest(path=path, status=status):
                venue = Binance(key=KEY, secret=SECRET, environment='live',
                                opener=lambda *args: (status, b'{"code":-2013}'))
                venue._offset_ms = 0
                with self.assertRaises(Unknown) as caught:
                    venue._get(path, signed=True)
                self.assertNotIsInstance(caught.exception, NotFound)

    def test_duplicate_order_refusal_preserves_unknown_original_identity(self):
        venue = Binance(key=KEY, secret=SECRET, environment='live', capital_limit=D(100),
                        demo_execution_uid='10001',
                        opener=lambda *args: (400, b'{"code":-2010,"msg":"Duplicate order sent."}'))
        venue._offset_ms = 0
        with self.assertRaises(Unknown):
            venue._get('/api/v3/order', {}, signed=True, method='POST')
        venue._opener = lambda *args: (400, b'{"code":-2010,"msg":"Account has insufficient balance."}')
        with self.assertRaises(Blocked):
            venue._get('/api/v3/order', {}, signed=True, method='POST')

    def test_trade_pages_reject_mutated_duplicate_and_below_cursor(self):
        fill = dict(id=10, orderId=20, time=1700000000000, qty='1', quoteQty='100',
                    price='100', commission='0', commissionAsset='BTC', isBuyer=True)
        venue = Binance(key=KEY, secret=SECRET, environment='live')
        venue._offset_ms = 0
        for rows, message in (([fill, dict(fill, qty='2')], 'conflicting'),
                              ([dict(fill, id=9)], 'precedes')):
            with self.subTest(message=message):
                venue._opener = lambda *args: (200, json.dumps(rows).encode())
                with self.assertRaisesRegex(Unknown, message):
                    venue.trades(fill['time'], from_id=10)
        venue._opener = lambda *args: (200, json.dumps([fill, fill]).encode())
        self.assertEqual(len(venue.trades(fill['time'], from_id=10)), 1)

    def test_open_order_id_and_execution_quantity_cannot_be_lossily_coerced(self):
        order = dict(orderId=1, clientOrderId='sq-stop', side='SELL', type='STOP_LOSS',
                     status='NEW', origQty='1', executedQty='0', stopPrice='72')
        for field, value in (('orderId', True), ('orderId', 1.5), ('orderId', '1'),
                             ('clientOrderId', None), ('executedQty', '1.1'), ('symbol', 'ETHUSDT')):
            with self.subTest(field=field, value=value), self.assertRaises(Unknown):
                _order(dict(order, **{field: value}))
        self.assertEqual(_order(order)['order_id'], 1)

    def test_live_gate_checks_every_prerequisite_before_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for environment, limit, uid in (
                    ('live', None, '10001'), ('live', '0', '10001'),
                    ('demo', '100', '10001'), ('live', '100', '999')):
                with self.subTest(environment=environment, limit=limit, uid=uid):
                    path.write_text(json.dumps(dict(account_uid='10001', state_dir=directory,
                                                   environment=environment, capital_limit_usdt=limit)))
                    with patch('spotquant.cli.connect') as connecting, \
                            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        code = main(['run', '--execute', '--authorize-uid', uid, '--config', str(path)])
                    self.assertEqual(code, 2)
                    connecting.assert_not_called()
            path.write_text(json.dumps(dict(account_uid='10001', state_dir=directory,
                                            environment='live', capital_limit_usdt='100')))
            with patch('spotquant.cli.connect', return_value=object()) as connecting, \
                    patch('spotquant.cli.run', return_value={'status': 'live_execution'}) as running, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(['run', '--execute', '--authorize-uid', '10001', '--config', str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(connecting.call_args.kwargs, {'execute_orders': True})
            self.assertEqual(running.call_args.kwargs, {'execute': True})

    def test_live_and_demo_credentials_never_fall_back_to_each_other(self):
        for environment, other in (('live', 'DEMO_'), ('demo', '')):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                config = Config('10001', directory, environment=environment, capital_limit_usdt='100')
                with patch.dict('os.environ', {
                        f'SPOTQUANT_BINANCE_{other}KEY': KEY,
                        f'SPOTQUANT_BINANCE_{other}SECRET': SECRET}, clear=True):
                    with self.assertRaises(Blocked):
                        connect(config, execute_orders=True)

    def test_live_uid_mismatch_never_reaches_write_transport(self):
        script = Script(uid='999')
        venue = Binance(key=KEY, secret=SECRET, environment='live', capital_limit=D(100),
                        demo_execution_uid='10001', opener=script)
        with self.assertRaises(NotSent):
            venue.submit('sq-live-stop', dict(symbol='BTCUSDT', side='SELL', type='STOP_LOSS',
                                             quantity='.1', stopPrice='90'))
        self.assertTrue(script.methods)
        self.assertTrue(all(method == 'GET' for method in script.methods))
        self.assertFalse(venue.write_attempted)


if __name__ == '__main__':
    unittest.main()
