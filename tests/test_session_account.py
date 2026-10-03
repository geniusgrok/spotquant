from decimal import Decimal as D
import tempfile
import unittest

from research.complete_spot import Policy, configured
from research.session_account import HistoricalVenue, audit
from spotquant.config import Config
from spotquant.model import DAY, ORIGIN
from spotquant.session import run


def bars():
    prices = [D(100)] * 400 + [D(98), D(101), D(102), D(103), D(104)]
    return [(ORIGIN + i * DAY, p, p, p, p, D(100000)) for i, p in enumerate(prices)]


class SessionAccountTests(unittest.TestCase):
    def test_dispatched_cancel_finishes_at_venue_even_after_client_deadline(self):
        venue = HistoricalVenue(bars(), ORIGIN + 403 * DAY, D(1000), lambda t: D(7))
        venue.submit('buy', {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': '500'})
        venue.submit('stop', {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                              'quantity': str(venue.btc), 'stopPrice': '80'})
        deadline = venue.now_ms + 500
        venue._stop = lambda: venue.now_ms >= deadline
        venue.cancel('stop')
        self.assertEqual(venue.orders['stop']['status'], 'CANCELED')
        self.assertGreater(venue.now_ms, deadline)
    def test_deadline_is_installed_and_prevents_late_client_dispatch(self):
        venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config('1', directory, 1, 1, 'demo', '5000000')
            for day in (401, 402, 403):
                venue.advance(ORIGIN + day * DAY)
                started = venue.now_ms
                run(cfg, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
                for event in venue.client_events:
                    if event['sent_ms'] >= started:
                        self.assertLess(event['sent_ms'], started + 1000)
            self.assertEqual(venue.fills, [])
    def test_candles_are_completed_and_quotes_interpolate_without_future_candles(self):
        venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        rows = venue.completed_daily(None)
        self.assertEqual(rows[-1][0], ORIGIN + 400 * DAY)
        self.assertEqual(venue.price, D(101))

    def test_actual_bounded_sessions_enter_protect_and_independently_audit(self):
        from crowding_fixtures import KnownFeatures
        venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        venue.crowding_features = KnownFeatures
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config('1', directory, 10, 5, 'demo', '5000000')
            for day in (401, 402, 403):
                venue.advance(ORIGIN + day * DAY + 60000)  # Causal basis is now published.
                report = run(cfg, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
                self.assertEqual(report['errors'], [])
            self.assertGreater(venue.btc, 0)
            self.assertTrue(audit(venue)['passed'])
            self.assertTrue(any(o['type'] == 'STOP_LOSS' and o['status'] == 'NEW' for o in venue.orders.values()))

    def test_basis_waits_for_current_date_publication(self):
        venue = HistoricalVenue(bars(), ORIGIN + 401 * DAY, D(1000), lambda t: D(7))
        t = venue.now_ms
        policy = Policy('basis', venue, {'basis': [[t - DAY + 60000, '0'], [t + 60000, '.02']]})
        self.assertIsNone(policy.feature('basis'))
        venue.now_ms += 60000
        self.assertEqual(policy.feature('basis'), D('.02'))

    def test_stop_acts_between_sessions_and_cash_fill_identity_holds(self):
        rows = bars()
        t = ORIGIN + 403 * DAY
        rows[403] = (t, D(100), D(110), D(60), D(100), D(100000))
        venue = HistoricalVenue(rows, t, D(1000), lambda t: D(7))
        venue.submit('test-buy', {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': '500'})
        qty = venue.btc
        venue.submit('test-stop', {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                                  'quantity': str(qty), 'stopPrice': '80'})
        venue.advance(t + 2 * DAY // 3)
        self.assertEqual(venue.btc, 0)
        self.assertEqual(venue.orders['test-stop']['status'], 'FILLED')
        self.assertTrue(audit(venue)['passed'])
