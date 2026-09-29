"""A followed buy explains the balance, and a pre-fill wick is not the stop."""
from decimal import Decimal as D
import unittest

from spotquant.follow import matched_buy, replay
from spotquant.model import DAY, ORIGIN
from spotquant.types import Unknown


class FollowTests(unittest.TestCase):
    def test_btc_commission_is_part_of_the_balance_and_not_the_fill_price(self):
        trades = [{
            'time': ORIGIN + DAY,
            'qty': D('1'),
            'quote': D('100'),
            'buyer': True,
            'commission': D('0.001'),
            'commission_asset': 'BTC',
        }]
        bought = matched_buy(trades, D('0.999'), ORIGIN, D('100'))
        self.assertEqual(bought['entry_fill'], D('100'))
        self.assertEqual(bought['qty'], D('0.999'))

    def test_a_balance_that_is_not_the_buy_is_unknown(self):
        trades = [{
            'time': ORIGIN + DAY, 'qty': D('1'), 'quote': D('100'), 'buyer': True,
            'commission': D('0'), 'commission_asset': 'BNB',
        }]
        with self.assertRaises(Unknown):
            matched_buy(trades, D('1.2'), ORIGIN, D('100'))

    def test_a_pre_fill_wick_does_not_raise_the_peak(self):
        bars = [
            (ORIGIN, D('10'), D('10'), D('10')),
            (ORIGIN + DAY, D('40'), D('10'), D('12')),
        ]
        later = replay(bars, entry_fill=D('12'), first_ms=ORIGIN + DAY + 3_600_000, repair=False)
        self.assertEqual(D(later['peak']), D('12'))
        opened = replay(bars, entry_fill=D('12'), first_ms=ORIGIN + DAY + 1000, repair=False)
        self.assertEqual(D(opened['peak']), D('40'))


if __name__ == '__main__':
    unittest.main()
