"""The spot adapter signs GET requests and has no order path."""
import hashlib
import hmac
import json
from decimal import Decimal as D
import unittest

from spotquant.binance import Binance, ORIGIN, DAY
from spotquant.types import Blocked


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
                {'filterType': 'TRAILING_DELTA', 'maxTrailingBelowDelta': 2000, 'maxTrailingAboveDelta': 2000},
            ],
        }],
    }


class Script:
    def __init__(self, uid='10001', trailing=2000):
        self.urls = []
        self.methods = []
        self.uid = uid
        self.trailing = trailing
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
            body['symbols'][0]['filters'][3]['maxTrailingBelowDelta'] = self.trailing
            return 200, json.dumps(body).encode()
        if '/api/v3/account' in url:
            return 200, json.dumps({'balances': [
                {'asset': 'BTC', 'free': '0.00000000', 'locked': '0.00000000'},
                {'asset': 'USDT', 'free': '25.50', 'locked': '1.00'},
            ]}).encode()
        if '/sapi/v1/account/uid' in url:
            return 200, json.dumps({'uid': self.uid}).encode()
        if '/api/v3/openOrders' in url:
            return 200, b'[]'
        if '/api/v3/klines' in url:
            row = [ORIGIN, '100', '110', '90', '105', '1', ORIGIN + DAY - 1, '1000']
            return 200, json.dumps([row]).encode()
        raise AssertionError(url)


class BinanceTests(unittest.TestCase):
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

    def test_demo_host_and_no_order_method(self):
        script = Script()
        venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=script, clock=lambda: 1_700_000_000)
        venue.snapshot('10001')
        self.assertTrue(all(url.startswith('https://demo-api.binance.com/') for url in script.urls))
        calls = []
        venue._opener = lambda *args: calls.append(args) or (_ for _ in ()).throw(AssertionError('network'))
        with self.assertRaises(Blocked):
            venue.place_order(symbol='BTCUSDT')
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


def self_now():
    return 1_700_000_000


if __name__ == '__main__':
    unittest.main()
