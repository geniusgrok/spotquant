"""Current book sizes real funds, sells owned coins and never invents a fill."""
from decimal import Decimal as D
import unittest

from spotquant.model import DAY, ORIGIN, Model, SLEEVES, TRAIL
from spotquant.preview import (
    STOP_BAND_BUFFER, _annotate_venue, _protection, clamp_stop, decision, decision_view,
    portfolio, stop_band_violation,
)
from spotquant.session import _entries_blocked, _follow_after, _view


def model(prices=(98, 101, 102), window=40):
    view = Model(window)
    for index, price in enumerate([100] * 400 + list(prices)):
        view.advance_open(ORIGIN + index * DAY, price)
        view.update(ORIGIN + index * DAY, price, price, price)
    return view


def snapshot(usdt='1000', btc='0', price='102', orders=0):
    return dict(btc=D(btc), usdt_free=D(usdt), usdt_locked=D(0), open_orders=orders,
                avg_price=D(price), account_uid='10001', environment='live')


class PreviewTests(unittest.TestCase):
    def test_owned_close_dust_can_reenter_but_a_partial_position_cannot_top_up(self):
        from venue_fixture import KnownFeatures
        for closed in (True, False):
            with self.subTest(closed=closed):
                view = model((101,), 40)
                position = dict(qty='.000001', dust=closed, entry_fill='101', peak='101',
                                first_ms=view.last + DAY, repair=False, repair_peak=None, adverse=False)
                held, quantity = _view(view, position)
                result = decision({40: held}, {40: quantity}, snapshot(btc='.000001'),
                                  positions={40: position}, owners={}, entries_enabled=True,
                                  capital_limit=None, crowding_source=KnownFeatures(),
                                  decision_ms=view.last + DAY + 120_000)
                if closed:
                    self.assertEqual(result['action'], 'enter')
                    self.assertEqual(result['order']['side'], 'BUY')
                else:
                    self.assertEqual(result['action'], 'flat')
                    self.assertEqual(result['orders'], [])
                    self.assertIn('held_sleeve_no_topup', result['reason'])

    def test_final_entry_filters_update_summary_and_sleeve_reason(self):
        view = model((101,), 40)
        cases = (
            (snapshot(), None, 'missing_causal_crowding_or_momentum'),
            (snapshot(btc='.04', price='100'), D(8), 'capital ceiling'),
            (snapshot(orders=1), None, 'open order blocks a new buy'),
        )
        for current, limit, reason in cases:
            with self.subTest(reason=reason):
                result = decision({40: view}, {40: D(0)}, current, positions={}, owners={},
                                  entries_enabled=True, capital_limit=limit,
                                  decision_ms=view.last + DAY + 120_000)
                self.assertEqual(result['action'], 'flat')
                self.assertEqual(result['orders'], [])
                self.assertEqual(result['protections'], [])
                self.assertIsNone(result['order'])
                self.assertEqual(result['sleeves']['40']['action'], 'flat')
                self.assertIn(reason, result['reason'])
                self.assertIn(reason, result['sleeves']['40']['reason'])

    def test_crossed_confirmed_stop_updates_final_summary(self):
        view = model((98, 101, 102, 103), 40)
        position = {'first_ms': view.last + DAY}
        owner = dict(sleeves=[40], native_status='NEW', position_first_ms={'40': position['first_ms']},
                     order=dict(type='STOP_LOSS', stopPrice='144'))
        current = dict(snapshot(btc='1', price='103'), last_price=D(120))
        result = decision({40: view}, {40: D(1)}, current, positions={40: position}, owners={'stop': owner},
                          entries_enabled=True, capital_limit=None, decision_ms=view.last + DAY + 120_000)
        self.assertEqual(result['action'], 'exit')
        self.assertEqual(result['orders'][0]['side'], 'SELL')
        self.assertEqual(result['sleeves']['40']['order']['side'], 'SELL')
        self.assertFalse(result['untradeable'])
        self.assertFalse(result['loss_capped'])
        self.assertIn('protection price is already crossed', result['reason'])
        self.assertIn('protection price is already crossed', result['sleeves']['40']['reason'])

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

    def test_shadow_exit_sells_and_an_unjoined_early_entry_still_holds(self):
        view = Model(40)
        for index in range(400):
            view.advance_open(ORIGIN + index * DAY, 100)
            view.update(ORIGIN + index * DAY, 100, 100, 100)
        view.advance_open(view.last + DAY, 101)
        view.update(view.last + DAY, 101, 101, 101)
        view.advance_open(view.last + DAY, 102)
        view.update(view.last + DAY, 102, 102, 102)
        signal = view.last
        view.advance_open(signal + DAY, 150)
        view.update(signal + DAY, 155, 139, 140)
        view.advance_open(view.last + DAY, 137)
        self.assertTrue(view.need_reset)
        self.assertFalse(view.shadow_in)
        position = dict(entry_fill='102', peak='140', qty='1', first_ms=view.last,
                        repair=False, repair_peak=None, adverse=False)
        held, qty = _view(view, position)
        result = decision({40: held}, {40: qty}, dict(snapshot('0', '1', price='137'), last_price=D(137)),
                          positions={40: position}, owners={}, entries_enabled=True, capital_limit=None,
                          decision_ms=view.last + DAY)
        self.assertEqual(result['action'], 'exit')
        self.assertFalse(result['sleeves']['40']['rearm'])
        early = model((101,), 40)
        early_position = dict(entry_fill='101', peak='101', qty='1', first_ms=early.last,
                              repair=False, repair_peak=None, adverse=False)
        early_held, early_qty = _view(early, early_position)
        held_early = decision({40: early_held}, {40: early_qty},
                              dict(snapshot('0', '1', price='101'), last_price=D(101)),
                              positions={40: early_position}, owners={}, entries_enabled=True,
                              capital_limit=None, decision_ms=early.last + DAY)
        self.assertEqual(held_early['action'], 'hold')

    def test_single_position_buy_uses_free_cash_and_capital_ceiling(self):
        views = {40: model()}
        pooled = portfolio(views, {}, snapshot('100.01'), entries_enabled=True, capital_limit=D(50))
        self.assertEqual(D(pooled['orders'][0]['quoteOrderQty']), D(50))
        self.assertEqual(D(pooled['sleeves']['40']['order']['quoteOrderQty']), D(50))
        held, _ = _view(views[40], dict(entry_fill='102', peak='130', qty='1', repair=False,
                                      repair_peak=None, adverse=False))
        result = portfolio({40: held}, {40: D(1)}, dict(snapshot('100', '1'), last_price=D(130)),
                           entries_enabled=True, capital_limit=D(50))
        self.assertEqual(result['action'], 'hold')
        self.assertEqual(result['orders'], [])

    def test_single_position_sells_its_rounded_dust(self):
        views = {w: model((70000,), w) for w in SLEEVES}
        for view in views.values():
            view.bull = False
        dust = D('0.00008')
        result = portfolio(views, {w: dust for w in SLEEVES}, snapshot('1000', '0.00008', '70000'),
                           entries_enabled=True, capital_limit=None)
        self.assertEqual(result['orders'][0]['side'], 'SELL')
        self.assertEqual(result['orders'][0]['quantity'], '0.00008')
        self.assertFalse(any(o['side'] == 'BUY' for o in result['orders']))

    def test_exit_reason_uses_current_notional_price_and_venue_minimum(self):
        view = model()
        view.bull = False
        quantity = D('.049')
        for price, minimum, tradable in (('110', '5', True), ('110', '6', False), ('90', '5', False)):
            with self.subTest(price=price, minimum=minimum):
                current = dict(snapshot('0', str(quantity), price=price), min_notional=D(minimum))
                result = portfolio({40: view}, {40: quantity}, current,
                                   entries_enabled=True, capital_limit=None)
                self.assertEqual(bool(result['orders']), tradable)
                self.assertEqual(result['untradeable'], not tradable)
                self.assertEqual('below the minimum notional' in result['reason'], not tradable)


class PercentBandTests(unittest.TestCase):
    def test_a_28_percent_stop_is_outside_the_sell_band(self):
        snapshot = {
            'avg_price': D('100'), 'last_price': D('100'), 'min_notional': D('5'),
            'percent_price_by_side': {
                'filter': 'PERCENT_PRICE_BY_SIDE',
                'ask_multiplier_down': D('0.8'),
                'ask_multiplier_up': D('5'),
                'avg_price_mins': 5,
            },
        }
        self.assertIn('PERCENT_PRICE_BY_SIDE', stop_band_violation(snapshot, '72'))
        self.assertIn('unprotected', stop_band_violation(snapshot, '72'))
        self.assertIsNone(stop_band_violation(snapshot, '80'))
        self.assertIsNone(stop_band_violation(snapshot, '80.08'))
        order = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                 'quantity': '0.1', 'stopPrice': '72.00'}
        _annotate_venue(order, Model(40), snapshot)
        self.assertTrue(order['placeable'])
        confirmed = dict(snapshot, stop_price_percent_band=True)
        refused = dict(order)
        _annotate_venue(refused, Model(40), confirmed)
        self.assertFalse(refused['placeable'])
        self.assertIn('unprotected', refused['unplaceable_reason'])

    def _band(self, avg='100', last='100', down='0.8', up='5', tick=None):
        snap = {
            'avg_price': D(avg), 'last_price': D(last), 'min_notional': D('5'),
            'percent_price_by_side': {
                'filter': 'PERCENT_PRICE_BY_SIDE',
                'ask_multiplier_down': D(down),
                'ask_multiplier_up': D(up),
                'avg_price_mins': 5,
            },
        }
        if tick is not None:
            snap['tick_size'] = D(tick)
        return snap

    def test_clamp_lifts_the_28_percent_target_to_the_buffered_tick(self):
        self.assertEqual(TRAIL, D('0.28'))
        self.assertEqual(STOP_BAND_BUFFER, D('0.001'))
        plan = clamp_stop(D('100') * (D(1) - TRAIL), self._band(), existing=D(0))
        self.assertEqual(plan['target'], '72.00')
        self.assertEqual(plan['band_floor'], '80.08')
        self.assertEqual(plan['placed'], '80.08')
        self.assertTrue(plan['clamped'])
        self.assertIsNone(plan['unplaceable_reason'])
        self.assertIsNone(stop_band_violation(self._band(), plan['placed']))

    def test_band_floor_ceil_and_target_floor_respect_the_tick(self):
        # 100.01 * 0.8 * 1.001 = 80.088008, which must round up to 80.09.
        plan = clamp_stop('72.009', self._band(avg='100.01'), existing=D(0))
        self.assertEqual(plan['target'], '72.00')
        self.assertEqual(plan['band_floor'], '80.09')
        self.assertEqual(plan['placed'], '80.09')
        odd = clamp_stop('72.019', self._band(tick='0.05'))
        self.assertEqual(odd['target'], '72.00')
        self.assertEqual(odd['band_floor'], '80.10')
        self.assertEqual(odd['placed'], '80.10')

    def test_ratchet_never_lowers_and_uses_the_28_percent_target_when_it_is_higher(self):
        band = self._band()
        held = clamp_stop('72', band, existing=D('85'))
        self.assertEqual(held['placed'], '85.00')
        self.assertEqual(held['band_floor'], '80.08')
        self.assertFalse(held['clamped'])
        self.assertGreaterEqual(D(held['placed']), D('85'))
        raised = clamp_stop('72', band, existing=D('80.08'))
        self.assertEqual(raised['placed'], '80.08')
        self.assertTrue(raised['clamped'])
        # After a drawdown the average falls and the 28% target is inside the band.
        placeable = clamp_stop('72', self._band(avg='80'), existing=D(0))
        self.assertEqual(placeable['target'], '72.00')
        self.assertEqual(placeable['band_floor'], '64.07')
        self.assertEqual(placeable['placed'], '72.00')
        self.assertFalse(placeable['clamped'])
        # An existing clamped stop still does not move down.
        kept = clamp_stop('72', self._band(avg='80'), existing=D('80.08'))
        self.assertEqual(kept['placed'], '80.08')
        self.assertFalse(kept['clamped'])
        # A higher peak makes the 28% target the price that is placed.
        above = clamp_stop(D('150') * (D(1) - TRAIL), band, existing=D('80.08'))
        self.assertEqual(above['target'], '108.00')
        self.assertEqual(above['placed'], '108.00')
        self.assertFalse(above['clamped'])

    def test_protection_order_reports_the_clamp_and_keeps_the_native_floor(self):
        view = Model(40)
        view.close = D('100')
        view.position_peak = D('100')
        plain = _protection(view, D('0.1'), self._band())
        self.assertEqual(plain['stop_target'], '72.00')
        self.assertEqual(plain['band_floor'], '80.08')
        self.assertEqual(plain['stopPrice'], '72.00')
        self.assertFalse(plain['clamped'])
        self.assertFalse(plain['rule_enabled'])
        self.assertTrue(plain['placeable'])
        view._stop_floor = D('90')
        held = dict(self._band(), stop_price_percent_band=True)
        order = _protection(view, D('0.1'), held)
        self.assertEqual(order['stop_target'], '72.00')
        self.assertEqual(order['band_floor'], '80.08')
        self.assertEqual(order['stopPrice'], '90.00')
        self.assertFalse(order['clamped'])
        self.assertTrue(order['rule_enabled'])
        view._stop_floor = D(0)
        order = _protection(view, D('0.1'), held)
        self.assertEqual(order['stopPrice'], '80.08')
        self.assertTrue(order['clamped'])
        self.assertTrue(order['placeable'])
        tight = dict(self._band(up='0.5'), stop_price_percent_band=True)
        blocked = _protection(view, D('0.1'), tight)
        self.assertFalse(blocked['placeable'])
        self.assertIn('PERCENT_PRICE', blocked['unplaceable_reason'])
        self.assertIn('unprotected', blocked['unplaceable_reason'])
