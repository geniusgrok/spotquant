"""Current daily regime, fill-based risk and checkpoint continuity."""
from decimal import Decimal as D
import hashlib
import json
import unittest

from spotquant.model import DAY, ORIGIN, Model
from spotquant.types import Blocked


def warmed(window=30):
    model = Model(window)
    for index in range(400):
        model.advance_open(ORIGIN + index * DAY, 100)
        model.update(ORIGIN + index * DAY, 100, 100, 100)
    return model


def close(model, price, high=None, open_price=None):
    model.advance_open(model.last + DAY, price if open_price is None else open_price)
    model.update(model.last + DAY, high or price, price, price)


class ModelTests(unittest.TestCase):
    def test_origin_contiguity_strict_bull_and_checkpoint(self):
        model = Model(30)
        with self.assertRaises(Blocked):
            model.update(ORIGIN + DAY, 100, 100, 100)
        for index in range(30):
            model.update(ORIGIN + index * DAY, 100, 100, 100)
        self.assertFalse(model.bull)
        with self.assertRaises(Blocked):
            model.update(model.last + 2 * DAY, 100, 100, 100)
        close(model, 101)
        self.assertTrue(model.bull)
        restored = Model.restore(model.checkpoint())
        self.assertEqual(restored.checkpoint(), model.checkpoint())
        self.assertFalse(Model.restore(Model().checkpoint()).bull)

    def test_two_closes_and_fresh_cross_after_exit(self):
        model = warmed()
        close(model, 98)
        close(model, 101)
        self.assertFalse(model.enter)
        close(model, 102)
        self.assertTrue(model.enter)
        model.note_flat()
        close(model, 103)
        self.assertFalse(model.enter)
        close(model, 98)
        close(model, 101)
        close(model, 102)
        self.assertTrue(model.enter)
        model.note_flat()
        self.assertFalse(model.enter)
        self.assertTrue(Model.restore(model.checkpoint()).need_reset)
        touched = warmed()
        close(touched, 101)
        close(touched, 102)
        self.assertTrue(touched.enter)
        self.assertFalse(touched.shadow_in)
        close(touched, 103)
        self.assertTrue(touched.shadow_in)
        touched.note_flat(rearm=True)
        self.assertTrue(touched.enter)
        self.assertFalse(touched.need_reset)
        blowoff = warmed()
        close(blowoff, 500)
        self.assertTrue(Model.restore(blowoff.checkpoint()).extended)

    def test_signal_executes_at_the_next_observed_open_without_its_hlc(self):
        model = warmed()
        close(model, 101)
        close(model, 102)
        signal_day = model.last
        self.assertTrue(model.enter)
        self.assertFalse(model.shadow_in)
        self.assertTrue(model.advance_open(signal_day + DAY, 150))
        self.assertTrue(model.shadow_in)
        self.assertEqual(model.shadow_entry, D(150))
        self.assertEqual(model.last, signal_day)
        self.assertEqual(model.close, D(102))
        self.assertFalse(model.shadow_adverse)
        restored = Model.restore(model.checkpoint())
        self.assertEqual(restored.checkpoint(), model.checkpoint())
        self.assertFalse(restored.advance_open(signal_day + DAY, 151))
        self.assertEqual(restored.shadow_entry, D(150))
        restored.update(signal_day + DAY, 155, 139, 140)
        self.assertTrue(restored.shadow_adverse)
        self.assertTrue(restored.shadow_in)
        restored.advance_open(signal_day + 2 * DAY, 137)
        self.assertFalse(restored.shadow_in)
        self.assertIsNone(restored.shadow_entry)

    def test_completed_close_never_invents_an_open(self):
        model = warmed()
        close(model, 101)
        close(model, 102)
        model.update(model.last + DAY, 104, 102, 103)
        self.assertTrue(model.enter)
        self.assertFalse(model.shadow_in)

    def test_rejects_missing_out_of_order_and_invalid_opens(self):
        model = warmed()
        before = model.checkpoint()
        for stamp, price in ((model.last + 2 * DAY, 100), (model.last - DAY, 100),
                             (model.last + DAY + 1, 100), (model.last + DAY, 0),
                             (model.last + DAY, 'NaN')):
            with self.subTest(stamp=stamp, price=price), self.assertRaises(Blocked):
                model.advance_open(stamp, price)
            self.assertEqual(model.checkpoint(), before)

    def test_consumed_shadow_waits_for_a_fresh_signal(self):
        model = warmed()
        close(model, 101)
        close(model, 102)
        model.advance_open(model.last + DAY, 103)
        model.note_flat()
        self.assertEqual(model.shadow_blocked, model.last)
        self.assertTrue(model.shadow_in)
        restored = Model.restore(model.checkpoint())
        self.assertEqual(restored.shadow_blocked, model.last)
        restored.update(restored.last + DAY, 104, 103, 104)
        close(restored, 105)
        self.assertEqual(restored.shadow_blocked, model.last)
        close(restored, 90)
        close(restored, 101)
        self.assertFalse(restored.shadow_in)
        close(restored, 102)
        restored.advance_open(restored.last + DAY, 106)
        self.assertTrue(restored.shadow_in)
        self.assertIsNone(restored.shadow_blocked)

    def test_rehashed_inconsistent_checkpoint_cannot_change_runtime_state(self):
        model = warmed()
        close(model, 101)
        for key, value in (('crash_ok', False), ('cap_enter', True), ('prev_close', '99'),
                           ('shadow_open_ms', model.last - DAY), ('shadow_open_ms', True),
                           ('shadow_blocked', model.last + DAY), ('shadow_blocked', True)):
            saved = model.checkpoint()
            saved['body'][key] = value
            saved['sha256'] = hashlib.sha256(json.dumps(saved['body'], sort_keys=True).encode()).hexdigest()
            with self.subTest(key=key), self.assertRaises(Blocked):
                Model.restore(saved)
