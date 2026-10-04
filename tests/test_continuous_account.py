"""New sizing never defeats mandatory safety or applies the factor twice."""
from decimal import Decimal as D
import unittest
from research.continuous_account import adjusted_factor
from research.flow_risk import DAY, START


class ContinuousAccountChecks(unittest.TestCase):
    def test_only_one_soft_factor_and_required_missing_safety_survives(self):
        spot={START:(D(100),D(101),D(99),D(100),D(10),D(4))}
        coin={START:(D(100),D(101),D(99),D(100),D(10),D(6))}
        now=START+DAY+60000
        self.assertEqual(adjusted_factor(D('.5'),'candidate',spot,coin,now,D('.95'))[0],D('.375'))
        self.assertEqual(adjusted_factor(D(0),'candidate',spot,coin,now,D('.95'))[0],D(0))
        self.assertEqual(adjusted_factor(D('.5'),'candidate',spot,{},now,D('.95'))[0],D('.5'))
