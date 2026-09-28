"""Causal daily regime: warmup, delay, checkpoint, and no same-bar lookahead."""
from decimal import Decimal as D
import unittest

from spotquant.model import DAY, ORIGIN, SMA_WINDOW, TRAIL, Model
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

    def test_defaults_match_the_placeable_grid_choice(self):
        self.assertEqual(SMA_WINDOW, 40)
        self.assertEqual(TRAIL, D('0.20'))

    def test_equal_close_is_not_bullish(self):
        model = Model(sma_window=2, trail='0.20')
        model.update(*bar(0, 5))
        model.update(*bar(1, 5))
        self.assertFalse(model.bull)


if __name__ == '__main__':
    unittest.main()
