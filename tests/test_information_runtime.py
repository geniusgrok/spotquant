"""Real legal BUY resizing retains mandatory gates, minimums and ownership."""
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from research.information_runtime import configured
from spotquant import session
from spotquant.model import DAY
from spotquant.state import State
from spotquant.types import Blocked, Unknown
from test_alpha_spot import views_at
import test_incremental_information as information_fixtures
from test_participation import Features


def admission(book):
    folder = Path(__file__).parents[1]/'research'
    result = dict(input_sha256=book.input_sha256, spec_sha256='b'*64,
        assessments={'spot':dict(status='ELIGIBLE_FINITE_ACCOUNT_PROPOSAL', account_entrant=True, gates={'fixture':True})},
        source_sha256={str(folder/name):hashlib.sha256((folder/name).read_bytes()).hexdigest()
            for name in ('incremental_information.py', 'edge_features.py', 'continuous_routes.py', 'tradeoff_routes.py', 'replacement_routes.py')})
    binding = dict(information_result_sha256=hashlib.sha256((json.dumps(result, indent=2)+'\n').encode()).hexdigest(),
                   information_spec_sha256=result['spec_sha256'])
    return result, binding


class InformationRuntimeTests(unittest.TestCase):
    def context(self, cause=None):
        book = information_fixtures.IncrementalInformationTests().book()
        book.at = lambda now:dict(status='FEATURE_READY', release=False, day_ms=now//DAY*DAY-DAY)
        source = Features(cause=cause)
        book.features = source
        result, binding = admission(book)
        views = views_at()
        venue = SimpleNamespace(offline=True, now_ms=views[30].last+DAY+60000)
        return configured('release-full-otherwise-half', book=book, binding=binding, admission=result,
                          venue=venue, features=source), views

    def test_actual_legal_buy_halves_once_and_keeps_minimum_ceiling_and_unknown_ownership(self):
        context, views = self.context()
        snapshot = dict(usdt_free='1000', usdt_locked='0', btc='0', avg_price='100', open_orders=0)
        with context as selected:
            def decide(snap=snapshot, cap=D(300)):
                return selected.policy(views, {w:D(0) for w in views}, snap,
                    positions={}, owners={}, entries_enabled=True, capital_limit=cap)
            self.assertEqual(D(decide()['orders'][0]['quoteOrderQty']), D(150))
            self.assertEqual(decide(dict(snapshot, open_orders=1))['orders'], [])
            self.assertEqual(decide(cap=D(8))['orders'], [])
            with self.assertRaises(Unknown):
                decide(dict(snapshot, btc='1'))

    def test_missing_original_mandatory_information_stays_blocked(self):
        context, views = self.context(cause='stale_funding')
        with context as selected:
            result = selected.policy(views, {w:D(0) for w in views},
                dict(usdt_free='1000', usdt_locked='0', btc='0', avg_price='100', open_orders=0),
                positions={}, owners={}, entries_enabled=True, capital_limit=D(300))
            self.assertEqual(result['orders'], [])
            self.assertFalse(any(row['event']=='information-new-buy-budget' for row in selected.policy.journal))

    def test_identity_mismatch_refuses_before_recovery(self):
        context, _ = self.context()
        with tempfile.TemporaryDirectory() as directory, State(directory, 'spot-info-test') as state:
            state.set('edge_identity', {'foreign':True})
            with context:
                with self.assertRaises(Blocked):
                    session._guard_state(state)
            self.assertEqual(state.get('edge_identity'), {'foreign':True})


if __name__ == '__main__':
    unittest.main()
