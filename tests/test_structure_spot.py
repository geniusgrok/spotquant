from decimal import Decimal as D
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research import alpha_spot as alpha
from research.structure_spot import Policy
from spotquant.model import Model


class ProtectionAfterCrowdingTests(unittest.TestCase):
    def test_blocked_entry_drops_only_its_protection_and_preserves_core_and_hold(self):
        policy = Policy.__new__(Policy)
        policy.venue = SimpleNamespace(now_ms=0)
        policy.features, policy.journal = object(), []
        policy.filters = {'blocked': 0, 'missing': 0}
        buy = {'side': 'BUY', 'sleeves': [30], 'quoteOrderQty': '10'}
        core = {'side': 'BUY', 'sleeves': [200], 'quoteOrderQty': '5'}
        held = {'side': 'SELL', 'type': 'STOP_LOSS', 'stopPrice': '90', 'quantity': '.1'}
        core_protection = {'side': 'SELL', 'type': 'STOP_LOSS', 'stopPrice': '90', 'sleeves': [200]}
        decision = {'orders': [buy, core], 'sleeves': {
            '30': {'action': 'enter', 'order': buy.copy(), 'protection': {'stopPrice': '90'}},
            '40': {'action': 'hold', 'order': None, 'protection': held},
            '50': {'action': 'flat', 'order': None, 'protection': None},
            '200': {'action': 'enter', 'order': core.copy(), 'protection': core_protection}},
            'protections': [dict(held, sleeves=[30, 40]), core_protection]}
        views = {w: Model(w) for w in (30, 40, 50, 200)}
        for view in views.values():
            view.close = D(100)
        with patch.object(alpha.Policy, '__call__', return_value=decision), patch(
                'research.structure_spot.evaluate', return_value=(D(0), {'blocked_reason': 'missing_causal_input'})):
            result = policy(views, {}, {'avg_price': '100'})
        self.assertEqual(result['orders'], [core])
        self.assertEqual(result['protections'][-1], core_protection)
        self.assertEqual(result['protections'][0]['sleeves'], [40])
        self.assertEqual(D(result['protections'][0]['quantity']), D('.1'))
        self.assertIsNone(result['sleeves']['30']['protection'])
