"""One directory stays bound to one spot account."""
import tempfile
import unittest
import json
from decimal import Decimal as D

from spotquant.state import State, client_id
from spotquant.types import Blocked, Unknown


class StateTests(unittest.TestCase):
    def test_only_nonterminal_market_orders_and_unsent_risk_actions_are_pending(self):
        with tempfile.TemporaryDirectory() as directory, State(directory, 'test') as state:
            for identity, status, side, order_type, result in (
                    ('sent-buy', 'unknown', 'BUY', 'MARKET', {}),
                    ('open-buy', 'resting', 'BUY', 'MARKET', {}),
                    ('open-stop', 'resting', 'SELL', 'STOP_LOSS', {}),
                    ('unsubmitted-buy', 'prepared', 'BUY', 'MARKET', {}),
                    ('vetoed-buy', 'prepared', 'BUY', 'MARKET', {'not_sent': True}),
                    ('vetoed-stop', 'prepared', 'SELL', 'STOP_LOSS', {'not_sent': True}),
                    ('canceling', 'canceling', 'SELL', 'STOP_LOSS', {})):
                state.db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?)',
                                 (identity, 'p4', json.dumps({'order': {'side': side, 'type': order_type}}),
                                  status, json.dumps(result), 0))
            state.db.commit()
            self.assertEqual({row['id'] for row in state.pending()},
                             {'sent-buy', 'open-buy', 'unsubmitted-buy', 'vetoed-stop', 'canceling'})

    def test_identity_mismatch_and_client_id_prefix(self):
        self.assertTrue(client_id('10001', 1577836800000, 'buy').startswith('sq-'))
        with tempfile.TemporaryDirectory() as directory:
            with State(directory, 'binance:BTCUSDT:spot:live:10001') as state:
                state.report({'status': 'read_only', 'write_attempted': False})
                self.assertEqual(state.pending(), [])
                with self.assertRaises(Blocked):
                    with State(directory, state.identity):
                        pass
            with self.assertRaises(Blocked):
                with State(directory, 'binance:BTCUSDT:spot:demo:10001'):
                    pass


def fill(identity, stamp):
    return {'id': identity, 'order_id': 1, 'time': stamp, 'qty': D('1'),
            'quote': D('100'), 'price': D('100'), 'commission': D('.001'),
            'commission_asset': 'BTC', 'buyer': True}

class Venue:
    def __init__(self):
        self.now = 100000000
        self.fills = [fill(1, self.now)]
        self.starts = []
        self.from_ids = []

    def clock(self):
        return self.now / 1000

    def trades(self, start, from_id=None):
        self.starts.append(start)
        self.from_ids.append(from_id)
        if from_id is None:
            return [x for x in self.fills if x['time'] >= start]
        return [x for x in self.fills if x['id'] >= from_id]

class DurableFillTests(unittest.TestCase):
    def test_overlap_keeps_late_same_time_id_and_avoids_epoch_query(self):
        with tempfile.TemporaryDirectory() as directory, State(directory, 'test') as state:
            venue = Venue()
            self.assertEqual(len(state.trades(venue, 0)), 1)
            venue.fills.append(fill(2, venue.now))
            venue.now += 1000
            self.assertEqual(len(state.trades(venue, 0)), 2)
            self.assertGreater(venue.starts[-1], 0)
            self.assertEqual(len(state.trades(venue, 0)), 2)

    def test_mutation_rolls_back_new_fills_and_watermark(self):
        with tempfile.TemporaryDirectory() as directory, State(directory, 'test') as state:
            venue = Venue()
            state.trades(venue, 0)
            prior = state.get('fill_watermark')
            venue.fills = [fill(2, venue.now), dict(fill(1, venue.now), quote=D(99))]
            with self.assertRaises(Unknown):
                state.trades(venue, 0)
            self.assertEqual(state.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 1)
            self.assertEqual(state.get('fill_watermark'), prior)

    def test_gap_and_reverse_clock_require_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory, State(directory, 'test') as state:
            venue = Venue()
            state.trades(venue, 0)
            venue.now -= 1
            with self.assertRaises(Unknown):
                state.trades(venue, 0)
            venue.now += 81 * 86400000
            kept = state.trades(venue, 0)
            self.assertEqual([row['id'] for row in kept], [1])
            self.assertEqual(venue.from_ids[-1], 1)
            venue.fills.append(fill(2, venue.now))
            self.assertEqual([row['id'] for row in state.trades(venue, 0)], [1, 2])
        with tempfile.TemporaryDirectory() as directory, State(directory, 'test') as state:
            venue = Venue()
            state.set('fill_watermark', {'from_ms': venue.now, 'through_ms': venue.now})
            state.db.execute('DELETE FROM fills')
            venue.now += 81 * 86400000
            with self.assertRaisesRegex(Unknown, 'fill observation gap'):
                state.trades(venue, 0)


if __name__ == '__main__':
    unittest.main()
