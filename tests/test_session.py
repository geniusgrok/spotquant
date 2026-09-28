"""A bounded session previews and does not record an order intent."""
from decimal import Decimal as D
import tempfile
import unittest

from spotquant.config import Config
from spotquant.model import DAY, ORIGIN
from spotquant.session import run


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

    def place_order(self, *args, **kwargs):
        self.orders_sent += 1
        raise AssertionError('session must not place orders')


def bars(count, close):
    return [(ORIGIN + index * DAY, D(close), D(close), D(close)) for index in range(count)]


class SessionTests(unittest.TestCase):
    def test_cold_start_then_a_new_bullish_close_previews_entry(self):
        venue = Venue(bars(40, 100))
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            first = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(first['status'], 'read_only')
            self.assertEqual(first['model_preview']['action'], 'flat')
            self.assertEqual(first['write_attempted'], False)
            self.assertEqual(first['pending_intents'], 0)
            self.assertGreaterEqual(first['cycles'], 1)
            venue.bars.append((ORIGIN + 40 * DAY, D(200), D(180), D(200)))
            second = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(second['model_preview']['action'], 'enter')
        self.assertEqual(second['model_preview']['order']['side'], 'BUY')
        self.assertEqual(second['qualification'], 'NOT_QUALIFIED')
        self.assertEqual(venue.orders_sent, 0)

    def test_external_btc_stops_the_observation(self):
        venue = Venue(bars(40, 100))
        venue.snapshot = lambda uid: {
            'account_uid': uid, 'btc': D('1'), 'usdt_free': D('1000'),
            'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live',
        }
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            report = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(report['status'], 'unknown')
        self.assertIn('no recorded spotquant fill', report['reason'])


if __name__ == '__main__':
    unittest.main()
