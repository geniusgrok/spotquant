from decimal import Decimal as D
import tempfile
import unittest

from spotquant.model import DAY,ORIGIN,Model,SLEEVES
from spotquant.state import State
from spotquant.types import Blocked,Unknown
from research.history_core import Signals,decision,runtime
from research.history_screen import run
from research.core_screen import Wallet


class HistoryCoreTests(unittest.TestCase):
    def book(self):
        bars = []
        views = {w:Model(w) for w in SLEEVES}
        for i in range(110):
            p = D(100)*D('1.005')**i
            t = ORIGIN+i*DAY
            bars.append((t,p,p,p,p))
            for view in views.values():view.update(t,p,p,p)
        return views,Signals(bars,'base','a'*64),bars

    def test_two_components_spend_confirmed_cash_within_whole_cap(self):
        views,signals,bars = self.book()
        snapshot=dict(avg_price=bars[-1][4],btc=D(0),usdt_free=D(1000),open_orders=0)
        kwargs=dict(positions={},owners={},entries_enabled=True,capital_limit=None,signals=signals,
                    decision_ms=bars[-1][0]+DAY+60000)
        result=decision(views,{},snapshot,**kwargs)
        self.assertEqual([o['sleeves'] for o in result['orders']],[[30],[40]])
        self.assertLessEqual(sum(D(o['quoteOrderQty']) for o in result['orders']),D(900))
        blocked=decision(views,{},snapshot,blocked_sleeves={'30':views[30].last},**kwargs)
        self.assertEqual([o['sleeves'] for o in blocked['orders']],[[40]])
        snapshot.update(btc=D(8),usdt_free=D(0))
        trim=decision(views,{40:D(8)},snapshot,**dict(kwargs,capital_limit=D(100)))
        self.assertTrue(all(o['side']=='SELL' and D(o['quantity'])<=8 for o in trim['orders']))
        with self.assertRaises(Unknown):decision(views,{},snapshot,**kwargs)

    def test_foreign_rule_blocks_before_recovery_and_scope_restores(self):
        views,signals,bars=self.book()
        from spotquant import model,session
        old_rule,old_origin=session.RULE,model.ORIGIN
        with tempfile.TemporaryDirectory() as folder,State(folder,'binance:BTCUSDT:spot:demo:123') as state:
            state.set('rule',old_rule)
            with runtime(signals):
                with self.assertRaises(Blocked):session.cycle(object(),state,object(),execute=True)
        self.assertEqual((session.RULE,model.ORIGIN),(old_rule,old_origin))

    def test_future_packet_quote_cannot_change_terminal_wallet(self):
        bars=[(ORIGIN+i*DAY,D(100),D(101),D(99),D(100)) for i in range(2)]
        args=('spot','constant90',bars,None,[],ORIGIN,ORIGIN+2*DAY,Wallet)
        first=run(*args)
        appended=bars+[(ORIGIN+2*DAY,D(100000),D(100000),D(100000),D(100000))]
        second=run('spot','constant90',appended,None,[],ORIGIN,ORIGIN+2*DAY,Wallet)
        self.assertEqual(first['final_wealth'],second['final_wealth'])
        self.assertEqual(second['terminal_price'],D(100))
