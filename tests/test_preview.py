"""Current book sizes real funds, sells owned coins and never invents a fill."""
from decimal import Decimal as D
import unittest

from spotquant.model import DAY, ORIGIN, Model, SLEEVES
from spotquant.preview import decision_view, portfolio
from spotquant.session import _entries_blocked, _follow_after, _view


def model(prices=(98, 101, 102), window=30):
    view = Model(window)
    for index, price in enumerate([100] * 400 + list(prices)):
        view.advance_open(ORIGIN + index * DAY, price)
        view.update(ORIGIN + index * DAY, price, price, price)
    return view


def snapshot(usdt='1000', btc='0', price='102', orders=0):
    return dict(btc=D(btc), usdt_free=D(usdt), usdt_locked=D(0), open_orders=orders,
                avg_price=D(price), account_uid='10001', environment='live')


class PreviewTests(unittest.TestCase):
    def test_shadow_long_entry_waits_until_price_leaves_touch_and_keeps_actual_exit_lock(self):
        view = model((98, 101, 102, 103), 40)
        self.assertTrue(view.shadow_in)
        current = snapshot(price='103')
        current['last_price'] = view.sma * (1 + view.touch)
        result = portfolio({40: view}, {}, current, entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'flat')
        current['last_price'] += D('.01')
        result = portfolio({40: view}, {}, current, entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'enter')

        # A real touch sale still consumes this bar, even after price recovers.
        view.note_flat(rearm=True)
        exit_through = {'40': view.last}
        self.assertTrue(_entries_blocked({40: view}, exit_through))
        blocked = portfolio({40: view}, {}, current,
                            entries_enabled=not _entries_blocked({40: view}, exit_through), capital_limit=None)
        self.assertEqual(blocked['action'], 'flat')
        self.assertIsNone(_follow_after(result, {40: view}, {40: None}, {40: None}, exit_through)[40])
        next_bar = view.last + DAY
        view.advance_open(next_bar, D(104))
        view.update(next_bar, D(104), D(104), D(104))
        current['last_price'] = D(104)
        allowed = portfolio({40: view}, {}, current,
                            entries_enabled=not _entries_blocked({40: view}, exit_through), capital_limit=None)
        self.assertEqual(allowed['action'], 'enter')

    def test_touch_entry_guard_exempts_early_and_bullish_repair_entries(self):
        early = model((101,), 40)
        repair = model((40,) * 40 + (45, 40, D('42.4')), 40)
        self.assertTrue(repair.cap_enter)
        for view in (early, repair):
            self.assertFalse(view.shadow_in)
            self.assertTrue(view.bull)
            current = snapshot(price=str(view.close))
            current['last_price'] = view.sma * (1 + view.touch)
            result = portfolio({40: view}, {}, current, entries_enabled=True, capital_limit=None)
            self.assertEqual(result['action'], 'enter')
            self.assertEqual(result['sleeves']['40']['repair'], view is repair)
        next_bar = repair.last + DAY
        repair.advance_open(next_bar, repair.close)
        repair.update(next_bar, repair.close, repair.close, repair.close)
        self.assertTrue(repair.shadow_repair)
        self.assertFalse(repair.cap_enter)
        current['last_price'] = repair.sma * (1 + repair.touch)
        result = portfolio({40: repair}, {}, current, entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'enter')
        self.assertTrue(result['sleeves']['40']['repair'])

    def test_one_close_can_buy_before_the_shadow_book_joins(self):
        view = model((101,))
        self.assertFalse(view.enter)
        self.assertFalse(view.shadow_in)
        result = portfolio({40: view}, {40: D(0)}, snapshot('1000', price='101'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'enter')

    def test_session_price_near_the_average_sells_while_the_shadow_book_stays_long(self):
        view = model()
        close_next = view.last + DAY
        view.advance_open(close_next, view.close)
        view.update(close_next, view.close, view.close, view.close)
        self.assertTrue(view.shadow_in)
        view, _qty = _view(view, dict(entry_fill='102', peak='110', qty='1',
                                      repair=False, repair_peak=None, adverse=False))
        near = snapshot('0', '1', price='102')
        near['last_price'] = view.sma
        result = portfolio({40: view}, {40: D(1)}, near, entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'exit')
        self.assertTrue(result['sleeves']['40']['rearm'])

    def test_crash_entry_identity_survives_follow_and_hold_before_shadow_entry(self):
        view = model(('50', '46', '48.76'), 40)
        self.assertTrue(view.cap_enter)
        self.assertFalse(view.shadow_in)
        self.assertFalse(view.bull)
        result = portfolio({40: view}, {}, snapshot(price='48.76'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'enter')
        follows = _follow_after(result, {40: view}, {40: None}, {40: None}, {})
        self.assertTrue(follows[40]['repair'])
        position = dict(entry_fill='48.76', peak='48.76', qty='1',
                        repair=follows[40]['repair'], repair_peak='48.76', adverse=False)
        held, qty = _view(view, position)
        result = portfolio({40: held}, {40: qty}, snapshot('0', '1', price='48.76'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'hold')
        self.assertEqual(result['protections'][0]['stopPrice'], '35.10')
        crossed = snapshot('0', '1', price='48.76')
        crossed['last_price'] = D(35)
        result = portfolio({40: held}, {40: qty}, crossed,
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'exit')
        self.assertFalse(result['sleeves']['40']['rearm'])

        # An unfilled preview can outlive the repair's completed-bar handoff.
        view.advance_open(view.last + DAY, D(100))
        view.update(view.last + DAY, D(100), D(100), D(100))
        self.assertFalse(view.shadow_repair)
        result = portfolio({40: view}, {}, snapshot(price='100'),
                           entries_enabled=True, capital_limit=None)
        updated = _follow_after(result, {40: view}, {40: None}, follows, {})
        self.assertFalse(updated[40]['repair'])
        self.assertEqual(updated[40]['signal_ms'], follows[40]['signal_ms'])

    def test_native_stop_floor_belongs_to_the_actual_position(self):
        view = model(('98', '101', '102', '103'), 40)
        first_ms = view.last + DAY + 60_000
        position = dict(first_ms=first_ms)
        old = dict(sleeves=[40], signal_ms=view.last, native_status='CANCELED',
                   position_first_ms={'40': first_ms - DAY},
                   native_created_ms=first_ms - 1000,
                   order=dict(type='STOP_LOSS', stopPrice='144'))
        current = dict(old, native_status='NEW', position_first_ms={'40': first_ms})
        self.assertEqual(decision_view(view, position, {'old': old})._stop_floor, D(0))
        self.assertEqual(decision_view(view, position, {'old': old, 'new': current})._stop_floor, D(144))
        legacy = dict(current)
        legacy.pop('position_first_ms')
        legacy['native_created_ms'] = first_ms
        self.assertEqual(decision_view(view, position, {'new': legacy})._stop_floor, D(144))
        legacy['native_created_ms'] = first_ms - 1000
        from spotquant.types import Unknown
        with self.assertRaisesRegex(Unknown, 'no proven position identity'):
            decision_view(view, position, {'unknown': legacy})

    def test_shadow_repair_does_not_exempt_an_ordinary_position(self):
        view = model(('50', '46', '48.76', '48.76'), 40)
        self.assertTrue(view.shadow_repair)
        held, qty = _view(view, dict(entry_fill='48.76', peak='48.76', qty='1',
                                     repair=False, repair_peak=None, adverse=False))
        result = portfolio({40: held}, {40: qty}, snapshot('0', '1', price='48.76'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'exit')
        self.assertFalse(result['sleeves']['40']['rearm'])

    def test_new_fill_stop_is_compared_with_current_price(self):
        view = model(('98', '101', '102', '103'), 40)
        held, qty = _view(view, dict(entry_fill='200', peak='200', qty='1',
                                     repair=True, repair_peak='200', adverse=False))
        current = snapshot('0', '1', price='200')
        current['last_price'] = D(200)
        result = portfolio({40: held}, {40: qty}, current,
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['action'], 'hold')
        self.assertEqual(result['protections'][0]['stopPrice'], '144.00')

    def test_non_touch_exit_blocks_old_shadow_signal_but_touch_can_rejoin(self):
        view = model(('98', '101', '102', '103'), 40)
        self.assertTrue(view.shadow_in)
        held, qty = _view(view, dict(entry_fill='110', peak='110', qty='1',
                                     repair=False, repair_peak=None, adverse=True))
        near = snapshot('0', '1', price='103')
        near['last_price'] = view.sma
        result = portfolio({40: held}, {40: qty}, near,
                           entries_enabled=True, capital_limit=None)
        self.assertFalse(result['sleeves']['40']['rearm'])
        view.note_flat(rearm=False)
        view.advance_open(view.last + DAY, D(104))
        view.update(view.last + DAY, D(104), D(104), D(104))
        blocked = portfolio({40: view}, {}, snapshot(price='104'),
                            entries_enabled=True, capital_limit=None)
        self.assertEqual(blocked['action'], 'flat')
        view.note_flat(rearm=True)
        allowed = portfolio({40: view}, {}, snapshot(price='104'),
                            entries_enabled=True, capital_limit=None)
        self.assertEqual(allowed['action'], 'enter')
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
