"""Followed fills explain the balance, and a sell must leave exactly the recorded sleeves."""
from decimal import Decimal as D
import unittest

from spotquant.follow import apply_day, day_open, replay, unexplained
from spotquant.model import DAY, ORIGIN
from spotquant.types import Unknown


class _Model:
    def note_flat(self, *, rearm=False):
        self.flat = True
        self.rearm = rearm


def _trade(index, time, qty, quote, *, buyer=True, commission='0', asset='BNB', order_id=None):
    return {
        'id': index, 'order_id': index if order_id is None else order_id, 'time': time,
        'qty': D(qty), 'quote': D(quote), 'price': D(quote) / D(qty),
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
        positions, follows, _accounted, closed = apply_day(
            {30: _Model()}, {30: None}, {30: {'signal_ms': signal, 'repair': False}}, set(),
            signal + DAY, trades,
            lambda: [(ORIGIN + index * DAY, D(100), D(100), D(100)) for index in range(12)],
        )
        self.assertEqual(D(positions[30]['entry_fill']), D('100'))
        self.assertEqual(D(positions[30]['qty']), D('0.999'))
        self.assertIsNone(follows[30])
        self.assertEqual(closed, [])

    def test_a_balance_that_is_not_the_buy_is_unknown(self):
        signal = ORIGIN + 10 * DAY
        trades = [_trade(1, signal + DAY + 5, '1', '100')]
        positions, follows, _ids, _closed = apply_day(
            {30: _Model()}, {30: None}, {30: {'signal_ms': signal, 'repair': False}}, set(),
            signal + DAY, trades, lambda: [],
        )
        with self.assertRaises(Unknown):
            unexplained(positions, D('1.2'), D('100'))
        with self.assertRaises(Unknown):
            unexplained(positions, D(0), D(100))

    def test_fill_owned_loss_and_repair_flags_follow_completed_bars(self):
        history = [(ORIGIN + i * DAY, D(100), D(100), D(100)) for i in range(400)]
        def held(prices, entry='100', repair=False):
            bars = history + [(ORIGIN + (400 + i) * DAY, D(p), D(p), D(p))
                              for i, p in enumerate(prices)]
            return replay(bars, entry_fill=D(entry), first_ms=ORIGIN + 400 * DAY,
                          repair=repair)
        self.assertFalse(held(('97',))['adverse'])
        self.assertTrue(held(('97', '96'))['adverse'])
        repairing = held(('48', '40'), entry='48', repair=True)
        self.assertTrue(repairing['repair'])
        self.assertFalse(repairing['adverse'])
        self.assertFalse(held(('48', '40', '100'), entry='48', repair=True)['repair'])

    def test_equal_sleeves_are_unknown_even_when_both_want_to_exit(self):
        first = ORIGIN + 5 * DAY
        held = {30: _position('0.1', first), 40: _position('0.1', first)}
        trades = [_trade(1, first, '0.2', '20'), _trade(2, first + DAY, '0.1', '11', buyer=False)]
        with self.assertRaises(Unknown):
            apply_day(
                {30: _Model(), 40: _Model()}, held, {30: None, 40: None}, set(),
                day_open(first + DAY), trades, lambda: [])

    def test_two_order_ids_are_not_merged_into_one_fill(self):
        signal = ORIGIN + 10 * DAY
        trades = [
            _trade(1, signal + DAY + 5, '0.1', '10', order_id=7),
            _trade(2, signal + DAY + 6, '0.2', '20', order_id=8),
        ]
        with self.assertRaises(Unknown):
            apply_day(
                {30: _Model()}, {30: None}, {30: {'signal_ms': signal, 'repair': False}}, set(),
                signal + DAY, trades, lambda: [])

    def test_a_sell_of_one_sleeve_does_not_erase_the_buy_of_another_at_the_same_open(self):
        first = ORIGIN + 5 * DAY
        signal = ORIGIN + 20 * DAY
        held = {30: _position('0.6', first)}
        history = lambda: [(ORIGIN + index * DAY, D(100), D(100), D(100)) for index in range(25)]
        trades = [
            _trade(1, first, '0.6', '60'),
            _trade(2, signal + DAY + 10, '0.3', '30', order_id=9),
            _trade(3, signal + DAY + 20, '0.6', '60', buyer=False, order_id=10),
        ]
        positions, follows, accounted, closed = apply_day(
            {30: _Model(), 50: _Model()},
            {30: held[30], 50: None}, {30: None, 50: {'signal_ms': signal, 'repair': False}},
            set(), signal + DAY, trades, history)
        self.assertEqual(closed, [30])
        self.assertEqual(positions[50]['qty'], '0.3')
        self.assertIsNone(positions[30])
        self.assertIsNone(follows[50])
        self.assertIn(3, accounted)
        again, _f, _ids, none_closed = apply_day(
            {50: _Model()}, positions, follows, accounted, signal + DAY, trades, history)
        self.assertEqual(none_closed, [])
        self.assertEqual(again[50]['qty'], '0.3')


if __name__ == '__main__':
    unittest.main()
