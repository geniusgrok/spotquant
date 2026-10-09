"""Offline venue restart and entry-dispatch regressions from the PR review."""
import io
import tempfile
from decimal import Decimal as D
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from spotquant import crowding
from spotquant.binance import Binance
from spotquant.config import Config
from spotquant.state import State
from spotquant.types import Blocked, NotSent, Unknown
import test_execution as execution_fixture
import test_preview as preview_fixture


class AdapterRecoveryTests(TestCase):
    def test_buy_cannot_cross_its_deadline_while_the_request_is_signed(self):
        for stop_requested in (False, True):
            with self.subTest(stop_requested=stop_requested):
                account = execution_fixture.venue_before_entry()
                venue = execution_fixture.OrderAdapter(account, None)
                ticks = [0]
                venue._monotonic = lambda: ticks[0]
                venue._stop = lambda: ticks[0] >= 31
                venue._risk_stop = lambda: ticks[0] >= (2 if stop_requested else 1)
                def signing_timestamp():
                    ticks[0] = 2
                    return account.now_ms
                venue._timestamp = signing_timestamp
                with self.assertRaisesRegex(NotSent, 'entry deadline'):
                    venue.submit('sq-deadline', {'symbol': 'BTCUSDT', 'side': 'BUY',
                                                'type': 'MARKET', 'quoteOrderQty': '100'})
                self.assertEqual(account.sent, [])
                self.assertFalse(venue.write_attempted)

    def test_dispatched_rate_limit_survives_restart_without_reusing_transport(self):
        for status in (418, 429):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                config = Config('10001', directory, environment='demo', capital_limit_usdt='100')
                now, ticks, requests = [1700000000000], [50], []
                def venue(opener):
                    result = Binance(key='test-key', secret='test-secret', environment='demo',
                                     capital_limit=D(100), demo_execution_uid='10001',
                                     clock=lambda: now[0] / 1000, opener=opener)
                    result._monotonic = lambda: ticks[0]
                    result._offset_ms = 0
                    return result
                limited = venue(lambda *args: (status, b'limited', {'Retry-After': '30'}))
                with State(directory, config.scope) as state:
                    limited.bind_state(state)
                    with self.assertRaises(Unknown) as caught:
                        limited._get('/api/v3/order', signed=True, method='POST')
                    self.assertNotIsInstance(caught.exception, NotSent)
                    self.assertTrue(limited.write_attempted)
                    self.assertEqual(state.get('rate_limits'), {'demo-api.binance.com': now[0] + 30000})
                ticks[0] = 0  # A new invocation has a different monotonic origin.
                restored = venue(lambda *args: requests.append(args) or (200, b'{}'))
                with State(directory, config.scope) as state:
                    restored.bind_state(state)
                    with self.assertRaisesRegex(Unknown, 'backoff'):
                        restored._get('/api/v3/account', signed=True)
                    with self.assertRaises(NotSent):
                        restored._get('/api/v3/order', signed=True, method='POST')
                    self.assertEqual(requests, [])
                    self.assertFalse(restored.write_attempted)
                    now[0] += 30000
                    ticks[0] += 30
                    restored._get('/api/v3/account', signed=True)
                    self.assertEqual(len(requests), 1)

    def test_public_futures_backoff_survives_restart_and_keeps_spot_available(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory)
            now, requests = [1700000000000], []
            def limited(url, timeout):
                requests.append(urlsplit(url).netloc)
                if urlsplit(url).netloc == 'fapi.binance.com':
                    raise HTTPError(url, 429, 'limited', {'Retry-After': '120'}, io.BytesIO(b''))
                response = io.BytesIO(b'[]')
                response.geturl = lambda: url
                return response
            def venue():
                result = Binance(key='test-key', secret='test-secret', environment='live',
                                 clock=lambda: now[0] / 1000, opener=lambda *args: (200, b'{}'))
                result._offset_ms = 0
                result._monotonic = lambda: now[0] / 1000
                return result
            with patch.object(crowding, 'urlopen', side_effect=limited):
                with State(directory, config.scope) as state:
                    original = venue()
                    original.bind_state(state)
                    original.crowding_features()
                    self.assertEqual(state.get('rate_limits'), {'fapi.binance.com': now[0] + 120000})
                self.assertEqual(requests, ['fapi.binance.com', 'api.binance.com'])
                requests.clear()
                with State(directory, config.scope) as state:
                    restored = venue()
                    restored.bind_state(state)
                    restored.crowding_features()
                    self.assertEqual(requests, ['api.binance.com'])
                    self.assertEqual(restored._get('/api/v3/account', signed=True), {})

    def test_invalid_saved_backoff_does_not_start_network(self):
        for saved in ([], {'api.binance.com': True}, {'api.binance.com': -1}):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as directory:
                config = Config('10001', directory)
                calls = []
                venue = Binance(key='test-key', secret='test-secret', environment='live',
                                opener=lambda *args: calls.append(args))
                with State(directory, config.scope) as state:
                    state.set('rate_limits', saved)
                    with self.assertRaisesRegex(Blocked, 'persisted rate limit'):
                        venue.bind_state(state)
                self.assertEqual(calls, [])

    def test_oversized_retry_after_uses_durable_default_before_any_retry(self):
        for retry in ('1e1000', '1e307'):
            with self.subTest(retry=retry), tempfile.TemporaryDirectory() as directory:
                config = Config('10001', directory)
                now, calls = 1700000000000, []
                venue = Binance(key='test-key', secret='test-secret', environment='live',
                                clock=lambda: now / 1000,
                                opener=lambda *args: calls.append(args) or (429, b'', {'Retry-After': retry}))
                venue._monotonic = lambda: 10
                with State(directory, config.scope) as state:
                    venue.bind_state(state)
                    with self.assertRaisesRegex(Unknown, 'rate limit'):
                        venue._get('/api/v3/exchangeInfo', signed=False)
                    with self.assertRaisesRegex(Unknown, 'backoff'):
                        venue._get('/api/v3/exchangeInfo', signed=False)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(state.get('rate_limits'), {'api.binance.com': now + 60000})
                    saved = []
                    source = crowding.PublicFeatures(clock=lambda: now,
                        on_rate_limit=lambda host, delay: saved.append((host, delay)))
                    def public(url, timeout):
                        raise HTTPError(url, 429, 'limited', {'Retry-After': retry}, io.BytesIO(b''))
                    with patch.object(crowding, 'urlopen', side_effect=public):
                        source.refresh(now)
                    self.assertEqual(saved, [('fapi.binance.com', 60), ('api.binance.com', 60)])

    def test_overextended_early_signal_does_not_buy_then_immediately_sell(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = execution_fixture.venue_before_entry()
            execution_fixture.run_day(config, venue)
            execution_fixture.add_day(venue, '200')
            report = execution_fixture.run_day(config, venue)
            self.assertEqual(report['errors'], [])
            self.assertEqual(venue.sent, [])
            self.assertEqual(venue.cash, D(1000))
            self.assertEqual(venue.btc, D(0))
            self.assertEqual(report['model_preview']['action'], 'flat')
            self.assertIn('overextended', report['model_preview']['reason'])

    def test_repair_entry_keeps_its_existing_exception_to_overextension(self):
        # A crash-reversal repair can be above a deeply depressed SMA while
        # still far below its historical high; its existing hold rule applies.
        view = preview_fixture.model((D('10'),) * 40 + (D('25'), D('22'), D('24')))
        self.assertTrue(view.cap_enter)
        self.assertTrue(view.extended)
        result = preview_fixture.portfolio({40: view}, {}, preview_fixture.snapshot(price='24'),
                                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'enter')
        self.assertTrue(result['sleeves']['40']['repair'])
