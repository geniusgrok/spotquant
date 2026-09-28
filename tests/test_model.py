"""Causal daily regime: warmup, delay, checkpoint, and no same-bar lookahead."""
from decimal import Decimal as D
import unittest

from spotquant.model import (
    ADVERSE, CAP_BOUNCE, CAP_DEPTH, CAP_DROP, CAP_HAND, CAP_WINDOW, CONFIRM, CRASH, DAY,
    EXTEND, FRESH, ORIGIN, SMA_WINDOW, TRAIL, Model,
)
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
        self.assertEqual(EXTEND, D('0.60'))
        self.assertEqual(CAP_DROP, D('0.08'))
        self.assertEqual(CAP_BOUNCE, D('0.06'))
        self.assertEqual(CAP_DEPTH, D('0.50'))
        self.assertEqual(CAP_HAND, D('0.20'))
        self.assertEqual(CAP_WINDOW, 400)
        self.assertEqual(ADVERSE, D('0.04'))

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

    def test_a_close_sixty_percent_above_the_average_is_a_blowoff(self):
        model = Model(sma_window=2, trail='0.20', crash='0', confirm=1, fresh=False, cap_drop='0')
        model.update(*bar(0, 10))
        model.update(*bar(1, 10))
        model.update(*bar(2, 40))
        self.assertTrue(model.bull)
        self.assertTrue(model.extended)
        restored = Model.restore(model.checkpoint())
        self.assertTrue(restored.extended)

    def test_crash_reversal_arms_and_holds_until_the_handoff(self):
        model = Model(
            sma_window=2, trail='0.28', crash='0', confirm=1, fresh=True,
            cap_window=4, cap_drop='0.10', cap_bounce='0.10', cap_depth='0.50', cap_hand='0.20',
        )
        for i, close in enumerate((20, 20, 20, 20)):
            model.update(*bar(i, close))
        self.assertFalse(model.cap_enter)
        model.update(*bar(4, 8))
        self.assertFalse(model.cap_enter)
        model.update(*bar(5, 10))
        self.assertTrue(model.cap_enter)
        model.note_cap_entry()
        model.update(*bar(6, 11, high=12))
        self.assertTrue(model.repair)
        self.assertEqual(model.repair_peak, D(12))
        # Back above the average and within 20% of the 4-day high releases the hold.
        model.update(*bar(7, 20))
        self.assertFalse(model.repair)
        self.assertIsNone(model.repair_peak)

    def test_a_close_four_percent_under_the_fill_invalidates_a_normal_entry(self):
        model = Model(sma_window=2, trail='0.28', crash='0', confirm=1, fresh=False, cap_drop='0')
        model.update(*bar(0, 100))
        model.update(*bar(1, 100))
        model.note_entry(D('100'))
        model.update(*bar(2, 97))
        self.assertFalse(model.adverse)
        model.update(*bar(3, 96))
        self.assertTrue(model.adverse)
        restored = Model.restore(model.checkpoint())
        self.assertTrue(restored.adverse)
        self.assertEqual(restored.entry, D('100'))
        model.note_exit()
        self.assertIsNone(model.entry)
        self.assertFalse(model.adverse)

    def test_a_crash_reversal_ignores_the_four_percent_close(self):
        model = Model(sma_window=2, trail='0.28', crash='0', confirm=1, fresh=False, cap_drop='0')
        model.update(*bar(0, 100))
        model.update(*bar(1, 100))
        model.note_entry(D('100'))
        model.note_cap_entry()
        model.update(*bar(2, 90))
        self.assertTrue(model.repair)
        self.assertFalse(model.adverse)

    def test_equal_close_is_not_bullish(self):
        model = Model(sma_window=2, trail='0.20')
        model.update(*bar(0, 5))
        model.update(*bar(1, 5))
        self.assertFalse(model.bull)


if __name__ == '__main__':
    unittest.main()
