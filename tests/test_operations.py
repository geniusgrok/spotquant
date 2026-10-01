from unittest import TestCase

from research.operations import combined


def snapshot(market, qty):
    return {'known': True, 'market': market, 'account_uid': '1', 'environment': 'demo',
            'symbol': 'BTCUSDT', 'observed_at_ms': 1000000, 'btc_price_usdt': '100',
            'equity_usdt': '1000', 'btc_position': qty}


class OperationsTests(TestCase):
    def test_hedge_does_not_erase_gross_exposure_or_double_count_margin(self):
        row = combined([snapshot('spot', '10'), snapshot('perpetual', '-10')], 1000001)
        self.assertEqual(row['btc_net'], '0')
        self.assertEqual(row['btc_gross'], '20')
        self.assertEqual(row['equity_usdt'], '2000')
        self.assertFalse(row['new_risk_authorized'])

    def test_unknown_stale_and_mixed_valuation_refuse_summary(self):
        for override in ({'known': False}, {'observed_at_ms': 1}, {'btc_price_usdt': '101'}, {'environment': 'live'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                combined([snapshot('spot', '1'), dict(snapshot('perpetual', '1'), **override)], 1000001)
