"""Real open clocks share one causal path in replay and repeated sessions."""
from decimal import Decimal as D
import tempfile
import unittest

from spotquant.config import Config
from spotquant.model import DAY, ORIGIN, Model
from spotquant.session import _commit, _load_models
from spotquant.state import State
from spotquant.types import Unknown


class DailyVenue:
    def __init__(self, bars, open_price):
        self.bars = bars
        self.open_price = D(open_price)
        self.now_ms = bars[-1][0] + DAY + 1000
        self.open_calls = 0

    def clock(self):
        return self.now_ms / 1000

    def completed_daily(self, after):
        return [bar for bar in self.bars if after is None or bar[0] > after]

    def daily_open(self, stamp):
        self.open_calls += 1
        return stamp, self.open_price

    def trades(self, since, from_id=None):
        raise AssertionError('price-only history must not query account trades')


def history():
    prices = [100] * 400 + [101, 102]
    return [(ORIGIN + i * DAY, D(p), D(p), D(p), D(p)) for i, p in enumerate(prices)]


class DailyClockTests(unittest.TestCase):
    def test_replay_and_session_tail_use_real_opens_and_restart_is_idempotent(self):
        venue = DailyVenue(history(), 150)
        direct = Model(40)
        for stamp, price, high, low, close in venue.bars:
            direct.advance_open(stamp, price)
            direct.update(stamp, high, low, close)
        direct.advance_open(venue.bars[-1][0] + DAY, 150)
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory)
            with State(config.state_dir, config.scope) as state:
                models, _enabled, fresh = _load_models(state, venue)
                self.assertTrue(fresh)
                self.assertEqual(models[40].checkpoint(), direct.checkpoint())
                self.assertEqual(models[40].shadow_entry, D(150))
                _commit(state, models, {40: None}, {40: None}, fresh, models[40].last)
            # The later ticker/current open response cannot reprice a recorded open.
            venue.open_price = D(175)
            with State(config.state_dir, config.scope) as state:
                models, _enabled, fresh = _load_models(state, venue)
                self.assertFalse(fresh)
                self.assertEqual(models[40].checkpoint(), direct.checkpoint())
                self.assertEqual(venue.open_calls, 1)
                stamp = venue.bars[-1][0] + DAY
                venue.bars.append((stamp, D(150), D(155), D(139), D(140)))
                venue.now_ms = stamp + DAY + 1000
                models, _enabled, _fresh = _load_models(state, venue)
                direct.update(stamp, 155, 139, 140)
                direct.advance_open(stamp + DAY, 175)
                self.assertFalse(models[40].shadow_in)
                self.assertEqual(models[40].checkpoint(), direct.checkpoint())

    def test_uncompleted_daily_hlc_is_rejected_before_model_update(self):
        venue = DailyVenue(history(), 150)
        venue.now_ms = venue.bars[-1][0] + 1000
        # These unfinished values must never enter averages or shadow decisions.
        venue.bars[-1] = (venue.bars[-1][0], D(102), object(), object(), object())
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory)
            with State(config.state_dir, config.scope) as state:
                with self.assertRaisesRegex(Unknown, 'not completed'):
                    _load_models(state, venue)
                self.assertIsNone(state.get('models'))
                self.assertEqual(venue.open_calls, 0)

    def test_missing_or_future_current_open_leaves_the_saved_model_unchanged(self):
        venue = DailyVenue(history(), 150)
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory)
            with State(config.state_dir, config.scope) as state:
                models, _enabled, fresh = _load_models(state, venue)
                _commit(state, models, {40: None}, {40: None}, fresh, models[40].last)
                saved = state.get('models')
                stamp = venue.bars[-1][0] + DAY
                venue.bars.append((stamp, D(150), D(155), D(149), D(154)))
                venue.now_ms = stamp + DAY + 1000
                venue.daily_open = lambda _stamp: (_ for _ in ()).throw(Unknown('missing real daily open'))
                with self.assertRaisesRegex(Unknown, 'missing real daily open'):
                    _load_models(state, venue)
                self.assertEqual(state.get('models'), saved)
                # A venue reporting an open ahead of its clock also fails closed.
                venue.daily_open = lambda _stamp: (_stamp + DAY, D(160))
                with self.assertRaisesRegex(Unknown, 'does not match'):
                    _load_models(state, venue)
                self.assertEqual(state.get('models'), saved)
