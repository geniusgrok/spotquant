"""Focused regressions from the second PR26 review; all venues are offline."""
import io
import json
import tempfile
import unittest
from decimal import Decimal as D
from urllib.error import HTTPError
from urllib.parse import urlsplit
from unittest.mock import patch

from spotquant import crowding
from spotquant.binance import Binance
from spotquant.config import Config
from spotquant.execution import Lifecycle
from spotquant.state import State
from spotquant.types import Unknown
from test_binance import _filters
from test_execution import add_day, run_day, venue_before_entry
import test_execution as execution_fixture


class FinalReviewTests(unittest.TestCase):
    def test_deadline_after_settled_buy_recovers_even_without_pending_intents(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            run_day(config, venue)
            add_day(venue, '101')
            save, interrupted = Lifecycle.save, []
            def delayed_observation(lifecycle, identity, payload, status, result):
                save(lifecycle, identity, payload, status, result)
                if status == 'settled' and payload['order']['side'] == 'BUY' and not interrupted:
                    interrupted.append(len(lifecycle.state.pending()))
                    venue.wait(1.2)
                    raise Unknown('one observation failed after the settled buy')
            with patch.object(Lifecycle, 'save', delayed_observation):
                report = run_day(config, venue)
            self.assertEqual(interrupted, [0])
            self.assertTrue(report.get('closeout_attempted'))
            self.assertFalse(report['manual_takeover'])
            self.assertEqual(sum(row['side'] == 'BUY' for row in venue.orders.values()), 1)
            self.assertEqual(sum(row['type'] == 'STOP_LOSS' and row['status'] == 'NEW'
                                 for row in venue.orders.values()), 1)
            self.assertLessEqual(report['elapsed_seconds'], 31)

    def test_confirmed_zero_fill_stop_terminal_gets_a_new_protection_identity(self):
        for terminal in ('EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as directory:
                config, venue = execution_fixture.ExecutionTests().entered(directory)
                original = next(row for row in venue.orders.values()
                                if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW')
                original_id = original['clientOrderId']
                original['status'] = terminal
                report = run_day(config, venue)
                self.assertEqual(report['status'], 'demo_execution')
                self.assertFalse(report['manual_takeover'])
                active = [row for row in venue.orders.values()
                          if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW']
                self.assertEqual(len(active), 1)
                self.assertNotEqual(active[0]['clientOrderId'], original_id)
                self.assertEqual(active[0]['quantity'], original['quantity'])
                self.assertEqual(active[0]['stopPrice'], original['stopPrice'])
                self.assertEqual(venue.sent.count(original_id), 1)
                self.assertEqual(sum(row['side'] == 'BUY' for row in venue.orders.values()), 1)
                with State(directory, config.scope) as state:
                    payload = json.loads(state.db.execute('SELECT payload FROM intents WHERE id=?',
                                                          (active[0]['clientOrderId'],)).fetchone()[0])
                    self.assertEqual(payload['replaced_stop'], original_id)
                self.assertFalse(run_day(config, venue)['manual_takeover'])
                self.assertEqual(len([row for row in venue.orders.values() if row['status'] == 'NEW']), 1)

    def test_public_futures_backoff_is_shared_between_routes_and_survives_cache_refresh(self):
        for status in (418, 429):
            with self.subTest(status=status):
                now, calls = [1700000000000], []
                source = crowding.PublicFeatures(clock=lambda: now[0])
                def limited(url, timeout):
                    calls.append(url)
                    raise HTTPError(url, status, 'limited', {'Retry-After': '300'}, io.BytesIO(b'not JSON'))
                with patch.object(crowding, 'urlopen', side_effect=limited):
                    source.refresh(now[0])
                    self.assertEqual(len(calls), 2)  # One per host, not per URL.
                    now[0] += 61000
                    source.refresh(now[0])
                    self.assertEqual(len(calls), 2)
                    now[0] += 240000
                    source.refresh(now[0])
                    self.assertEqual(len(calls), 4)
                self.assertIsNone(source.value('funding', now[0]))
                self.assertIsNone(source.value('basis', now[0]))

    def test_public_spot_backoff_also_blocks_private_spot_requests(self):
        now, calls = [1700000000000], []
        venue = Binance(key='test-key', secret='test-secret', environment='live',
                        clock=lambda: now[0] / 1000,
                        opener=lambda *args: calls.append(args) or (200, b'{}'))
        venue._offset_ms = 0
        venue._monotonic = lambda: now[0] / 1000
        def limited(url, timeout):
            raise HTTPError(url, 429, 'limited', {'Retry-After': '300'}, io.BytesIO(b'not JSON'))
        with patch.object(crowding, 'urlopen', side_effect=limited):
            venue.crowding_features()
        with self.assertRaisesRegex(Unknown, 'backoff'):
            venue._get('/api/v3/account', signed=True)
        self.assertEqual(calls, [])

    def test_public_backoff_missing_or_invalid_headers_is_bounded_and_host_scoped(self):
        for headers in (None, {}, {'Retry-After': 'NaN'}, {'Retry-After': '-1'}):
            with self.subTest(headers=headers):
                now, calls = [1700000000000], []
                source = crowding.PublicFeatures(clock=lambda: now[0])
                def limited(url, timeout):
                    calls.append(urlsplit(url).netloc)
                    if urlsplit(url).netloc == 'fapi.binance.com':
                        raise HTTPError(url, 429, 'limited', headers, io.BytesIO(b''))
                    response = io.BytesIO(b'[]')
                    response.geturl = lambda: url
                    return response
                with patch.object(crowding, 'urlopen', side_effect=limited):
                    source.refresh(now[0])
                    self.assertEqual(calls, ['fapi.binance.com', 'api.binance.com'])
                    self.assertEqual(source.retry_after, {'fapi.binance.com': now[0] + 60000})

    def test_filter_refresh_drops_removed_limits_but_keeps_explicit_zero_maximum(self):
        info = _filters()
        filters = info['symbols'][0]['filters']
        filters[-1]['maxNotional'] = '10'
        market = dict(filterType='MARKET_LOT_SIZE', minQty='0.01', maxQty='0.02', stepSize='0.01')
        filters.append(market)
        venue = Binance(key='test-key', secret='test-secret', environment='demo',
                        opener=lambda *args: (200, json.dumps(info).encode()))
        venue._filters()
        self.assertEqual(venue.market_max_qty, D('.02'))
        market['maxQty'] = '0.00000000'
        venue._filters()
        self.assertEqual(venue.market_max_qty, D(0))
        filters.remove(market)
        del filters[-1]['maxNotional']
        venue._filters()
        self.assertTrue(all(getattr(venue, field) is None for field in
                            ('market_step', 'market_min_qty', 'market_max_qty', 'max_notional')))


if __name__ == '__main__':
    unittest.main()
