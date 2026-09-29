"""Hand-checked spot account path: fee, one-bar delay, trail, and CNY haircut."""
from decimal import Decimal as D
import unittest

from research.account import simulate, simulate_sleeves
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
            confirm=1, fresh=False, crash='0',
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

    def test_a_close_four_percent_under_the_fill_sells_the_next_open(self):
        spec = [
            (100, 100, 100, 100),
            (100, 100, 100, 100),
            (100, 110, 100, 110),
            (110, 110, 104, 105),
            (104, 104, 104, 104),
            (104, 104, 104, 104),
        ]
        series = [
            (ORIGIN + i * DAY, D(o), D(h), D(l), D(c), D('1'))
            for i, (o, h, l, c) in enumerate(spec)
        ]
        result = simulate(
            series, fx, start_ms=ORIGIN + 3 * DAY, end_ms=ORIGIN + 6 * DAY,
            sma_window=2, trail='0.28', confirm=1, fresh=False, crash='0',
            fee=D('0'), entry_slip=D('0'), exit_slip=D('0'), stop_slip=D('0'), conversion=D('0'),
        )
        self.assertEqual(len(result['trades']), 1)
        trade = result['trades'][0]
        self.assertEqual(trade['kind'], 'adverse')
        self.assertEqual(D(trade['entry']), D('110'))
        self.assertEqual(D(trade['exit']), D('104'))

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

    def test_one_sleeve_prints_the_single_account(self):
        args = dict(
            start_ms=START, end_ms=END, confirm=1, fresh=False, crash='0', trail='0.20',
            fee=D('0.001'), entry_slip=D('0.0005'), exit_slip=D('0.0005'), stop_slip=D('0.001'),
            conversion=D('0.001'),
        )
        single = simulate(bars(), fx, sma_window=3, **args)
        sleeve = simulate_sleeves(
            bars(), fx, start_ms=START, end_ms=END, windows=(3,), fee=args['fee'],
            entry_slip=args['entry_slip'], exit_slip=args['exit_slip'], stop_slip=args['stop_slip'],
            conversion=args['conversion'],
            model_kwargs={'confirm': 1, 'fresh': False, 'crash': '0', 'trail': '0.20'},
        )
        self.assertEqual(sleeve['final_cny'], single['final_cny'])
        self.assertEqual(sleeve['mdd'], single['mdd'])
        self.assertEqual(
            [(t['entry'], t['exit'], t['kind']) for t in sleeve['trades']],
            [(t['entry'], t['exit'], t['kind']) for t in single['trades']])

    def test_two_sleeves_split_the_pool_and_exit_on_their_own_coins(self):
        rows = []
        closes = [100] * 8 + [110, 120, 130, 140, 100, 100, 100]
        for index, close in enumerate(closes):
            rows.append((ORIGIN + index * DAY, D(close), D(close), D(close), D(close), D(1)))
        result = simulate_sleeves(
            rows, fx, start_ms=ORIGIN + 8 * DAY, end_ms=ORIGIN + 15 * DAY, windows=(2, 5),
            model_kwargs={'confirm': 1, 'fresh': False, 'crash': '0', 'trail': '0.50', 'cap_drop': '0',
                          'adverse_stop': '0', 'extend': '0'},
            fee=D(0), entry_slip=D(0), exit_slip=D(0), stop_slip=D(0), conversion=D(0),
        )
        by_sleeve = {trade['sleeve']: trade for trade in result['trades']}
        self.assertEqual(sorted(by_sleeve), [2, 5])
        self.assertEqual(by_sleeve[2]['entry'], by_sleeve[5]['entry'])
        self.assertLessEqual(D(by_sleeve[2]['exit_ms']), D(by_sleeve[5]['exit_ms']))
        self.assertEqual(result['position_btc'], D(0))

    def test_a_cold_start_waits_for_a_fresh_cross_and_the_first_open_never_buys(self):
        rows = [(ORIGIN + index * DAY, D(100 + index), D(100 + index), D(100 + index), D(100 + index), D(1))
                for index in range(12)]
        result = simulate_sleeves(
            rows, fx, start_ms=ORIGIN + 8 * DAY, end_ms=ORIGIN + 12 * DAY, windows=(3,),
            model_kwargs={'confirm': 1, 'fresh': True, 'crash': '0', 'cap_drop': '0'},
            fee=D(0), entry_slip=D(0), exit_slip=D(0), stop_slip=D(0), conversion=D(0),
            cold_start=True,
        )
        self.assertEqual(result['trades'], [])
        self.assertEqual(result['position_btc'], D(0))


if __name__ == '__main__':
    unittest.main()
