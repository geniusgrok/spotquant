"""The spot adapter signs GET requests and has no order path."""
import hashlib
import hmac
import json
from decimal import Decimal as D
import unittest

from spotquant.binance import Binance, ORIGIN, DAY
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
        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=script, clock=lambda: self_now())
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

    def test_missing_stop_loss_blocks_before_the_account_call(self):
        script = Script()
        body = _filters()
        body['symbols'][0]['orderTypes'] = ['LIMIT', 'MARKET']

        def opener(method, url, headers):
            if '/api/v3/exchangeInfo' in url:
                script.urls.append(url)
                return 200, json.dumps(body).encode()
            return script(method, url, headers)

        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener, clock=lambda: 1_700_000_000)
        with self.assertRaises(Blocked):
            venue.snapshot('10001')
        self.assertFalse(any('/api/v3/account?' in url for url in script.urls))

    def test_completed_daily_normalizes_and_stops_at_today(self):
        script = Script()
        # server time is one day after the returned bar, so that bar is complete
        script.now = ORIGIN + DAY + 1000
        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=script, clock=lambda: script.now / 1000)
        bars = venue.completed_daily(None)
        self.assertEqual(bars, [(ORIGIN, D('110'), D('90'), D('105'))])
        self.assertEqual(venue.completed_daily(ORIGIN), [])

    def test_completed_daily_rejects_a_page_that_does_not_start_at_the_cursor(self):
        script = Script()
        script.now = ORIGIN + 5 * DAY

        def opener(method, url, headers):
            if '/api/v3/klines' in url:
                row = [ORIGIN + 2 * DAY, '100', '110', '90', '105', '1', 0, '1']
                return 200, json.dumps([row]).encode()
            return script(method, url, headers)

        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener, clock=lambda: script.now / 1000)
        with self.assertRaises(Unknown):
            venue.completed_daily(None)

    def test_completed_daily_rejects_a_gap_and_keeps_a_contiguous_page(self):
        script = Script()
        script.now = ORIGIN + 5 * DAY
        pages = {'gap': [
            [ORIGIN, '100', '110', '90', '105', '1', 0, '1'],
            [ORIGIN + 2 * DAY, '100', '110', '90', '105', '1', 0, '1'],
        ], 'ok': [
            [ORIGIN, '100', '110', '90', '105', '1', 0, '1'],
            [ORIGIN + DAY, '101', '111', '91', '106', '1', 0, '1'],
        ]}

        def opener(kind, now):
            def _open(method, url, headers):
                if '/api/v3/klines' in url:
                    return 200, json.dumps(pages[kind]).encode()
                if '/api/v3/time' in url:
                    return 200, json.dumps({'serverTime': now}).encode()
                return script(method, url, headers)
            return _open

        gapped = Binance(
            key=KEY, secret=SECRET, environment='live', opener=opener('gap', script.now),
            clock=lambda: script.now / 1000,
        )
        with self.assertRaises(Unknown):
            gapped.completed_daily(None)
        reached = ORIGIN + 2 * DAY
        whole = Binance(
            key=KEY, secret=SECRET, environment='live', opener=opener('ok', reached),
            clock=lambda: reached / 1000,
        )
        bars = whole.completed_daily(None)
        self.assertEqual([item[0] for item in bars], [ORIGIN, ORIGIN + DAY])
        self.assertEqual(bars[1][3], D('106'))

    def test_completed_daily_rejects_a_short_page_before_today(self):
        script = Script()
        script.now = ORIGIN + 5 * DAY

        def opener(method, url, headers):
            if '/api/v3/klines' in url:
                return 200, json.dumps([[ORIGIN, '100', '110', '90', '105', '1', 0, '1']]).encode()
            return script(method, url, headers)

        venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener, clock=lambda: script.now / 1000)
        with self.assertRaises(Unknown) as caught:
            venue.completed_daily(None)
        self.assertIn('stops before', str(caught.exception))

        def empty(method, url, headers):
            if '/api/v3/klines' in url:
                return 200, b'[]'
            return script(method, url, headers)

        blank = Binance(key=KEY, secret=SECRET, environment='live', opener=empty, clock=lambda: script.now / 1000)
        with self.assertRaises(Unknown):
            blank.completed_daily(None)

    def test_missing_spot_permission_blocks_before_the_account_call(self):
        for allowed in (False, None):
            script = Script()
            body = _filters()
            if allowed is None:
                del body['symbols'][0]['isSpotTradingAllowed']
            else:
                body['symbols'][0]['isSpotTradingAllowed'] = allowed

            def opener(method, url, headers, payload=body, seen=script):
                if '/api/v3/exchangeInfo' in url:
                    seen.urls.append(url)
                    return 200, json.dumps(payload).encode()
                return seen(method, url, headers)

            venue = Binance(key=KEY, secret=SECRET, environment='live', opener=opener, clock=lambda: 1_700_000_000)
            with self.assertRaises(Blocked):
                venue.snapshot('10001')
            self.assertFalse(any('/api/v3/account?' in url for url in script.urls))


def self_now():
    return 1_700_000_000


if __name__ == '__main__':
    unittest.main()
