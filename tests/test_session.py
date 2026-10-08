"""A bounded session previews and does not record an order intent."""
from decimal import Decimal as D
import tempfile
import unittest

from spotquant.config import Config
from spotquant.model import DAY, ORIGIN
from spotquant.session import run
from spotquant.types import Unknown


class Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return 0 if self.n < 6 else 10


class Venue:
    def crowding_features(self):
        from venue_fixture import KnownFeatures
        return KnownFeatures()

    def __init__(self, bars):
        self.bars = list(bars)
        self.environment = 'live'
        self.capital_limit = None
        self.orders_sent = 0
        self.trade_rows = []

    def clock(self):
        # The observation clock and completed candles share one historical date.
        return (self.bars[-1][0] + DAY + 60_000) / 1000

    def snapshot(self, uid):
        return {
            'account_uid': uid,
            'btc': D(0),
            'usdt_free': D('1000'),
            'usdt_locked': D(0),
            'open_orders': 0,
            'environment': 'live', 'avg_price': self.bars[-1][-1],
        }

    def completed_daily(self, after):
        if after is None:
            return list(self.bars)
        return [bar for bar in self.bars if bar[0] > after]

    def trades(self, since):
        return [row for row in self.trade_rows if row['time'] >= since]

    def submit(self, *args, **kwargs):
        self.orders_sent += 1
        raise AssertionError('session must not place orders')


def bars(count, close):
    return [(ORIGIN + index * DAY, D(close), D(close), D(close)) for index in range(count)]


class SessionTests(unittest.TestCase):
    def test_cold_start_then_two_bullish_closes_preview_entry(self):
        venue = Venue(bars(252, 100))
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            first = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(first['status'], 'read_only')
            self.assertEqual(first['model_preview']['action'], 'flat')
            self.assertEqual(first['write_attempted'], False)
            self.assertEqual(first['pending_intents'], 0)
            self.assertGreaterEqual(first['cycles'], 1)
            venue.bars.append((ORIGIN + 252 * DAY, D(200), D(180), D(200)))
            second = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(second['model_preview']['action'], 'flat')
            venue.bars.append((ORIGIN + 253 * DAY, D(210), D(190), D(210)))
            third = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(third['model_preview']['action'], 'enter')
        self.assertEqual(third['model_preview']['order']['side'], 'BUY')
        self.assertEqual(third['model_preview']['order']['sleeves'], [30, 40, 50])
        self.assertEqual(third['model_preview']['order']['quoteOrderQty'], '999.99')
        self.assertEqual(venue.orders_sent, 0)

    def test_a_followed_buy_previews_the_fill_stop_and_a_failed_cycle_drops_the_old_view(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._held_venue(directory)
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(held['status'], 'read_only')
            self.assertTrue(held['followed_position'])
            self.assertEqual(held['followed_sleeves'], [30, 40, 50])
            self.assertEqual(held['model_preview']['action'], 'hold')
            # ATR clips to 10%; the actual 111 fill is the peak, not the pre-fill wick.
            self.assertEqual(held['model_preview']['protections'][0]['stopPrice'], '99.90')
            self.assertEqual(held['model_preview']['protections'][0]['sleeves'], [30, 40, 50])
            self.assertIn('since the fill', held['model_preview']['sleeves']['40']['reason'])
            observed = venue.snapshot
            venue.snapshot = lambda uid: dict(observed(uid), btc=D(1))
            external = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(external['status'], 'unknown')
            self.assertNotIn('model_preview', external)
            venue.snapshot = lambda uid: (_ for _ in ()).throw(Unknown('feed broke'))
            failed = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(failed['status'], 'unknown')
        self.assertNotIn('model_bull', failed)
        self.assertNotIn('model_preview', failed)
        self.assertNotIn('followed_position', failed)
        self.assertNotIn('followed_sleeves', failed)
        self.assertFalse(failed['recorded_limits']['adverse_loss_capped'])

    def _held_venue(self, directory):
        venue = Venue(bars(252, 100))
        config = Config('10001', directory, session_seconds=2, poll_seconds=1)
        run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        venue.bars.append((ORIGIN + 252 * DAY, D(110), D(100), D(110)))
        run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        venue.bars.append((ORIGIN + 253 * DAY, D(111), D(100), D(111)))
        run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        venue.snapshot = lambda uid: {
            'account_uid': uid, 'btc': D('0.6'), 'usdt_free': D('0'),
            'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live', 'avg_price': venue.bars[-1][-1],
        }
        venue.trade_rows = [{
            'id': 1, 'time': ORIGIN + 254 * DAY + 60_000, 'qty': D('0.6'), 'quote': D('66.6'),
            'buyer': True, 'order_id': 1, 'commission': D('0'), 'commission_asset': 'BNB',
        }]
        held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertTrue(held['followed_position'])
        return config, venue

    def test_a_failed_preview_still_keeps_the_positions_in_step_with_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._held_venue(directory)
            healthy = venue.snapshot
            venue.snapshot = lambda uid: (_ for _ in ()).throw(Unknown('feed broke'))
            venue.bars.append((ORIGIN + 254 * DAY, D(111), D(111), D(111)))
            venue.bars.append((ORIGIN + 255 * DAY, D(150), D(111), D(150)))
            failed = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(failed['status'], 'unknown')
            venue.snapshot = healthy
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(held['status'], 'read_only')
        # The 150 peak and completed true ranges catch up together after failure.
        self.assertEqual(held['model_preview']['protections'][0]['stopPrice'], '132.85')
        self.assertEqual(held['model_preview']['protections'][0]['sleeves'], [30, 40, 50])

    def test_an_old_checkpoint_is_rejected_even_when_flat(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = Venue(bars(252, 100))
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            from spotquant.state import State
            with State(config.state_dir, config.scope) as state:
                state.set('rule', 'older')
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(held['status'], 'blocked')
        self.assertIn('another rule', held['reason'])
        self.assertNotIn('model_preview', held)


if __name__ == '__main__':
    unittest.main()
