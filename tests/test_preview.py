"""Decisions stay long-or-flat and do not invent a fill."""
from decimal import Decimal as D
import unittest

from spotquant.model import DAY, ORIGIN, Model
from spotquant.preview import preview
from spotquant.types import Unknown


def _model(closes):
    model = Model(sma_window=3, trail='0.20', confirm=1, fresh=False, crash='0')
    for index, close in enumerate(closes):
        model.update(ORIGIN + index * DAY, close, close, close)
    return model


def _snap(usdt='1000', btc='0', orders=0):
    return {
        'btc': D(btc), 'usdt_free': D(usdt), 'usdt_locked': D(0), 'open_orders': orders,
        'account_uid': '10001', 'environment': 'live',
    }


class PreviewTests(unittest.TestCase):
    def test_cold_start_does_not_buy_an_already_bullish_regime(self):
        model = _model((10, 10, 10, 12))
        self.assertTrue(model.bull)
        decision = preview(model, _snap(), entries_enabled=False, capital_limit=None)
        self.assertEqual(decision['action'], 'flat')
        self.assertIsNone(decision['order'])

    def test_fresh_bull_close_previews_a_market_buy_and_a_stop_price(self):
        model = _model((10, 10, 10, 12))
        decision = preview(model, _snap('1000.019'), entries_enabled=True, capital_limit=D('100'))
        self.assertEqual(decision['action'], 'enter')
        self.assertEqual(decision['order']['quoteOrderQty'], '100.00')
        self.assertEqual(decision['order']['side'], 'BUY')
        self.assertEqual(decision['protection']['stopPrice'], '9.60')
        self.assertIn('two confirmed', decision['reason'])
        self.assertNotIn('quantity', decision['protection'])
        self.assertNotIn('trailingDelta', decision['protection'])

    def test_entry_stop_uses_the_completed_close_when_the_streak_high_is_through_it(self):
        model = Model(sma_window=2, trail='0.20', confirm=1, fresh=False, crash='0', cap_drop='0')
        model.update(ORIGIN, 10, 10, 10)
        model.update(ORIGIN + DAY, 10, 10, 10)
        model.update(ORIGIN + 2 * DAY, 40, 10, 12)
        self.assertTrue(model.enter)
        self.assertFalse(model.cap_enter)
        decision = preview(model, _snap(), entries_enabled=True, capital_limit=None)
        self.assertEqual(decision['action'], 'enter')
        self.assertEqual(decision['protection']['stopPrice'], '9.60')

    def test_crash_reversal_wins_when_the_ordinary_entry_is_also_armed(self):
        model = Model(
            sma_window=4, trail='0.28', confirm=2, fresh=False, crash='0',
            cap_window=6, cap_drop='0.08', cap_bounce='0.06', cap_depth='0.50',
        )
        series = ((100, 100), (20, 20), (20, 20), (30, 30), (80, 27), (30, 29))
        for index, (high, close) in enumerate(series):
            model.update(ORIGIN + index * DAY, D(high), D(close), D(close))
        self.assertTrue(model.enter)
        self.assertTrue(model.cap_enter)
        decision = preview(model, _snap(), entries_enabled=True, capital_limit=None)
        self.assertEqual(decision['action'], 'enter')
        self.assertIn('crash reversal', decision['reason'])
        self.assertNotIn('two confirmed', decision['reason'])
        # 29 * 0.72 = 20.88. The streak high of 80 would print 57.60, above the close.
        self.assertEqual(decision['protection']['stopPrice'], '20.88')

    def test_owned_hold_uses_the_close_until_the_fill_and_then_the_fill_peak(self):
        model = Model(sma_window=2, trail='0.20', confirm=1, fresh=False, crash='0', cap_drop='0')
        model.update(ORIGIN, 10, 10, 10)
        model.update(ORIGIN + DAY, 40, 10, 12)
        decision = preview(
            model, _snap(btc='1'), entries_enabled=True, capital_limit=None, owned_btc=D('1'),
        )
        self.assertEqual(decision['action'], 'hold')
        # 12 * 0.80 = 9.60. The pre-fill wick at 40 would print 32.00.
        self.assertEqual(decision['protection']['stopPrice'], '9.60')
        self.assertIn('completed close', decision['reason'])
        self.assertNotIn('streak', decision['protection']['note'])
        model.position_peak = D('15')
        filled = preview(
            model, _snap(btc='1'), entries_enabled=True, capital_limit=None, owned_btc=D('1'),
        )
        self.assertEqual(filled['protection']['stopPrice'], '12.00')
        self.assertIn('since the fill', filled['reason'])

    def test_a_stop_wider_than_the_sell_band_is_not_placeable(self):
        model = Model(sma_window=2, trail='0.28', confirm=1, fresh=False, crash='0', cap_drop='0')
        model.update(ORIGIN, 10, 10, 10)
        model.update(ORIGIN + DAY, 12, 10, 12)
        snap = _snap()
        snap.update(avg_price=D('12'), ask_multiplier_down=D('0.8'), ask_multiplier_up=D('2'), trailing_max_bips=2000)
        decision = preview(model, snap, entries_enabled=True, capital_limit=None)
        protection = decision['protection']
        self.assertEqual(protection['stopPrice'], '8.64')
        self.assertFalse(protection['placeable'])
        self.assertFalse(protection['limit_placeable'])
        self.assertFalse(protection['trailing_placeable'])
        self.assertNotIn('price', protection)

    def test_external_btc_is_unknown(self):
        model = _model((10, 10, 10, 12))
        with self.assertRaises(Unknown):
            preview(model, _snap(btc='1'), entries_enabled=True, capital_limit=None)

    def test_owned_coins_exit_a_blowoff_while_still_above_the_average(self):
        model = Model(sma_window=2, trail='0.20', confirm=1, fresh=False, crash='0', cap_drop='0')
        for index, close in enumerate((10, 10, 40)):
            model.update(ORIGIN + index * DAY, close, close, close)
        self.assertTrue(model.extended)
        decision = preview(
            model, _snap(btc='1'), entries_enabled=True, capital_limit=None, owned_btc=D('1'),
        )
        self.assertEqual(decision['action'], 'exit')
        self.assertIn('extended', decision['reason'])

    def test_owned_coins_exit_when_the_close_is_four_percent_under_the_fill(self):
        model = Model(sma_window=2, trail='0.28', confirm=1, fresh=False, crash='0', cap_drop='0')
        model.update(ORIGIN, 100, 100, 100)
        model.update(ORIGIN + DAY, 110, 110, 110)
        model.note_entry(D('100'))
        model.update(ORIGIN + 2 * DAY, 100, 95, 96)
        self.assertTrue(model.adverse)
        decision = preview(
            model, _snap(btc='1'), entries_enabled=True, capital_limit=None, owned_btc=D('1'),
        )
        self.assertEqual(decision['action'], 'exit')
        self.assertIn('4%', decision['reason'])
        self.assertIn('not capped', decision['reason'])
        self.assertFalse(decision['loss_capped'])

    def test_owned_coins_exit_when_the_close_is_not_bullish(self):
        model = _model((10, 10, 10, 9))
        self.assertFalse(model.bull)
        decision = preview(
            model, _snap(btc='1'), entries_enabled=True, capital_limit=None, owned_btc=D('1'),
        )
        self.assertEqual(decision['action'], 'exit')
        self.assertEqual(decision['order']['side'], 'SELL')
        self.assertEqual(decision['order']['quantity'], '1.00000')


if __name__ == '__main__':
    unittest.main()
