"""Followed fills explain the balance, and a sell must leave exactly the recorded sleeves."""
from decimal import Decimal as D
import unittest

from spotquant.follow import reconcile, replay
from spotquant.model import DAY, ORIGIN
from spotquant.types import Unknown


def _trade(index, time, qty, quote, *, buyer=True, commission='0', asset='BNB'):
    return {
        'id': index, 'time': time, 'qty': D(qty), 'quote': D(quote), 'price': D(quote) / D(qty),
        'buyer': buyer, 'commission': D(commission), 'commission_asset': asset,
    }


def _position(qty, first_ms, entry='100'):
    return {
        'entry_fill': entry, 'first_ms': first_ms, 'entry_open_ms': first_ms // DAY * DAY,
        'peak': entry, 'repair': False, 'repair_peak': None, 'adverse': False,
        'through': None, 'qty': qty,
    }


class FollowTests(unittest.TestCase):
    def test_a_pre_fill_wick_does_not_raise_the_peak(self):
        bars = [
            (ORIGIN, D('10'), D('10'), D('10')),
            (ORIGIN + DAY, D('40'), D('10'), D('12')),
        ]
        later = replay(bars, entry_fill=D('12'), first_ms=ORIGIN + DAY + 3_600_000, repair=False)
        self.assertEqual(D(later['peak']), D('12'))
        opened = replay(bars, entry_fill=D('12'), first_ms=ORIGIN + DAY + 1000, repair=False)
        self.assertEqual(D(opened['peak']), D('40'))

    def test_btc_commission_is_part_of_the_balance_and_not_the_fill_price(self):
        signal = ORIGIN + 10 * DAY
        trades = [_trade(1, signal + DAY + 5, '1', '100', commission='0.001', asset='BTC')]
        positions, follows, ledger, closed = reconcile(
            {30: None}, {30: {'signal_ms': signal, 'repair': False}}, trades, D('0.999'), D('100'),
            None, set(), lambda: [(ORIGIN + index * DAY, D(100), D(100), D(100)) for index in range(12)],
        )
        self.assertEqual(D(positions[30]['entry_fill']), D('100'))
        self.assertEqual(D(positions[30]['qty']), D('0.999'))
        self.assertIsNone(follows[30])
        self.assertEqual(closed, [])

    def test_sleeves_on_one_signal_day_share_one_fill_equally(self):
        signal = ORIGIN + 10 * DAY
        trades = [_trade(1, signal + DAY + 5, '0.3', '30')]
        follows = {window: {'signal_ms': signal, 'repair': False} for window in (30, 40, 50)}
        positions, follows, _ledger, _closed = reconcile(
            {30: None, 40: None, 50: None}, follows, trades, D('0.3'), D('100'), None, set(),
            lambda: [(ORIGIN + index * DAY, D(100), D(100), D(100)) for index in range(12)],
        )
        self.assertEqual(
            [D(positions[window]['qty']) for window in (30, 40, 50)],
            [D('0.1'), D('0.1'), D('0.1')])
        self.assertTrue(all(item is None for item in follows.values()))

    def test_a_balance_that_is_not_the_buy_is_unknown(self):
        signal = ORIGIN + 10 * DAY
        trades = [_trade(1, signal + DAY + 5, '1', '100')]
        with self.assertRaises(Unknown):
            reconcile(
                {30: None}, {30: {'signal_ms': signal, 'repair': False}}, trades, D('1.2'), D('100'),
                None, set(), lambda: [],
            )

    def test_a_sell_that_leaves_one_sleeve_closes_only_that_sleeve(self):
        first = ORIGIN + 5 * DAY + 60
        held = {30: _position('0.1', first), 40: _position('0.2', first), 50: None}
        trades = [_trade(1, first, '0.3', '30'), _trade(2, first + 2 * DAY, '0.1', '11', buyer=False)]
        positions, _follows, ledger, closed = reconcile(
            held, {30: None, 40: None, 50: None}, trades, D('0.2'), D('110'), None, {30}, lambda: [])
        self.assertEqual(closed, [30])
        self.assertIsNone(positions[30])
        self.assertEqual(positions[40]['qty'], '0.2')
        self.assertEqual(ledger, first + 2 * DAY)

    def test_a_full_transfer_out_with_no_sell_is_unknown(self):
        first = ORIGIN + 5 * DAY
        held = {30: _position('0.1', first)}
        with self.assertRaises(Unknown):
            reconcile(held, {30: None}, [_trade(1, first, '0.1', '10')], D('0'), D('100'),
                      None, {30}, lambda: [])

    def test_a_sell_already_accounted_for_is_not_counted_twice(self):
        first = ORIGIN + 5 * DAY
        sold_at = first + DAY
        held = {40: _position('0.2', first + 2 * DAY)}
        trades = [
            _trade(1, first, '0.1', '10'), _trade(2, sold_at, '0.1', '11', buyer=False),
            _trade(3, first + 2 * DAY, '0.2', '20'),
        ]
        positions, _f, ledger, closed = reconcile(
            held, {40: None}, trades, D('0.2'), D('100'), sold_at, set(), lambda: [])
        self.assertEqual(closed, [])
        self.assertEqual(positions[40]['qty'], '0.2')
        self.assertEqual(ledger, sold_at)

    def test_an_unflagged_tie_between_equal_sleeves_is_unknown(self):
        first = ORIGIN + 5 * DAY
        held = {30: _position('0.1', first), 40: _position('0.1', first)}
        trades = [_trade(1, first, '0.2', '20'), _trade(2, first + DAY, '0.1', '11', buyer=False)]
        with self.assertRaises(Unknown):
            reconcile(held, {30: None, 40: None}, trades, D('0.1'), D('110'), None, set(), lambda: [])
        positions, _f, _l, closed = reconcile(
            held, {30: None, 40: None}, trades, D('0.1'), D('110'), None, {40}, lambda: [])
        self.assertEqual(closed, [40])
        self.assertIsNotNone(positions[30])

    def test_a_sell_of_one_sleeve_does_not_erase_the_buy_of_another_at_the_same_open(self):
        first = ORIGIN + 5 * DAY
        signal = ORIGIN + 20 * DAY
        held = {30: _position('0.6', first)}
        history = lambda: [(ORIGIN + index * DAY, D(100), D(100), D(100)) for index in range(25)]
        trades = [
            _trade(1, first, '0.6', '60'),
            _trade(2, signal + DAY + 10, '0.3', '30'),
            _trade(3, signal + DAY + 20, '0.6', '60', buyer=False),
        ]
        positions, follows, ledger, closed = reconcile(
            {30: held[30], 50: None}, {30: None, 50: {'signal_ms': signal, 'repair': False}},
            trades, D('0.3'), D('100'), None, {30}, history)
        self.assertEqual(closed, [30])
        self.assertEqual(positions[50]['qty'], '0.3')
        self.assertIsNone(positions[30])
        self.assertIsNone(follows[50])
        self.assertEqual(ledger, signal + DAY + 20)
        again, _f, _l, none_closed = reconcile(
            positions, follows, trades, D('0.3'), D('100'), ledger, set(), history)
        self.assertEqual(none_closed, [])
        self.assertEqual(again[50]['qty'], '0.3')


if __name__ == '__main__':
    unittest.main()
