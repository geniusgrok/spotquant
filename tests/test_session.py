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
        self.trade_since = []

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
            'environment': 'live',
        }

    def completed_daily(self, after):
        if after is None:
            return list(self.bars)
        return [bar for bar in self.bars if bar[0] > after]

    def trades(self, since):
        self.trade_since.append(since)
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
        self.assertEqual(third['model_preview']['order']['sleeves'], [30, 40, 50])
        self.assertEqual(third['model_preview']['order']['quoteOrderQty'], '999.99')
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
                'account_uid': uid, 'btc': D('0.6'), 'usdt_free': D('0'),
                'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live',
            }
            venue.trade_rows = [{
                'id': 1,
                'time': ORIGIN + 254 * DAY + 60_000,
                'qty': D('0.6'),
                'quote': D('66.6'),
                'buyer': True,
                'order_id': 1,
                'commission': D('0'),
                'commission_asset': 'BNB',
            }]
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(held['status'], 'read_only')
            self.assertTrue(held['followed_position'])
            self.assertEqual(held['followed_sleeves'], [30, 40, 50])
            self.assertEqual(held['model_preview']['action'], 'hold')
            # 111 * 0.72 for each sleeve. The bullish-streak high is not the anchor.
            self.assertEqual(held['model_preview']['protections'][0]['stopPrice'], '79.92')
            self.assertEqual(held['model_preview']['protections'][0]['sleeves'], [30, 40, 50])
            self.assertIn('since the fill', held['model_preview']['sleeves']['40']['reason'])
            venue.snapshot = lambda uid: (_ for _ in ()).throw(Unknown('feed broke'))
            failed = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(failed['status'], 'unknown')
        self.assertNotIn('model_bull', failed)
        self.assertNotIn('model_preview', failed)
        self.assertNotIn('followed_position', failed)
        self.assertNotIn('followed_sleeves', failed)
        self.assertFalse(failed['recorded_limits']['adverse_loss_capped'])
        self.assertFalse(failed['recorded_limits']['skip_stress_targets_met'])

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
            'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live',
        }
        venue.trade_rows = [{
            'id': 1, 'time': ORIGIN + 254 * DAY + 60_000, 'qty': D('0.6'), 'quote': D('66.6'),
            'buyer': True, 'order_id': 1, 'commission': D('0'), 'commission_asset': 'BNB',
        }]
        held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertTrue(held['followed_position'])
        return config, venue

    def test_a_full_transfer_out_without_a_sell_is_unknown_and_drops_the_followed_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._held_venue(directory)
            venue.snapshot = lambda uid: {
                'account_uid': uid, 'btc': D(0), 'usdt_free': D(0),
                'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live',
            }
            report = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(report['status'], 'unknown')
        self.assertIn('does not match the recorded', report['reason'])
        self.assertNotIn('followed_position', report)
        self.assertNotIn('model_preview', report)

    def test_a_sell_on_the_account_closes_the_followed_sleeves(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._held_venue(directory)
            venue.bars.append((ORIGIN + 254 * DAY, D(111), D(50), D(50)))
            venue.snapshot = lambda uid: {
                'account_uid': uid, 'btc': D(0), 'usdt_free': D('66'),
                'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live',
            }
            venue.trade_rows.append({
                'id': 2, 'time': ORIGIN + 255 * DAY + 60_000, 'qty': D('0.6'), 'quote': D('30'),
                'buyer': False, 'order_id': 2, 'commission': D('0'), 'commission_asset': 'BNB',
            })
            report = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(report['status'], 'read_only')
        self.assertFalse(report['followed_position'])
        self.assertEqual(report['model_preview']['action'], 'flat')

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
        # The 150 high arrived while the preview failed and still lifts the stop: 150 * 0.72.
        self.assertEqual(held['model_preview']['protections'][0]['stopPrice'], '108.00')
        self.assertEqual(held['model_preview']['protections'][0]['sleeves'], [30, 40, 50])

    def test_a_flat_account_reads_later_trades_from_the_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._held_venue(directory)
            venue.bars.append((ORIGIN + 254 * DAY, D(111), D(50), D(50)))
            venue.snapshot = lambda uid: {
                'account_uid': uid, 'btc': D(0), 'usdt_free': D('66'),
                'usdt_locked': D(0), 'open_orders': 0, 'orders': [], 'environment': 'live',
            }
            sold_at = ORIGIN + 255 * DAY + 60_000
            venue.trade_rows.append({
                'id': 2, 'time': sold_at, 'qty': D('0.6'), 'quote': D('30'),
                'buyer': False, 'order_id': 2, 'commission': D('0'), 'commission_asset': 'BNB',
            })
            run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            venue.trade_since.clear()
            run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertTrue(venue.trade_since)
        self.assertEqual(min(venue.trade_since), sold_at - DAY)
        # The durable ledger serves the committed cursor; venue reads overlap
        # one day so a late fill at the same millisecond cannot disappear.

    def test_open_order_details_stay_on_the_report(self):
        orders = [{
            'order_id': 9, 'side': 'SELL', 'type': 'STOP_LOSS', 'status': 'NEW',
            'stop_price': '79.92',
        }]
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._held_venue(directory)
            previous = venue.snapshot
            venue.snapshot = lambda uid: dict(previous(uid), open_orders=1, orders=orders)
            report = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(report['status'], 'read_only')
        self.assertEqual(report['actual']['orders'], orders)
        self.assertNotEqual(report['model_preview']['action'], 'enter')

    def test_an_old_checkpoint_does_not_keep_an_already_bullish_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = Venue(bars(252, 100))
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            venue.bars.append((ORIGIN + 252 * DAY, D(200), D(180), D(200)))
            run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            venue.bars.append((ORIGIN + 253 * DAY, D(210), D(190), D(210)))
            armed = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(armed['model_preview']['action'], 'enter')
            from spotquant.state import State
            with State(config.state_dir, config.scope) as state:
                state.set('rule', 'older')
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertNotEqual(held['model_preview']['action'], 'enter')


if __name__ == '__main__':
    unittest.main()
