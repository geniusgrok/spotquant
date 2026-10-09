"""Followed fills explain the balance, and a sell must leave exactly the recorded sleeves."""
from decimal import Decimal as D
import json
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
            (ORIGIN, D('10'), D('10'), D('10'), D('10')),
            (ORIGIN + DAY, D('12'), D('40'), D('10'), D('12')),
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
            lambda: [(ORIGIN + index * DAY, D(100), D(100), D(100), D(100)) for index in range(12)],
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
        history = [(ORIGIN + i * DAY, D(100), D(100), D(100), D(100)) for i in range(400)]
        def held(prices, entry='100', repair=False):
            bars = history + [(ORIGIN + (400 + i) * DAY, D(p), D(p), D(p), D(p))
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
        history = lambda: [(ORIGIN + index * DAY, D(100), D(100), D(100), D(100)) for index in range(25)]
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

    def test_only_durable_touch_sale_rearms_a_nonrepair_position(self):
        first = ORIGIN + 10 * DAY
        for kind, permission, repair, expected in (
                ('MARKET', True, False, True),
                ('MARKET', False, False, False),
                ('MARKET', None, False, False),
                ('STOP_LOSS', True, False, False),
                ('MARKET', True, True, False)):
            with self.subTest(kind=kind, permission=permission, repair=repair):
                model = _Model()
                model.shadow_in, model.shadow_repair = True, True
                held = dict(_position('1', first), repair=repair,
                            repair_peak='100' if repair else None)
                owner = {'order': {'side': 'SELL', 'type': kind, 'quantity': '1'},
                         'sleeves': [40], 'weights': {'40': '1'}}
                if permission is not None:
                    owner['rearm'] = {'40': permission}
                positions, _, accounted, closed = apply_day(
                    {40: model}, {40: held}, {40: None}, set(), first + DAY,
                    [_trade(1, first + DAY + 60_000, '1', '100', buyer=False, order_id=7)],
                    lambda: [], owners={'7': owner})
                self.assertEqual(model.rearm, expected)
                self.assertEqual(closed, [40])
                self.assertIsNone(positions[40])
                self.assertEqual(accounted, {1})

    def test_unowned_sell_does_not_infer_touch_permission_from_long_shadow(self):
        first = ORIGIN + 10 * DAY
        model = _Model()
        model.shadow_in = True
        apply_day({40: model}, {40: _position('1', first)}, {40: None}, set(), first + DAY,
                  [_trade(1, first + DAY + 60_000, '1', '100', buyer=False)], lambda: [])
        self.assertFalse(model.rearm)

    def test_touch_permission_survives_partial_sale_and_restart(self):
        first = ORIGIN + 10 * DAY
        owner = {'order': {'side': 'SELL', 'type': 'MARKET', 'quantity': '1'},
                 'sleeves': [40], 'weights': {'40': '1'}, 'rearm': {'40': True}}
        trades = [_trade(1, first + DAY + 60_000, '.4', '40', buyer=False, order_id=7),
                  _trade(2, first + DAY + 60_001, '.6', '60', buyer=False, order_id=7)]
        model = _Model()
        model.shadow_in = True
        positions, follows, accounted, closed = apply_day(
            {40: model}, {40: _position('1', first)}, {40: None}, set(), first + DAY,
            trades[:1], lambda: [], owners={'7': owner})
        self.assertEqual(closed, [])
        self.assertFalse(getattr(model, 'flat', False))
        positions = {40: json.loads(json.dumps(positions[40]))}
        owner = json.loads(json.dumps(owner))
        positions, _, accounted, closed = apply_day(
            {40: model}, positions, follows, accounted, first + DAY, trades,
            lambda: [], owners={'7': owner})
        self.assertTrue(model.rearm)
        self.assertEqual(closed, [40])
        self.assertIsNone(positions[40])
        self.assertEqual(accounted, {1, 2})

    def test_higher_partial_buy_updates_fill_peak_and_is_not_recounted_on_restart(self):
        first = ORIGIN + 10 * DAY + 60_000
        trades = [_trade(1, first, '1', '100', order_id=7),
                  _trade(2, first, '.5', '60', order_id=7)]
        for repair in (False, True):
            with self.subTest(repair=repair):
                owner = {'order': {'side': 'BUY', 'type': 'MARKET'},
                         'sleeves': [40], 'weights': {'40': '1'}, 'repair': {'40': repair}}
                positions, follows, accounted, _ = apply_day(
                    {40: _Model()}, {40: None}, {40: None}, set(), day_open(first),
                    trades[:1], lambda: [], owners={'7': owner})
                positions = {40: json.loads(json.dumps(positions[40]))}
                positions, follows, accounted, _ = apply_day(
                    {40: _Model()}, positions, follows, accounted, day_open(first),
                    trades, lambda: [], owners={'7': owner})
                self.assertEqual(D(positions[40]['qty']), D('1.5'))
                self.assertEqual(D(positions[40]['entry_gross_qty']), D('1.5'))
                self.assertEqual(D(positions[40]['entry_fill']), D('160') / D('1.5'))
                self.assertEqual(D(positions[40]['peak']), D('120'))
                self.assertEqual(positions[40]['repair_peak'], '120' if repair else None)
                self.assertEqual(positions[40]['first_ms'], first)
                before = json.loads(json.dumps(positions[40]))
                positions, _, _, _ = apply_day(
                    {40: _Model()}, positions, follows, accounted, day_open(first),
                    trades, lambda: [], owners={'7': owner})
                self.assertEqual(positions[40], before)

    def test_partial_buy_average_uses_gross_fills_when_fee_assets_differ(self):
        first = ORIGIN + 10 * DAY + 60_000
        trades = [_trade(1, first, '1', '100', order_id=7, commission='.001', asset='BTC'),
                  _trade(2, first + 1, '1', '120', order_id=7, commission='.12', asset='USDT')]
        owner = {'order': {'side': 'BUY', 'type': 'MARKET'}, 'sleeves': [40],
                 'weights': {'40': '1'}, 'repair': {'40': False}}
        positions, follows, accounted, _ = apply_day(
            {40: _Model()}, {40: None}, {40: None}, set(), day_open(first), trades[:1],
            lambda: [], owners={'7': owner})
        positions = {40: json.loads(json.dumps(positions[40]))}
        positions, follows, accounted, _ = apply_day(
            {40: _Model()}, positions, follows, accounted, day_open(first), trades,
            lambda: [], owners={'7': owner})
        self.assertEqual(D(positions[40]['entry_fill']), D(110))
        self.assertEqual(D(positions[40]['entry_gross_qty']), D(2))
        self.assertEqual(D(positions[40]['qty']), D('1.999'))
        before = json.loads(json.dumps(positions[40]))
        positions, _, _, _ = apply_day(
            {40: _Model()}, positions, follows, accounted, day_open(first), trades,
            lambda: [], owners={'7': owner})
        self.assertEqual(positions[40], before)
        positions[40].pop('entry_gross_qty')
        with self.assertRaisesRegex(Unknown, 'gross fill amount'):
            apply_day({40: _Model()}, positions, follows, accounted, day_open(first),
                      [_trade(3, first + 2, '1', '140', order_id=7)], lambda: [], owners={'7': owner})

    def test_new_buy_reuses_only_dust_cost_basis_from_the_closed_position(self):
        first = ORIGIN + 10 * DAY + 60_000
        dust = dict(_position('.000001', first - DAY), dust=True, entry_gross_qty='999')
        owner = {'order': {'side': 'BUY', 'type': 'MARKET'}, 'sleeves': [40],
                 'weights': {'40': '1'}, 'repair': {'40': False}}
        trades = [_trade(1, first, '1', '120', order_id=7, commission='.001', asset='BTC'),
                  _trade(2, first + 1, '1', '140', order_id=7, commission='.14', asset='USDT')]
        positions, _, _, _ = apply_day(
            {40: _Model()}, {40: dust}, {40: None}, set(), day_open(first), trades,
            lambda: [], owners={'7': owner})
        position = positions[40]
        self.assertEqual(D(position['entry_gross_qty']), D('2.000001'))
        self.assertEqual(D(position['entry_fill']), D('260.0001') / D('2.000001'))
        self.assertEqual(D(position['qty']), D('1.999001'))
        self.assertEqual(position['first_ms'], first)
        self.assertFalse(position.get('dust', False))


if __name__ == '__main__':
    unittest.main()
