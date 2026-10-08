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
        model.update(ORIGIN + index * DAY, 100, 100, 100)
    return model


def close(model, price, high=None):
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
        blowoff = warmed()
        close(blowoff, 500)
        self.assertTrue(Model.restore(blowoff.checkpoint()).extended)

    def test_rehashed_inconsistent_checkpoint_cannot_change_runtime_state(self):
        model = warmed()
        close(model, 101)
        for key, value in (('crash_ok', False), ('cap_enter', True), ('prev_close', '99')):
            saved = model.checkpoint()
            saved['body'][key] = value
            saved['sha256'] = hashlib.sha256(json.dumps(saved['body'], sort_keys=True).encode()).hexdigest()
            with self.subTest(key=key), self.assertRaises(Blocked):
                Model.restore(saved)
