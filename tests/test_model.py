"""Causal daily regime: warmup, delay, checkpoint, and no same-bar lookahead."""
from decimal import Decimal as D
import unittest

from spotquant.model import CONFIRM, CRASH, DAY, FRESH, ORIGIN, SMA_WINDOW, TRAIL, Model
from spotquant.types import Blocked


def bar(i, close, high=None, low=None):
    close = D(close)
    return ORIGIN + i * DAY, high or close, low or close, close


class ModelTests(unittest.TestCase):
    def test_origin_and_contiguity(self):
        model = Model(sma_window=3, trail='0.20')
        with self.assertRaises(Blocked):
            model.update(ORIGIN + DAY, 1, 1, 1)
        model.update(*bar(0, 10))
        model.update(*bar(1, 10))
        self.assertFalse(model.bull)
        model.update(*bar(2, 10))
        self.assertEqual(model.sma, D(10))
        self.assertFalse(model.bull)

    def test_bull_is_strict_and_checkpoint_roundtrips(self):
        model = Model(sma_window=3, trail='0.20')
        for i, close in enumerate((10, 10, 10, 11)):
            model.update(*bar(i, close))
        self.assertTrue(model.bull)
        restored = Model.restore(model.checkpoint())
        self.assertTrue(restored.bull)
        self.assertEqual(restored.sma, model.sma)
        self.assertEqual(restored.last, model.last)
        self.assertEqual(restored.stop_price(D('100')), D('80'))

    def test_defaults_match_the_selected_spot_book(self):
        self.assertEqual(SMA_WINDOW, 40)
        self.assertEqual(TRAIL, D('0.28'))
        self.assertEqual(CONFIRM, 2)
        self.assertEqual(CRASH, D('0.50'))
        self.assertIs(FRESH, True)

    def test_two_closes_arm_entry_and_an_exit_requires_a_fresh_cross(self):
        model = Model(sma_window=2, trail='0.20', crash='0', confirm=2, fresh=True)
        model.update(*bar(0, 10))
        model.update(*bar(1, 10))
        model.update(*bar(2, 12))
        self.assertTrue(model.bull)
        self.assertFalse(model.enter)
        model.update(*bar(3, 14))
        self.assertTrue(model.enter)
        model.note_exit()
        model.update(*bar(4, 16))
        self.assertTrue(model.bull)
        self.assertFalse(model.enter)
        model.update(*bar(5, 10))
        self.assertFalse(model.bull)
        model.update(*bar(6, 12))
        model.update(*bar(7, 14))
        self.assertTrue(model.enter)

    def test_equal_close_is_not_bullish(self):
        model = Model(sma_window=2, trail='0.20')
        model.update(*bar(0, 5))
        model.update(*bar(1, 5))
        self.assertFalse(model.bull)


if __name__ == '__main__':
    unittest.main()
