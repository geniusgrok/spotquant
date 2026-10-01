import tempfile
from decimal import Decimal as D
from unittest import TestCase

from spotquant.offline import Lifecycle, OfflineVenue
from spotquant.state import State
from spotquant.types import Blocked, Unknown

BUY = {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': '100'}


class OfflineTests(TestCase):
    def test_lost_ack_restart_and_partial_fill_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = OfflineVenue()
            venue.fraction, venue.lose_ack = D('.5'), True
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                row = Lifecycle(state, venue).submit(BUY, 1)
                self.assertEqual(row['status'], 'EXPIRED')
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                lifecycle = Lifecycle(state, venue)
                lifecycle.submit(BUY, 1)
                lifecycle.submit({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                                  'quantity': str(venue.btc), 'stopPrice': '72'}, 1)
            self.assertEqual(len(venue.sent), 2)
            venue.trigger('70')
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                Lifecycle(state, venue)
                self.assertEqual(D(state.get('offline_balances')['btc']), 0)
            self.assertEqual(len(venue.sent), 2)

    def test_unconfirmed_send_and_external_funds_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = OfflineVenue()
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                lifecycle = Lifecycle(state, venue)
                venue.unknown_query = True
                with self.assertRaises(Unknown):
                    lifecycle.submit(BUY, 1)
                self.assertEqual(venue.sent, [])
                venue.unknown_query = False
                venue.cash += 1
                with self.assertRaises(Unknown):
                    lifecycle.submit(BUY, 1)
                self.assertEqual(venue.sent, [])

    def test_network_adapter_and_native_state_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with State(directory, 'binance:BTCUSDT:spot:demo:1') as state:
                with self.assertRaises(Blocked):
                    Lifecycle(state, OfflineVenue())

    def test_query_outage_after_fill_never_resubmits_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = OfflineVenue()
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                lifecycle = Lifecycle(state, venue)
                original = venue.submit
                def submit(identity, payload):
                    result = original(identity, payload)
                    venue.unknown_query = True
                    return result
                venue.submit = submit
                with self.assertRaises(Unknown):
                    lifecycle.submit(BUY, 1)
            venue.unknown_query = False
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                Lifecycle(state, venue).submit(BUY, 1)
                self.assertEqual(len(venue.sent), 1)

    def test_stop_failure_and_replacement_do_not_report_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = OfflineVenue()
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                lifecycle = Lifecycle(state, venue)
                lifecycle.submit(BUY, 1)
                venue.reject_stop = True
                stop = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                        'quantity': str(venue.btc), 'stopPrice': '72'}
                with self.assertRaises(Unknown):
                    lifecycle.submit(stop, 1)
                self.assertEqual(len(state.pending()), 1)
