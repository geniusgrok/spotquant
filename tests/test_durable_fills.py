from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from research.restore_check import check
from spotquant.state import State
from spotquant.types import Unknown


def fill(identity, stamp):
    return {'id': identity, 'order_id': 1, 'time': stamp, 'qty': D('1'),
            'quote': D('100'), 'price': D('100'), 'commission': D('.001'),
            'commission_asset': 'BTC', 'buyer': True}


class Venue:
    def __init__(self):
        self.now = 100000000
        self.fills = [fill(1, self.now)]
        self.starts = []

    def clock(self):
        return self.now / 1000

    def trades(self, start):
        self.starts.append(start)
        return [x for x in self.fills if x['time'] >= start]


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
            with self.assertRaises(Unknown):
                state.trades(venue, 0)

    def test_backup_reopens_read_only_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            with State(directory, 'test') as state:
                state.set('execution_anchor', {'cash': '100', 'btc': '0', 'at_ms': 1})
                state.trades(Venue(), 0)
                saved = state.archive({'session_started_at_ms': 1})
                second = state.archive({'session_started_at_ms': 1})
                self.assertNotEqual(saved['report'], second['report'])
            proof = check(saved['report'])
            self.assertTrue(proof['execution_anchor_preserved'])
            self.assertFalse(proof['new_risk_authorized'])
            with open(saved['backup'], 'ab') as stream:
                stream.write(b'x')
            with self.assertRaises(ValueError):
                check(saved['report'])
