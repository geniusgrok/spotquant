from decimal import Decimal as D
from unittest import TestCase

from research.baselines import decision, passive
from research.rebuild import START_MS
from spotquant.model import DAY


class BaselineTests(TestCase):
    def test_initial_cash_is_conserved_across_partial_monthly_buy(self):
        bars = [(START_MS, D(100), D(100), D(100), D(100), D(1))]
        row = passive(bars, lambda _now: D(1), 'monthly-12', end_ms=START_MS + DAY)
        held = row['daily_positions'][0]
        initial = D(10000) * D('.999')
        self.assertEqual(D(held['cash_usdt']), initial - initial / 12)
        self.assertEqual(len(row['fills']), 1)
        self.assertEqual(D(held['btc']) * D('100.05') + D(row['fees_usdt']) + D(held['cash_usdt']), initial)

    def test_better_return_with_worse_drawdown_does_not_dominate(self):
        p4 = {'cost_net_cagr': .8, 'continuous_mdd': '.3'}
        risky = {'cost_net_cagr': .9, 'continuous_mdd': '.6'}
        results = {name: {'base': risky} for name in ('cash', 'buy-hold', 'monthly-12')}
        results['P4'] = {'base': p4}
        self.assertEqual(decision(results)['dominators'], [])
