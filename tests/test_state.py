"""One directory stays bound to one spot account."""
import tempfile
import unittest

from spotquant.state import OBSERVATION_LIMIT, State, client_id
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

    def test_the_observation_table_keeps_only_the_latest_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            with State(directory, 'binance:BTCUSDT:spot:live:10001') as state:
                for index in range(OBSERVATION_LIMIT + 25):
                    state.report({'status': 'read_only', 'cycle': index})
                rows = state.db.execute('SELECT COUNT(*), MAX(sequence) FROM observations').fetchone()
                self.assertEqual(rows[0], OBSERVATION_LIMIT)
                self.assertEqual(rows[1], OBSERVATION_LIMIT + 25)


if __name__ == '__main__':
    unittest.main()
