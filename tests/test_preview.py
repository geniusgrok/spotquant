"""Current book sizes real funds, sells owned coins and never invents a fill."""
from decimal import Decimal as D
import unittest

from spotquant.model import DAY, ORIGIN, Model, SLEEVES
from spotquant.preview import portfolio, preview
from spotquant.types import Unknown


def model(prices=(98, 101, 102), window=30):
    view = Model(window)
    for index, price in enumerate([100] * 400 + list(prices)):
        view.update(ORIGIN + index * DAY, price, price, price)
    return view


def snapshot(usdt='1000', btc='0', price='102', orders=0):
    return dict(btc=D(btc), usdt_free=D(usdt), usdt_locked=D(0), open_orders=orders,
                avg_price=D(price), account_uid='10001', environment='live')


class PreviewTests(unittest.TestCase):
    def test_cold_start_does_not_buy_and_new_entry_is_bounded_and_rounded(self):
        view = model()
        self.assertTrue(view.enter)
        self.assertEqual(preview(view, snapshot(), entries_enabled=False, capital_limit=None)['action'], 'flat')
        result = preview(view, snapshot('1000.019'), entries_enabled=True, capital_limit=D(100))
        self.assertEqual(result['order']['quoteOrderQty'], '100.00')
        self.assertEqual(result['protection']['stopPrice'], '73.44')
        self.assertNotIn('quantity', result['protection'])
        self.assertNotIn('trailingDelta', result['protection'])

    def test_stop_peak_starts_with_fill_and_not_prefill_wick(self):
        view = model((98, 101, 102))
        view.peak = D(150)
        held = preview(view, snapshot(btc='1'), entries_enabled=True, capital_limit=None, owned_btc=D(1))
        self.assertEqual(held['action'], 'hold')
        self.assertEqual(held['protection']['stopPrice'], '73.44')
        view.note_entry(102, 110)
        filled = preview(view, snapshot(btc='1'), entries_enabled=True, capital_limit=None, owned_btc=D(1))
        self.assertEqual(filled['protection']['stopPrice'], '79.20')

    def test_exit_reasons_sell_actual_coins_and_never_claim_capped_loss(self):
        cases = [(model((500,)), 'extended'), (model((98,)), 'below'), (model((97, 96)), '4%')]
        cases[-1][0].note_entry(100)
        # Set the fill before another completed loss close.
        cases[-1][0].update(cases[-1][0].last + DAY, 96, 96, 96)
        for view, reason in cases:
            with self.subTest(reason=reason):
                result = preview(view, snapshot(btc='1'), entries_enabled=True,
                                 capital_limit=None, owned_btc=D(1))
                self.assertEqual(result['action'], 'exit')
                self.assertEqual(result['order']['quantity'], '1.00000')
                self.assertFalse(result['loss_capped'])

    def test_external_btc_and_unowned_pool_are_unknown(self):
        view = model()
        with self.assertRaises(Unknown):
            preview(view, snapshot(btc='1'), entries_enabled=True, capital_limit=None)
        with self.assertRaises(Unknown):
            portfolio({30: view}, {30: D(0)}, snapshot(btc='1'), entries_enabled=True, capital_limit=None)

    def test_consensus_allocation_and_capital_limit_use_current_shared_pool(self):
        views = {30: model(), 40: model(window=40), 50: model((98,), 50)}
        views[40].note_entry(102, 102)
        result = portfolio(views, {30: D(0), 40: D(1), 50: D(0)}, snapshot('100', '1'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['orders'][0]['quoteOrderQty'], '90.00')
        self.assertEqual(result['orders'][0]['sleeves'], [30])
        capped = portfolio(views, {30: D(0), 40: D(1), 50: D(0)}, snapshot('100', '1'),
                           entries_enabled=True, capital_limit=D(140))
        self.assertEqual(D(capped['orders'][0]['quoteOrderQty']), D(38))
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
        dust = D('.00006')
        result = portfolio(views, {w: dust for w in SLEEVES}, snapshot('1000', '.00018', '70000'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['orders'][0]['side'], 'SELL')
        self.assertEqual(result['orders'][0]['quantity'], '0.00018')
        self.assertFalse(any(o['side'] == 'BUY' for o in result['orders']))
