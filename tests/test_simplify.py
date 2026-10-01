"""A deletion must survive every registered matched scenario."""
from copy import deepcopy
import unittest

from research.simplify import PROFILES, choose


class SimplificationTests(unittest.TestCase):
    def test_a_base_winner_is_rejected_when_its_skip_drawdown_is_worse(self):
        row = {'cost_net_cagr': 0.8, 'continuous_mdd': '0.30', 'final_cny': '500000'}
        results = {name: {'base': deepcopy(row), 'skip': deepcopy(row)} for name in PROFILES}
        results['simple']['base']['final_cny'] = '900000'
        results['simple']['skip']['continuous_mdd'] = '0.31'
        decision = choose(results)
        self.assertNotIn('simple', decision['eligible'])
        self.assertNotEqual(decision['selected'], 'simple')

    def test_delete_all_only_when_every_matched_scenario_passes(self):
        row = {'cost_net_cagr': 0.8, 'continuous_mdd': '0.30', 'final_cny': '500000'}
        results = {name: {'base': dict(row)} for name in PROFILES}
        self.assertEqual(choose(results)['selected'], 'simple')
        for name in PROFILES:
            if name != 'P4':
                results[name]['base']['cost_net_cagr'] = 0.78
        self.assertEqual(choose(results)['selected'], 'P4')
