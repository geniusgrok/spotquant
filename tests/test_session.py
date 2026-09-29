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
    def __init__(self, bars):
        self.bars = list(bars)
        self.environment = 'live'
        self.capital_limit = None
        self.orders_sent = 0
        self.trade_rows = []

    def clock(self):
        return 1_700_000_000.0

    def snapshot(self, uid):
        return {
            'account_uid': uid,
            'btc': D(0),
            'usdt_free': D('1000'),
            'usdt_locked': D(0),
            'open_orders': 0,
            'environment': 'live',
        }

    def completed_daily(self, after):
        if after is None:
            return list(self.bars)
        return [bar for bar in self.bars if bar[0] > after]

    def trades(self, since):
        return [row for row in self.trade_rows if row['time'] >= since]

    def place_order(self, *args, **kwargs):
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
        self.assertEqual(third['qualification'], 'NOT_QUALIFIED')
        self.assertEqual(venue.orders_sent, 0)

    def test_external_btc_stops_the_observation(self):
        venue = Venue(bars(252, 100))
        venue.snapshot = lambda uid: {
            'account_uid': uid, 'btc': D('1'), 'usdt_free': D('1000'),
            'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live',
        }
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            report = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(report['status'], 'unknown')
        self.assertIn('no recorded spotquant fill', report['reason'])
        self.assertNotIn('model_bull', report)
        self.assertNotIn('model_preview', report)

    def test_a_followed_buy_previews_the_fill_stop_and_a_failed_cycle_drops_the_old_view(self):
        venue = Venue(bars(252, 100))
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            venue.bars.append((ORIGIN + 252 * DAY, D(110), D(100), D(110)))
            run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            venue.bars.append((ORIGIN + 253 * DAY, D(111), D(100), D(111)))
            armed = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(armed['model_preview']['action'], 'enter')
            venue.snapshot = lambda uid: {
                'account_uid': uid, 'btc': D('0.05'), 'usdt_free': D('0'),
                'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live',
            }
            venue.trade_rows = [{
                'time': ORIGIN + 254 * DAY + 60_000,
                'qty': D('0.05'),
                'quote': D('5.55'),
                'buyer': True,
                'commission': D('0'),
                'commission_asset': 'BNB',
            }]
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(held['status'], 'read_only')
            self.assertTrue(held['followed_position'])
            self.assertEqual(held['model_preview']['action'], 'hold')
            # 111 * 0.72. The bullish-streak high is not the anchor.
            self.assertEqual(held['model_preview']['protection']['stopPrice'], '79.92')
            self.assertIn('since the fill', held['model_preview']['reason'])
            venue.snapshot = lambda uid: (_ for _ in ()).throw(Unknown('feed broke'))
            failed = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(failed['status'], 'unknown')
        self.assertNotIn('model_bull', failed)
        self.assertNotIn('model_preview', failed)
        self.assertFalse(failed['recorded_limits']['adverse_loss_capped'])
        self.assertFalse(failed['recorded_limits']['skip_stress_targets_met'])


if __name__ == '__main__':
    unittest.main()
