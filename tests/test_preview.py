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
        self.assertNotIn('quantity', decision['protection'])
        self.assertNotIn('trailingDelta', decision['protection'])

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
