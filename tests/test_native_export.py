from decimal import Decimal as D
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, Mock

from spotquant.binance import Binance, _default_opener
from spotquant.snapshot import export
from spotquant.types import Blocked, Unknown
from research.operations import combined
from tests.test_binance import Script, KEY, SECRET


class ExportTests(TestCase):
    def test_default_http_transport_preserves_explicit_demo_methods(self):
        response = Mock(status=200)
        response.read.return_value = b'{}'
        response.headers.items.return_value = []
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch('spotquant.binance.urllib.request.build_opener', return_value=opener):
            for method in ('POST', 'DELETE'):
                _default_opener(method, 'https://demo-api.binance.com/api/v3/order', {})
                self.assertEqual(opener.open.call_args.args[0].get_method(), method)

    def test_changing_balances_during_order_scan_are_unknown(self):
        script = Script()
        account_reads = 0
        def transport(method, url, headers):
            nonlocal account_reads
            status, body = script(method, url, headers)
            if '/api/v3/account' in url:
                account_reads += 1
                if account_reads > 1:
                    payload = json.loads(body)
                    payload['balances'][1]['free'] = '20'
                    body = json.dumps(payload).encode()
            return status, body
        venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=transport)
        with self.assertRaises(Unknown):
            venue.snapshot('10001')

    def test_read_only_export_includes_locked_cash_and_uses_fresh_price(self):
        script = Script()
        def transport(method, url, headers):
            if '/ticker/price' in url:
                return 200, b'{"price":"101"}'
            return script(method, url, headers)
        venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=transport,
                        clock=lambda: script.now / 1000)
        row = export(SimpleNamespace(account_uid='10001', environment='demo'), venue)
        self.assertEqual(row['cash_usdt'], '26.50')
        self.assertEqual(D(row['equity_usdt']), D('26.50'))
        self.assertEqual(row['btc_price_usdt'], '101')
        self.assertTrue(all(method == 'GET' for method in script.methods))

    def test_missing_asset_balance_is_unknown(self):
        script = Script()
        def transport(method, url, headers):
            status, body = script(method, url, headers)
            if '/api/v3/account' in url:
                payload = json.loads(body)
                payload['balances'] = payload['balances'][1:]
                body = json.dumps(payload).encode()
            return status, body
        venue = Binance(key=KEY, secret=SECRET, environment='demo', opener=transport)
        with self.assertRaises(Unknown):
            venue.snapshot('10001')

    def test_demo_order_gate_rejects_live_before_transport(self):
        with self.assertRaises(Blocked):
            Binance(key=KEY, secret=SECRET, environment='live', capital_limit=D(10), demo_execution_uid='1')

    def test_normalized_accounts_revalue_at_perpetual_mark_without_adding_margin(self):
        base = {'known': True, 'symbol': 'BTCUSDT', 'environment': 'demo', 'account_uid': '1',
                'observed_at_ms': 1000000}
        spot = dict(base, market='spot', cash_usdt='500', btc_position='2', btc_price_usdt='99', equity_usdt='698')
        perp = dict(base, market='perpetual', wallet_usdt='1000', entry_price_usdt='90',
                    btc_position='-1', btc_price_usdt='100', equity_usdt='990')
        row = combined([spot, perp], 1000001)
        self.assertEqual(row['equity_usdt'], '1690')
        self.assertEqual(row['btc_gross'], '3')
        self.assertEqual(row['btc_net'], '1')
