from copy import deepcopy
import unittest

from research.complete_assessment import daily_metrics, regression, select_spot
from research.complete_spot import CANDIDATES, SCENARIOS


class CompleteAssessmentTests(unittest.TestCase):
    def accounts(self):
        return {c + '-' + s: {'complete': True, 'audit': {'passed': True}, 'cagr': .7, 'mdd': '.3'}
                for c in CANDIDATES for s in SCENARIOS}

    def test_one_good_base_cannot_hide_a_stress_regression(self):
        results = self.accounts()
        results['consensus-base']['cagr'] = .9
        results['consensus-fee150']['mdd'] = '.31'
        self.assertEqual(select_spot(results)['selected_research_candidate'], 'default')
        results['consensus-fee150']['mdd'] = '.3'
        self.assertEqual(select_spot(results)['selected_research_candidate'], 'consensus')
        results['consensus-slip2']['audit']['passed'] = False
        self.assertEqual(select_spot(results)['selected_research_candidate'], 'default')

    def test_incomplete_baseline_blocks_every_promotion(self):
        results = self.accounts()
        for s in SCENARIOS:
            results['downside-' + s]['cagr'] = .9
        results['default-outage']['complete'] = False
        self.assertEqual(select_spot(results)['selected_research_candidate'], 'default')

    def test_beta_is_distinct_from_residual(self):
        market = [-.04, .01, .03, -.01, .02]
        result = regression([.001 + 2 * v for v in market], market)
        self.assertAlmostEqual(result['beta_btc'], 2)
        self.assertAlmostEqual(result['intercept_daily'], .001)
        self.assertFalse(result['prospective_alpha_proven'])

    def test_drawdown_keeps_the_initial_budget_in_the_peak(self):
        result, _ = daily_metrics([75, 80, 100, 90], 100)
        self.assertEqual(result['daily_mdd'], .25)
        self.assertEqual(result['longest_daily_underwater_days'], 2)
