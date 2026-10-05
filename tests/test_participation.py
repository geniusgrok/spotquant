"""Funds and causality boundaries; synthetic fixtures are not market receipts."""
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from research import edge_spot as edge
from research.participation import configured, optional_half
from spotquant import session
from spotquant.model import DAY
from spotquant.state import State
from spotquant.types import Blocked, Unknown
from test_alpha_spot import views_at


class Features:
    sha256 = 'a'*64

    def __init__(self, cause=None, receipt=None):
        self.cause, self.receipt = cause, receipt

    def value(self, name, now):
        observation = now-28800000 if name == 'funding' else now//DAY*DAY
        available = observation+(28800000 if name == 'funding' else 60000)
        self.last_lookup = dict(name=name, value=None if self.cause else '0', cause=self.cause,
                                observation_ms=observation, available_ms=available,
                                receipt_ms=self.receipt or now)
        return None if self.cause else D(0)


class ParticipationBoundaries(unittest.TestCase):
    def test_only_optional_absence_changes_and_keeps_missing_evidence(self):
        now = 10*DAY+60000
        view = SimpleNamespace(last=9*DAY, close=D(100), closes=[D(100)]*6)
        factor, detail = optional_half(None, view, now)
        self.assertEqual(factor, D('.5'))
        self.assertTrue(all(row['value'] is None for row in detail['inputs']))
        self.assertFalse(detail['imputed_features'])
        self.assertEqual(optional_half(Features(), view, now)[0], 1)
        self.assertEqual(optional_half(Features('invalid_feature_value'), view, now)[0], 0)
        self.assertEqual(optional_half(Features('stale_funding', now+1), view, now)[0], 0)
        self.assertEqual(optional_half(None, SimpleNamespace(last=10*DAY, close=D(100),
                                                           closes=[D(100)]*6), now)[0], 0)

    def test_foreign_identity_rejects_before_recovery_and_hooks_restore(self):
        original_guard, original_evaluator = session._guard_state, edge.evaluate
        venue = SimpleNamespace(offline=True)
        with tempfile.TemporaryDirectory() as directory:
            with State(str(Path(directory)/'account'), 'participation-test') as state:
                foreign = dict(participation_expression='foreign')
                state.set('edge_identity', foreign)
                with configured('optional-crowding-half', venue=venue, features=None,
                                binding=dict(specification_sha256='b'*64)):
                    with self.assertRaisesRegex(Blocked, 'mismatched edge account'):
                        session._guard_state(state)
                self.assertEqual(state.get('edge_identity'), foreign)
                with self.assertRaisesRegex(Blocked, 'canonical rule'):
                    session._guard_state(state)
        self.assertIs(session._guard_state, original_guard)
        self.assertIs(edge.evaluate, original_evaluator)

    def test_adapter_rejected_before_clock_state_or_recovery(self):
        def no_clock():
            self.fail('adapter clock must not be read')
        with self.assertRaisesRegex(ValueError, 'before recovery'):
            with configured('optional-crowding-half', venue=SimpleNamespace(offline=False, clock=no_clock),
                            features=None, binding=dict(specification_sha256='b'*64)):
                self.fail('adapter context must not open')

    def test_half_preserves_account_ceiling_open_order_ownership_and_minimum(self):
        views = views_at()
        venue = SimpleNamespace(offline=True, now_ms=views[30].last+DAY+60000)
        snapshot = dict(usdt_free='1000', usdt_locked='0', btc='0', avg_price='100', open_orders=0)
        owned = {w:D(0) for w in views}
        with configured('optional-crowding-half', venue=venue, features=None,
                        binding=dict(specification_sha256='b'*64)) as selected:
            def decide(snap=snapshot, cap=D(300)):
                return selected.policy(views, owned, snap, positions={}, owners={},
                                       entries_enabled=True, capital_limit=cap)
            self.assertEqual(D(decide()['orders'][0]['quoteOrderQty']), D(150))
            self.assertEqual(decide(dict(snapshot, open_orders=1))['orders'], [])
            with self.assertRaisesRegex(Unknown, 'no recorded spotquant fill'):
                decide(dict(snapshot, btc='1'))
            tiny = decide(cap=D(8))
            self.assertEqual(tiny['orders'], [])
            self.assertEqual(tiny['action'], 'flat')
