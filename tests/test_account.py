"""Hand-checked spot account path: fee, one-bar delay, trail, and CNY haircut."""
from decimal import Decimal as D
import unittest

from research.account import simulate
from spotquant.model import DAY, ORIGIN


START = ORIGIN + 5 * DAY
END = ORIGIN + 12 * DAY


def fx(_now):
    return D('7')


def bars():
    # Five warmup closes at 100 (SMA 3 is ready before the window).
    # Then a bullish close, an entry day, a trail day whose close is not bullish,
    # and flat days so the account does not re-enter.
    spec = [
        (100, 100, 100, 100),
        (100, 100, 100, 100),
        (100, 100, 100, 100),
        (100, 100, 100, 100),
        (100, 100, 100, 100),
        (100, 110, 100, 110),
        (110, 112, 108, 111),
        (112, 140, 100, 100),
        (90, 90, 90, 90),
        (90, 90, 90, 90),
        (90, 90, 90, 90),
        (90, 90, 90, 90),
    ]
    out = []
    for i, (open_, high, low, close) in enumerate(spec):
        out.append((ORIGIN + i * DAY, D(open_), D(high), D(low), D(close), D('1000')))
    return out


class AccountTests(unittest.TestCase):
    def test_entry_is_delayed_until_the_next_open_and_trail_exits(self):
        result = simulate(
            bars(), fx, start_ms=START, end_ms=END, sma_window=3, trail='0.20',
            fee=D('0.001'), entry_slip=D('0'), exit_slip=D('0'), stop_slip=D('0'), conversion=D('0'),
        )
        self.assertEqual(len(result['trades']), 1)
        trade = result['trades'][0]
        self.assertEqual(trade['kind'], 'trail')
        self.assertEqual(D(trade['entry']), D('110'))
        # High 140 tightens the stop to 112 before the low at 100.
        self.assertEqual(D(trade['exit']), D('112'))
        self.assertEqual(result['position_btc'], D(0))
        self.assertGreater(result['fees'], D(0))
        # Bought the whole cash balance and sold above the entry, so CNY exceeds the start.
        self.assertGreater(result['final_cny'], D('10000'))
        # The same-day high is marked before the stop. Selling 20% under that high
        # is a continuous drawdown of about 20% plus the exit fee.
        self.assertGreater(result['mdd'], D('0.20'))
        self.assertLess(result['mdd'], D('0.21'))

    def test_conversion_haircut_is_applied_twice_on_an_idle_account(self):
        flat = []
        for i in range(12):
            flat.append((ORIGIN + i * DAY, D(100), D(100), D(100), D(100), D(1)))
        result = simulate(
            flat, fx, start_ms=START, end_ms=END, sma_window=3, trail='0.20',
            fee=D('0'), entry_slip=D('0'), exit_slip=D('0'), stop_slip=D('0'), conversion=D('0.001'),
        )
        self.assertEqual(result['trades'], [])
        expected = D('10000') * D('0.999') * D('0.999')
        self.assertEqual(result['final_cny'].quantize(D('0.000001')), expected.quantize(D('0.000001')))


if __name__ == '__main__':
    unittest.main()
