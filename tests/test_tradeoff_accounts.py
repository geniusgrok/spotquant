import unittest
from decimal import Decimal as D
from types import SimpleNamespace
from research.tradeoff_accounts import policy_runtime, signal_book


class TradeoffAccounts(unittest.TestCase):
    def test_control_owns_real_components_and_causal_clock(self):
        packet = {'bars':{str(i*86400000):dict(open='100', high='101', low='99', close=str(100+i))
                          for i in range(120)}}
        for mode in ('tactical', 'constant25'):
            book, _ = signal_book(packet, mode, 'a'*64)
            self.assertEqual(book.mode, mode)
            self.assertTrue(all(r['fraction'] == sum(r['components'].values(), D(0)) for r in book.rows))
            self.assertTrue(all(r['components'][30 if mode == 'tactical' else 40] == 0 for r in book.rows))
            self.assertIsNone(book.at(86459999))

    def test_offline_guard_restores_scoped_rule(self):
        from spotquant import session
        before = session.RULE
        with self.assertRaisesRegex(ValueError, 'offline'):
            with policy_runtime('baseline', None) as runtime:
                runtime.run(None, SimpleNamespace(offline=False), execute=True)
        self.assertEqual(session.RULE, before)
