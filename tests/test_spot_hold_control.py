"""Money, ownership and clock counterexamples for the offline hold reference."""
from copy import deepcopy
from decimal import Decimal as D
from unittest import TestCase

from research.spot_hold_control import CLOCK, DAY, measure, stamp, window_bars


class SpotHoldControlTests(TestCase):
    def setUp(self):
        self.begin = stamp('2020-04-01')
        self.end = self.begin + 3 * DAY
        self.start = self.begin + 3600000
        self.packet = {'bars': {str(self.begin + i * DAY):
            dict(open='100', high='120', low='20', close='80') for i in range(-1, 3)}}
        self.bars = window_bars(self.packet, self.begin, self.end)
        self.registration = dict(policies=['hold25', 'cash0'], clock=dict(CLOCK,
            first_registered_start_ms={'2020-04-01': self.start}))

    def run_wallet(self, policy):
        return measure(policy, self.bars, [self.start], lambda _: D(1),
            begin=self.begin, end=self.end, registration=self.registration,
            identity={'specification_sha256': 'a' * 64})

    def test_actual_filled_btc_is_held_through_large_drop_with_original_costs(self):
        row = self.run_wallet('hold25')
        trade = row['fills'][0]
        net = D(trade['qty']) - D(trade['commission'])
        self.assertEqual(D(row['entry_spend_usdt']), D('2497.50'))
        self.assertEqual(D(row['cash_usdt']), D('7492.50'))
        self.assertEqual(D(row['btc']), net)
        self.assertEqual(trade['commission_asset'], 'BTC')
        self.assertLessEqual(abs(D(trade['commission']) - D(trade['qty']) * D('.001')), D('1e-20'))
        self.assertTrue(all(D(r['btc']) == net for r in row['daily'][1:]))
        self.assertEqual(len(row['client_events']), 1)
        self.assertEqual(row['client_events'][0]['received_ms'], self.start + 1200)
        self.assertEqual(row['ownership'][0]['filled_ms'], self.start + 1200)
        self.assertTrue(row['audit']['passed'])
        self.assertFalse(row['protection_acceptance'])
        self.assertFalse(row['production_candidate'])

    def test_independent_cash_wallet_does_not_inherit_hold_fills_or_commissions(self):
        held = self.run_wallet('hold25')
        row = self.run_wallet('cash0')
        self.assertNotEqual(row['case'], held['case'])
        self.assertEqual(row['fills'], [])
        self.assertEqual(row['ownership'], [])
        self.assertEqual(row['client_events'], [])
        self.assertEqual(row['btc'], '0')
        self.assertEqual(D(row['final_usdt']), D('9990'))
        self.assertEqual(D(row['final_cny']), D('9980.01'))
        self.assertEqual(row['audit']['fees_usdt'], '0')
        self.assertTrue(row['complete_finite'])

    def test_actual_midnight_marks_and_terminal_close_do_not_use_next_day(self):
        self.packet['bars'][str(self.end)] = dict(open='99999', high='99999', low='99999', close='99999')
        self.bars = window_bars(self.packet, self.begin, self.end)
        row = self.run_wallet('hold25')
        self.assertEqual([r['timestamp_ms'] for r in row['daily']],
                         list(range(self.begin, self.end + DAY, DAY)))
        self.assertEqual(row['daily'][-1]['price_usdt'], '80')
        self.assertEqual(row['daily'][0]['btc'], '0')
        self.assertFalse(row['decision_clock']['shared_session_equivalent'])
        self.assertEqual(row['sessions'], [])

    def test_missing_input_and_changed_registered_entry_clock_reject(self):
        packet = deepcopy(self.packet)
        del packet['bars'][str(self.begin + DAY)]
        with self.assertRaisesRegex(ValueError, 'coverage'):
            window_bars(packet, self.begin, self.end)
        self.registration['clock']['first_registered_start_ms']['2020-04-01'] += 1
        with self.assertRaisesRegex(ValueError, 'preregistration'):
            self.run_wallet('hold25')

    def test_no_synthetic_boundary_before_late_registered_entry(self):
        self.start = self.begin + DAY + 3600000
        self.registration['clock']['first_registered_start_ms']['2020-04-01'] = self.start
        row = self.run_wallet('hold25')
        self.assertEqual(row['daily'][1]['btc'], '0')
        self.assertGreater(D(row['daily'][2]['btc']), 0)
        self.assertEqual(row['client_events'][0]['sent_ms'], self.start + 200)
