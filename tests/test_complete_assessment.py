from copy import deepcopy
from decimal import Decimal as D
import unittest

from research.complete_assessment import attribution, daily_metrics, regression, select_spot
from research.rebuild import START_MS
from spotquant.model import DAY
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

    def test_currency_regressions_use_their_own_btc_returns(self):
        usd_returns = [-.04, -.01, -.02, .02, .03]
        cny_returns = [.01, -.03, .04, -.02, .01]
        usd, cny, curve = 9990., 10000., []
        for i, (a, b) in enumerate(zip(usd_returns, cny_returns)):
            usd, cny = usd * (1 + a), cny * (1 + b)
            curve.append({'day_ms': START_MS + i * DAY, 'equity_usdt': usd,
                          'equity_cny': cny, 'net_btc': 1,
                          'gross_exposure_over_equity': 1})
        result = attribution({'mdd': '.1'}, curve, usd_returns, cny_returns,
                             10000, lambda stamp: D(1))
        self.assertAlmostEqual(result['usdt_btc_regression']['beta_btc'], 1)
        self.assertAlmostEqual(result['cny_btc_regression']['beta_btc'], 1)
        self.assertAlmostEqual(result['cny_btc_regression']['intercept_daily'], 0)
