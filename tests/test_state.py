"""One directory stays bound to one spot account."""
import tempfile
import unittest

from spotquant.state import State, client_id
from spotquant.types import Blocked


class StateTests(unittest.TestCase):
    def test_identity_mismatch_and_client_id_prefix(self):
        self.assertTrue(client_id('10001', 1577836800000, 'buy').startswith('sq-'))
        with tempfile.TemporaryDirectory() as directory:
            with State(directory, 'binance:BTCUSDT:spot:live:10001') as state:
                state.report({'status': 'read_only', 'write_attempted': False})
                self.assertEqual(state.pending(), [])
            with self.assertRaises(Blocked):
                with State(directory, 'binance:BTCUSDT:spot:demo:10001'):
                    pass


if __name__ == '__main__':
    unittest.main()
