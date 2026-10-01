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

    def test_stop_failure_does_not_report_protection(self):
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

    def test_sell_proceeds_are_reconciled_before_a_new_buy(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = OfflineVenue()
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                lifecycle = Lifecycle(state, venue)
                lifecycle.submit(BUY, 1)
                lifecycle.submit({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET',
                                  'quantity': str(venue.btc)}, 2)
                with self.assertRaises(Blocked):
                    lifecycle.submit(dict(BUY, quoteOrderQty='1000'), 2)
                self.assertEqual(len(venue.sent), 2)
                lifecycle.submit(dict(BUY, quoteOrderQty=str(venue.cash)), 2)
                self.assertEqual(len(venue.sent), 3)

    def test_unsupported_replacement_keeps_the_prior_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = OfflineVenue()
            with State(directory, 'offline:BTCUSDT:spot:1') as state:
                lifecycle = Lifecycle(state, venue)
                lifecycle.submit(BUY, 1)
                stop = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                        'quantity': str(venue.btc), 'stopPrice': '72'}
                original = lifecycle.submit(stop, 1)
                with self.assertRaises(Blocked):
                    lifecycle.submit(dict(stop, stopPrice='75'), 2)
                self.assertEqual(venue.orders[original['id']]['status'], 'NEW')
                self.assertEqual(len(venue.sent), 2)
