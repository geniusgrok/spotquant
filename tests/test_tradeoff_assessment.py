"""Focused counterexamples for money, clock and adoption semantics."""
from copy import deepcopy
import unittest

from research.tradeoff_assessment import DAY, _daily_metrics, assess_comparison, wallet_metrics, wallet_view


class TradeoffAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.start = 1577836800000  # 2020-01-01 UTC
        self.conditions = {
            'market': 'BTCUSDT perpetual', 'valuation_currency': 'USDT', 'reporting_currency': 'CNY',
            'initial_state': {'kind': 'cold_cash', 'cash_usdt': '100', 'btc': '0', 'pending_intents': []},
            'market_data': 'same frozen market', 'fx': 'same frozen FX', 'cost_model': 'same cost',
            'execution_model': 'same matcher', 'clock': 'same original schedule',
        }
        self.row = {
            'case': 'baseline', 'identity': {'source': 'fixture'}, 'window': ['2020-01-01', '2020-01-05'],
            'initial_cny': '100', 'initial_usdt': '100', 'final_usdt': '110', 'final_cny': '110',
            'return_cny': '.1', 'mdd': '.2', 'fees': '1', 'funding': '0', 'trades': [],
            'price_model': 'fixture proxy', 'complete_finite': True, 'audit': {'passed': True},
            'known_path': True, 'failure': None, 'pending_intents': [], 'session_count': 1,
            'registered_session_count': 1, 'sessions': [{'start_ms': self.start}],
            'daily': [{'stamp_ms': self.start + i * DAY, 'equity_usdt': str(v), 'equity_cny': str(v)}
                      for i, v in enumerate((100, 120, 96, 105, 110))],
        }
        self.benchmark = {self.start + i * DAY: v for i, v in enumerate((100, 110, 90, 95, 101))}

    def view(self, row=None, conditions=None):
        row = deepcopy(row or self.row)
        return wallet_view(row, kind='coin', conditions=conditions or self.conditions,
                           source={'path': row['case'] + '.json', 'sha256': ('a' if row['case'] == 'baseline' else 'b') * 64,
                                   'accepted': True}, original_gate={'status': 'ORIGINAL_FAIL'})

    def candidate(self):
        row = deepcopy(self.row)
        row.update(case='candidate', final_usdt='105', final_cny='105', return_cny='.05', mdd='.02', fees='.5')
        for item, value in zip(row['daily'], (100, 102, 101, 104, 105)):
            item.update(equity_usdt=str(value), equity_cny=str(value))
        return row

    def test_lower_return_keeps_explicit_tradeoff_and_original_failure(self):
        report = assess_comparison(self.view(), self.view(self.candidate()), self.benchmark,
                                   purpose='reduce drawdown', accepted_costs='less upside may be acceptable')
        self.assertEqual(report['status'], 'EXPLICIT_NET_BENEFIT_JUDGMENT_REQUIRED')
        self.assertEqual(report['original_gates']['candidate']['status'], 'ORIGINAL_FAIL')
        self.assertEqual(report['comparison']['pareto']['relation'], 'tradeoff_no_dominance')
        self.assertNotIn('beta_btc_usdt', report['comparison']['pareto']['axes'])

    def test_currency_capital_clock_and_source_cannot_be_disguised(self):
        baseline = self.view()
        for key, value in [('initial_cny', '200'), ('initial_usdt', '200'),
                           ('session_starts_ms', [self.start + 1]), ('source', baseline['source'])]:
            candidate = self.view(self.candidate())
            candidate[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                assess_comparison(baseline, candidate, self.benchmark, purpose='risk', accepted_costs='return')
        conditions = deepcopy(self.conditions)
        conditions['valuation_currency'] = 'CNY'
        with self.assertRaises(ValueError):
            self.view(conditions=conditions)

    def test_missing_duplicate_or_shifted_daily_marks_are_not_silently_matched(self):
        for mode in ('missing', 'duplicate', 'shifted', 'terminal'):
            row = self.candidate()
            if mode == 'missing':
                row['daily'].pop(2)
            elif mode == 'duplicate':
                row['daily'].append(deepcopy(row['daily'][1]))
            elif mode == 'shifted':
                row['daily'][2]['stamp_ms'] += 1
            else:
                row['daily'][-1]['equity_cny'] = '999'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.view(row)

    def test_nonflat_start_and_failed_audit_remain_integrity_failures(self):
        conditions = deepcopy(self.conditions)
        conditions['initial_state']['btc'] = '1'
        with self.assertRaises(ValueError):
            self.view(conditions=conditions)
        row = self.candidate()
        row['audit']['passed'] = False
        with self.assertRaises(ValueError):
            self.view(row)
        row = self.candidate()
        row['daily'][0]['quantity_btc'] = '1'
        with self.assertRaises(ValueError):
            self.view(row)

    def test_unrecovered_drawdown_is_censored_and_beta_uses_usdt(self):
        metrics, _ = _daily_metrics([(i * DAY, v) for i, v in enumerate((100, 120, 90, 100))], 100)
        self.assertTrue(metrics['worst_drawdown_recovery']['right_censored'])
        self.assertIsNone(metrics['worst_drawdown_recovery']['peak_to_recovery_days'])
        candidate = self.view(self.candidate())
        before = wallet_metrics(candidate, self.benchmark)
        # A different CNY FX path must not enter a USDT/BTCUSDT regression.
        candidate['daily'][self.start + DAY]['CNY'] *= 2
        after = wallet_metrics(candidate, self.benchmark)
        self.assertEqual(before['beta_btc_usdt'], after['beta_btc_usdt'])
        self.assertNotEqual(before['daily_by_currency']['CNY']['daily_mdd'], after['daily_by_currency']['CNY']['daily_mdd'])
        with self.assertRaises(ValueError):
            assess_comparison(self.view(), candidate, self.benchmark, purpose='risk', accepted_costs='return')


if __name__ == '__main__':
    unittest.main()
