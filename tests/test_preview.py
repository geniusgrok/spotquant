"""Current book sizes real funds, sells owned coins and never invents a fill."""
from decimal import Decimal as D
import unittest

from spotquant.model import DAY, ORIGIN, Model, SLEEVES
from spotquant.preview import portfolio
from spotquant.session import _view


def model(prices=(98, 101, 102), window=30):
    view = Model(window)
    for index, price in enumerate([100] * 400 + list(prices)):
        view.update(ORIGIN + index * DAY, price, price, price)
    return view


def snapshot(usdt='1000', btc='0', price='102', orders=0):
    return dict(btc=D(btc), usdt_free=D(usdt), usdt_locked=D(0), open_orders=orders,
                avg_price=D(price), account_uid='10001', environment='live')


class PreviewTests(unittest.TestCase):
    def test_consensus_allocation_and_capital_limit_use_current_shared_pool(self):
        views = {30: model(), 40: model(window=40), 50: model((98,), 50)}
        views[40], _ = _view(views[40], dict(entry_fill='102', peak='102', qty='1',
                                          repair=False, repair_peak=None, adverse=False))
        result = portfolio(views, {30: D(0), 40: D(1), 50: D(0)}, snapshot('100', '1'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['orders'][0]['quoteOrderQty'], '90.00')
        self.assertEqual(result['orders'][0]['sleeves'], [30])
        capped = portfolio(views, {30: D(0), 40: D(1), 50: D(0)}, snapshot('100', '1'),
                           entries_enabled=True, capital_limit=D(140))
        self.assertEqual(D(capped['orders'][0]['quoteOrderQty']), D(38))
        rising = snapshot('100', '1')
        rising['last_price'] = D(130)
        marked = portfolio(views, {30: D(0), 40: D(1), 50: D(0)}, rising,
                           entries_enabled=True, capital_limit=D(140))
        self.assertEqual(D(marked['orders'][0]['quoteOrderQty']), D(10))
        all_enter = {w: model(window=w) for w in SLEEVES}
        pooled = portfolio(all_enter, {}, snapshot('100.01'), entries_enabled=True, capital_limit=D(50))
        self.assertEqual(D(pooled['orders'][0]['quoteOrderQty']), D(50))
        self.assertEqual(sum((D(pooled['sleeves'][str(w)]['order']['quoteOrderQty']) for w in SLEEVES), D(0)), D(50))

    def test_single_bullish_sleeve_does_not_invent_consensus(self):
        views = {30: model(), 40: model((98,), 40), 50: model((98,), 50)}
        result = portfolio(views, {}, snapshot('100'), entries_enabled=True, capital_limit=None)
        self.assertEqual(result['orders'][0]['quoteOrderQty'], '33.33')

    def test_pool_sells_its_rounded_dust_together(self):
        views = {w: model((70000,), w) for w in SLEEVES}
        for view in views.values():
            view.bull = False
        dust = D('0.00008')
        result = portfolio(views, {w: dust for w in SLEEVES}, snapshot('1000', '0.00008', '70000'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['orders'][0]['side'], 'SELL')
        self.assertEqual(result['orders'][0]['quantity'], '0.00008')
        self.assertFalse(any(o['side'] == 'BUY' for o in result['orders']))
