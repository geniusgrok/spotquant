"""Signed adapter, source history and native rejection checks use an offline opener."""
import hashlib
import hmac
import json
from pathlib import Path
from decimal import Decimal as D
import unittest

from spotquant.binance import Binance, ORIGIN, DAY
from spotquant.preview import _qty_ok
from spotquant.types import Blocked, Unknown


SECRET = 'test-secret'
KEY = 'test-key'


def _filters():
    return {
        'symbols': [{
            'symbol': 'BTCUSDT',
            'status': 'TRADING',
            'baseAsset': 'BTC',
            'quoteAsset': 'USDT',
            'isSpotTradingAllowed': True,
            'orderTypes': ['LIMIT', 'MARKET', 'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'],
            'filters': [
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.01000000'},
                {'filterType': 'LOT_SIZE', 'stepSize': '0.00001000', 'minQty': '0.00001000'},
                {'filterType': 'NOTIONAL', 'minNotional': '5.00000000'},
            ],
        }],
    }


class Script:
    def __init__(self, uid='10001'):
        self.urls = []
        self.methods = []
        self.uid = uid
        self.now = 1_700_000_000_000

    def __call__(self, method, url, headers):
        self.methods.append(method)
        self.urls.append(url)
        self.headers = headers
        if SECRET in url or SECRET in json.dumps(headers):
            raise AssertionError('secret leaked into the request')
        if method != 'GET':
            raise AssertionError(method)
        if '/api/v3/time' in url:
            return 200, json.dumps({'serverTime': self.now}).encode()
        if '/api/v3/exchangeInfo' in url:
            body = _filters()
            return 200, json.dumps(body).encode()
        if '/api/v3/account' in url:
            return 200, json.dumps({'uid': self.uid, 'canTrade': True, 'balances': [
                {'asset': 'BTC', 'free': '0.00000000', 'locked': '0.00000000'},
                {'asset': 'USDT', 'free': '25.50', 'locked': '1.00'},
            ]}).encode()
        if '/api/v3/openOrders' in url:
            return 200, b'[]'
        if '/api/v3/klines' in url:
            row = [ORIGIN, '100', '110', '90', '105', '1', ORIGIN + DAY - 1, '1000']
            return 200, json.dumps([row]).encode()
        if '/api/v3/avgPrice' in url:
            return 200, json.dumps({'mins': 5, 'price': '100.00'}).encode()
        if '/api/v3/ticker/price' in url:
            return 200, json.dumps({'symbol': 'BTCUSDT', 'price': '100.00'}).encode()
        if '/api/v3/myTrades' in url:
            return 200, b'[]'
        raise AssertionError(url)


class BinanceTests(unittest.TestCase):
    def test_official_btcusdt_market_zero_filters_allow_snapshot(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' /
                              'btcusdt_exchange_info_20261006.json').read_text())
        script = Script()
        def opener(method, url, headers):
            if '/api/v3/exchangeInfo' in url:
                return 200, json.dumps(fixture).encode()
            return script(method, url, headers)
        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener,
                        clock=lambda: 1_700_000_000)
        observed = venue.snapshot('10001')
        self.assertEqual(observed['min_qty'], D('.00001'))
        self.assertIsNone(observed['market_min_qty'])
        self.assertIsNone(observed['market_step'])
        self.assertEqual(observed['market_max_qty'], D('130.55122112'))
        self.assertTrue(_qty_ok('.00001', observed))
        self.assertFalse(_qty_ok('.000001', observed))

        market = next(item for item in fixture['symbols'][0]['filters']
                      if item['filterType'] == 'MARKET_LOT_SIZE')
        market.update(minQty='0.00004000', maxQty='0.00006000', stepSize='0.00002000')
        limited = venue.snapshot('10001')
        self.assertFalse(_qty_ok('.00003', limited))
        self.assertTrue(_qty_ok('.00004', limited))
        self.assertFalse(_qty_ok('.00005', limited))
        self.assertFalse(_qty_ok('.00008', limited))
        market['stepSize'] = '-0.00001000'
        with self.assertRaises(Blocked):
            venue.snapshot('10001')

    def test_fee_endpoint_failure_only_gates_new_buy(self):
        script = Script()
        writes = []
        def opener(method, url, headers):
            if '/api/v3/account/commission' in url:
                return 503, b'{"code":-1000,"msg":"unavailable"}'
            if method == 'POST':
                writes.append(url)
                return 200, b'{"orderId":1,"clientOrderId":"sq-stop","status":"NEW"}'
            return script(method, url, headers)
        venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=opener,
                        clock=lambda: 1_700_000_000, capital_limit=D('100'),
                        demo_execution_uid='10001')
        snapshot = venue.snapshot('10001')
        self.assertEqual(snapshot['fee_status'], 'unavailable')
        self.assertEqual(snapshot['fee_mode'], 'unknown')
        with self.assertRaisesRegex(Blocked, 'fee mode'):
            venue.submit('sq-buy', dict(symbol='BTCUSDT', side='BUY', type='MARKET', quoteOrderQty='10'))
        venue.submit('sq-stop', dict(symbol='BTCUSDT', side='SELL', type='STOP_LOSS',
                                     quantity='.1', stopPrice='90'))
        self.assertEqual(len(writes), 1)

    def test_native_rejection_is_distinct_from_unknown_write(self):
        for status, code, expected in ((400, -1013, Blocked), (504, -1007, Unknown),
                                       (503, -1000, Unknown)):
            script = Script()
            def opener(method, url, headers):
                if method == 'POST':
                    return status, json.dumps({'code': code, 'msg': 'failed'}).encode()
                return script(method, url, headers)
            venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=opener,
                            clock=lambda: 1_700_000_000, capital_limit=D('100'),
                            demo_execution_uid='10001')
            with self.assertRaises(expected):
                venue._get('/api/v3/order', {'symbol': 'BTCUSDT'}, signed=True, method='POST')

    def test_bnb_discount_blocks_buy_before_order_transport(self):
        script = Script()
        writes = []
        def opener(method, url, headers):
            if method != 'GET':
                writes.append(url)
                raise AssertionError('buy reached order transport')
            if '/api/v3/account/commission' in url:
                return 200, json.dumps({
                    'symbol': 'BTCUSDT',
                    'standardCommission': {'taker': '.001', 'buyer': '0'},
                    'specialCommission': {'taker': '0', 'buyer': '0'},
                    'taxCommission': {'taker': '0', 'buyer': '0'},
                    'discount': {'enabledForAccount': True, 'enabledForSymbol': True,
                                 'discountAsset': 'BNB'},
                }).encode()
            if '/api/v3/account?' in url:
                return 200, json.dumps({'uid': '10001', 'canTrade': True, 'balances': [
                    {'asset': 'BTC', 'free': '0', 'locked': '0'},
                    {'asset': 'USDT', 'free': '100', 'locked': '0'},
                    {'asset': 'BNB', 'free': '1', 'locked': '0'},
                ]}).encode()
            return script(method, url, headers)
        venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=opener,
                        clock=lambda: 1_700_000_000, capital_limit=D('100'),
                        demo_execution_uid='10001')
        with self.assertRaisesRegex(Blocked, 'fee mode'):
            venue.submit('sq-test', dict(symbol='BTCUSDT', side='BUY', type='MARKET', quoteOrderQty='10'))
        self.assertEqual(writes, [])

    def test_snapshot_signs_gets_and_checks_uid(self):
        script = Script()
        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=script, clock=lambda: script.now / 1000)
        # clock is seconds; serverTime is ms. Keep them aligned.
        snapshot = venue.snapshot('10001')
        self.assertEqual(snapshot['usdt_free'], D('25.50'))
        self.assertEqual(snapshot['usdt_locked'], D('1.00'))
        self.assertEqual(snapshot['open_orders'], 0)
        self.assertTrue(all(method == 'GET' for method in script.methods))
        signed = [url for url in script.urls if 'signature=' in url]
        self.assertTrue(signed)
        account = next(url for url in signed if '/api/v3/account?' in url)
        query = account.split('?', 1)[1].rsplit('&signature=', 1)[0]
        expected = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        self.assertIn('signature=' + expected, account)
        self.assertTrue(account.startswith('https://api.binance.com/'))
        script.uid = '999'
        with self.assertRaises(Blocked):
            venue.snapshot('10001')

    def test_a_missing_balance_list_is_not_an_empty_account(self):
        script = Script()

        def opener(method, url, headers):
            if '/api/v3/account' in url:
                return 200, json.dumps({'uid': '10001'}).encode()
            return script(method, url, headers)

        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener, clock=lambda: 1_700_000_000)
        with self.assertRaises(Unknown):
            venue.snapshot('10001')

    def test_demo_host_and_read_only_submit_is_blocked(self):
        script = Script()
        venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=script, clock=lambda: 1_700_000_000)
        venue.snapshot('10001')
        self.assertTrue(all(url.startswith('https://demo-api.binance.com/') for url in script.urls))
        calls = []
        venue._opener = lambda *args: calls.append(args) or (_ for _ in ()).throw(AssertionError('network'))
        with self.assertRaises(Blocked):
            venue.submit('sq-test', dict(symbol='BTCUSDT', side='BUY', type='MARKET', quoteOrderQty='10'))
        self.assertEqual(calls, [])

    def test_missing_spot_or_stop_permission_blocks_before_account_read(self):
        for field, value in (('orderTypes', ['LIMIT', 'MARKET']), ('isSpotTradingAllowed', False),
                             ('isSpotTradingAllowed', None)):
            script, body = Script(), _filters()
            body['symbols'][0][field] = value
            def opener(method, url, headers):
                if '/api/v3/exchangeInfo' in url:
                    return 200, json.dumps(body).encode()
                return script(method, url, headers)
            venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener,
                            clock=lambda: script.now / 1000)
            with self.assertRaises(Blocked):
                venue.snapshot('10001')
            self.assertFalse(any('/api/v3/account?' in url for url in script.urls))

    def test_daily_history_normalizes_completed_bars_and_rejects_gaps_or_short_pages(self):
        for days, today, valid in (([0, 1, 2], 2, True), ([0, 2], 3, False), ([0], 2, False)):
            script = Script()
            script.now = ORIGIN + today * DAY + 1000
            def opener(method, url, headers):
                if '/api/v3/klines' in url:
                    return 200, json.dumps([[ORIGIN + i * DAY, '100', '110', '90', '105']
                                            for i in days]).encode()
                return script(method, url, headers)
            venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener,
                            clock=lambda: script.now / 1000)
            if valid:
                self.assertEqual(venue.completed_daily(None),
                                 [(ORIGIN + i * DAY, D(100), D(110), D(90), D(105)) for i in range(2)])
                self.assertEqual(venue.completed_daily(ORIGIN + DAY), [])
            else:
                with self.assertRaises(Unknown):
                    venue.completed_daily(None)

    def test_daily_open_reads_the_arrived_open_without_unfinished_hlc(self):
        script = Script()
        script.now = ORIGIN + 2 * DAY + 1000
        calls = []
        def opener(method, url, headers):
            if '/api/v3/klines' in url:
                calls.append(url)
                return 200, json.dumps([[ORIGIN + 2 * DAY, '123', 'unfinished high', None, 'unfinished close']]).encode()
            return script(method, url, headers)
        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener,
                        clock=lambda: script.now / 1000)
        self.assertEqual(venue.daily_open(ORIGIN + 2 * DAY), (ORIGIN + 2 * DAY, D(123)))
        self.assertIn('limit=1', calls[0])
        self.assertNotIn('signature=', calls[0])
        with self.assertRaises(Unknown):
            venue.daily_open(ORIGIN + 3 * DAY)
        self.assertEqual(len(calls), 1)

    def test_daily_open_missing_wrong_day_and_invalid_price_are_blocking(self):
        for payload in ([], [[ORIGIN, '100']], [[ORIGIN + 2 * DAY]],
                        [[ORIGIN + 2 * DAY, '0']], [[ORIGIN + 2 * DAY, 'NaN']]):
            script = Script()
            script.now = ORIGIN + 2 * DAY + 1000
            def opener(method, url, headers):
                if '/api/v3/klines' in url:
                    return 200, json.dumps(payload).encode()
                return script(method, url, headers)
            venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener,
                            clock=lambda: script.now / 1000)
            with self.subTest(payload=payload), self.assertRaises((Unknown, Blocked)):
                venue.daily_open(ORIGIN + 2 * DAY)

    def test_completed_history_rejects_an_open_outside_the_daily_range(self):
        script = Script()
        script.now = ORIGIN + DAY + 1000
        def opener(method, url, headers):
            if '/api/v3/klines' in url:
                return 200, json.dumps([[ORIGIN, '150', '110', '90', '105']]).encode()
            return script(method, url, headers)
        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener,
                        clock=lambda: script.now / 1000)
        with self.assertRaisesRegex(Unknown, 'OHLC'):
            venue.completed_daily(None)


if __name__ == '__main__':
    unittest.main()
