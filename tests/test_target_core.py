from decimal import Decimal as D
import tempfile
import unittest

from spotquant import core, session
from spotquant.model import Model, SLEEVES, DAY, ORIGIN
from spotquant.target import exposure, forecast
from spotquant.types import Unknown, Blocked
from spotquant.state import State


class CoreTests(unittest.TestCase):
    def book(self):
        views={w:Model(w) for w in SLEEVES}
        for i in range(100):
            p=D(100)*D('1.005')**i
            for m in views.values():m.update(ORIGIN+i*DAY,p,p,p)
        return views

    def test_target_is_causal_bounded_and_has_no_short_spot(self):
        values=[D(100)*D('1.01')**i for i in range(100)]
        self.assertGreater(exposure(values,spot=True),0)
        self.assertLessEqual(exposure(values,spot=True),D('.95'))
        self.assertEqual(exposure(list(reversed(values)),spot=True),0)
        self.assertEqual(forecast(values[:60]),(D(0),D(0)))
        with self.assertRaises(ValueError):forecast([D('NaN')])

    def test_owned_target_can_add_and_trim_without_old_entry_event(self):
        views=self.book();price=views[30].close
        snapshot=dict(avg_price=price,btc=D(0),usdt_free=D(1000),usdt_locked=D(0),open_orders=0)
        p=core.decision(views,{},snapshot,positions={},owners={},entries_enabled=True,capital_limit=None)
        self.assertEqual(p['orders'][0]['sleeves'],[40]);self.assertEqual(p['orders'][0]['side'],'BUY')
        snapshot.update(btc=D(8),usdt_free=D(0))
        p=core.decision(views,{40:D(8)},snapshot,positions={},owners={},entries_enabled=True,capital_limit=D(100))
        self.assertEqual(p['orders'][0]['side'],'SELL')
        self.assertLess(D(p['orders'][0]['quantity']),D(8))

    def test_external_coins_block_and_open_orders_block_add_only(self):
        views=self.book();snap=dict(avg_price=views[30].close,btc=D(1),usdt_free=D(1000),open_orders=0)
        with self.assertRaises(Unknown):core.decision(views,{},snap,positions={},owners={},entries_enabled=True,capital_limit=None)
        snap.update(btc=D(0),open_orders=1)
        self.assertEqual(core.decision(views,{},snap,positions={},owners={},entries_enabled=True,capital_limit=None)['orders'],[])

    def test_old_state_refuses_before_venue_reads(self):
        with tempfile.TemporaryDirectory() as folder,State(folder,'binance:BTCUSDT:spot:demo:123') as state:
            state.set('rule','2026-10-03-atr-stop-crowding-interaction-v1')
            with self.assertRaises(Blocked):session.cycle(object(),state,object(),execute=True)
