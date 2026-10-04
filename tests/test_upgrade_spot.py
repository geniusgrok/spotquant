from decimal import Decimal as D
import unittest

from research.upgrade_spot import model_for
from spotquant.model import ORIGIN, DAY
from spotquant.types import Blocked


class ParticipationTests(unittest.TestCase):
    def warmed(self, candidate):
        cls = model_for(candidate)
        value = cls(30)
        for i in range(410):
            close = D(100)+D(i)/10
            value.update(ORIGIN+i*DAY, close+1, close-1, close)
        return cls, value

    def test_reentry_requires_cooldown_and_new_completed_breakout(self):
        _, value = self.warmed('trend-reentry')
        value.note_exit()
        last, price = value.last, value.close
        for i in range(1, 7):
            value.update(last+i*DAY, price+i+1, price+i-1, price+i)
            self.assertFalse(value.enter)
        value.update(last+7*DAY, price+8, price+6, price+7)
        self.assertTrue(value.enter)
        self.assertFalse(value.need_reset)

    def test_reentry_state_roundtrip_and_foreign_rule_rejection(self):
        cls, value = self.warmed('trend-reentry')
        value.note_exit()
        saved = value.checkpoint()
        restored = cls.restore(saved)
        self.assertEqual(restored.last_exit_day, value.last)
        self.assertEqual(restored.checkpoint(), saved)
        with self.assertRaises(Blocked):
            model_for('slow-participation').restore(saved)

    def test_slow_sleeve_preserves_other_two_windows(self):
        cls = model_for('slow-participation')
        self.assertEqual([cls(w).sma_window for w in (30, 40, 50)], [30, 40, 120])
        with self.assertRaises(ValueError):
            model_for('optimized-window')
