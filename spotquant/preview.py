"""Read-only spot decision. No order is built into a network request here."""
from __future__ import annotations

import copy
from decimal import Decimal as D

from .model import Model, percent
from .types import Blocked, Unknown, ceil_step, floor_step, number, serial

MIN_NOTIONAL = D('5')
QUOTE_STEP = D('0.01')
BASE_STEP = D('0.00001')
PRICE_STEP = D('0.01')
# Keeps a stop above askMultiplierDown if the average moves slightly before POST.
STOP_BAND_BUFFER = D('0.001')


def _decide(model: Model, snapshot: dict, *, entries_enabled: bool, owned_btc: D, budget: D) -> dict:
    """One sleeve. ``budget`` is the USDT this sleeve may spend on an entry."""
    owned = floor_step(owned_btc, BASE_STEP)
    if owned > 0:
        return _position_decision(model, snapshot, owned)
    blocked = model.shadow_blocked
    if blocked is not None and model.last <= blocked:
        return _flat('the completed bar already consumed its entry signal')
    early = model.streak >= 1 and model.crash_ok and not model.need_reset and not model.shadow_in
    armed = (model.shadow_in and blocked is None) or model.enter or model.cap_enter or early
    if not armed:
        if model.sma is None:
            return _flat('SMA warmup is incomplete')
        if not model.bull:
            return _flat('completed daily close is not above its SMA')
        return _flat('entry is not armed: confirmation, fresh cross, crash filter, or crash reversal')
    if not entries_enabled:
        return _flat('cold start: no completed daily close after the first checkpoint')
    spend = floor_step(max(budget, D(0)), QUOTE_STEP)
    if spend < D(snapshot.get('min_notional') or MIN_NOTIONAL):
        return _flat('available cash or capital ceiling is below the current venue minimum notional')
    repair = bool(model.cap_enter if not model.shadow_in or blocked is not None else model.shadow_repair)
    if not repair and model.extended:
        return _flat('completed daily close is already in the overextended exit region')
    last = snapshot.get('last_price')
    if (not repair and model.shadow_in and model.bull and not model.extended
            and last is not None and model.sma is not None
            and D(last) <= model.sma * (D(1) + model.touch)):
        return _flat('session price is already in the SMA touch exit region')
    return {
        'action': 'enter',
        'repair': repair,
        'reason': (
            (
                f'crash reversal: {percent(model.cap_drop)} down, then {percent(model.cap_bounce)} up, '
                f'still at least {percent(model.cap_depth)} under the {model.cap_window}-day high'
            )
            if repair
            else (
                'one completed close is back above its SMA and passed the crash filter'
                if not model.shadow_in
                else 'the daily book is long and this session is flat'
            )
        ),
        'order': {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'type': 'MARKET',
            'quoteOrderQty': _step(spend, QUOTE_STEP),
        },
        'protection': _protection(model, None, snapshot),
    }


def _position_decision(model: Model, snapshot: dict, owned: D) -> dict:
    """A sleeve with any coins is a position, even below the minimum notional.

    Dust is not a flat sleeve and is not a place to add risk. An exit remains
    visible even when its quantity cannot meet the current venue minimum.
    """
    breached = getattr(model, 'protection', 'resting') in ('breached', 'through_close')
    last = snapshot.get('last_price')
    crossed = (model.position_peak is not None
               and model.stop_price(model.position_peak) >= D(last if last is not None else model.close))
    if breached or crossed:
        return _exit(
            snapshot, owned,
            'the protection price is already crossed; the session sells',
        )
    if model.repair:
        return {
            'action': 'hold',
            'reason': (
                f'crash-reversal hold; SMA, blow-off, and the {percent(model.adverse_stop)} close '
                'stay off until the handoff'
            ),
            'order': None,
            'protection': _protection(model, owned, snapshot),
        }
    if model.adverse:
        return _exit(
            snapshot, owned,
            (
                f'completed daily close is at least {percent(model.adverse_stop)} under the entry fill; '
                f'the session sells, so the loss is not capped at {percent(model.adverse_stop)}'
            ),
        )
    if not model.shadow_in:
        if model.bull and not model.extended and not model.need_reset:
            return {
                'action': 'hold',
                'reason': 'early entry is waiting for the daily book to join or for the close to lose the SMA',
                'order': None,
                'protection': _protection(model, owned, snapshot),
            }
        return _exit(snapshot, owned, 'the daily book is flat; the session sells')
    if model.extended:
        return _exit(
            snapshot, owned,
            f'completed daily close is extended at least {percent(model.extend)} above its SMA',
        )
    if not model.bull:
        return _exit(snapshot, owned, 'completed daily close is not above its SMA')
    if last is not None and model.sma is not None and D(last) <= model.sma * (D(1) + model.touch):
        return _exit(
            snapshot, owned,
            f'session price is within {percent(model.touch)} of the SMA while the daily book stays long',
            rearm=True,
        )
    if model.position_peak is not None:
        reason = (
            f'still above the SMA; protection target is a stop {percent(model.trail)} '
            'under the high since the fill; the exchange stopPrice can be higher when the percent band applies'
        )
    else:
        reason = (
            f'still above the SMA; no fill is recorded, so the target stays {percent(model.trail)} '
            'under the completed close'
        )
    return {
        'action': 'hold',
        'reason': reason,
        'order': None,
        'protection': _protection(model, owned, snapshot),
    }


def _exit(snapshot: dict, owned: D, reason: str, *, rearm: bool = False) -> dict:
    tradable = owned * D(snapshot['avg_price']) >= D(snapshot.get('min_notional') or MIN_NOTIONAL)
    order = None
    if tradable:
        order = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': _step(owned, BASE_STEP)}
    return {
        'action': 'exit',
        'reason': reason if tradable else reason + '; this position is below the minimum notional',
        'loss_capped': False,
        'untradeable': not tradable,
        'order': order,
        'protection': None,
        'rearm': rearm,
    }


def _protection(model: Model, quantity: D | None, snapshot: dict) -> dict:
    # No recorded fill yet. The bullish-streak high can start before the buy,
    # so it is not the stop. A crash reversal is still a repair hold on the
    # book when the ordinary entry is also armed.
    trail = percent(model.trail)
    if quantity is None:
        peak = model.close
        if model.cap_enter:
            note = (
                f'quantity would be the filled base amount; crash-reversal target starts {trail} under '
                'the completed close and is amended to the fill'
            )
        else:
            note = (
                f'quantity would be the filled base amount; target starts {trail} under the completed '
                'close and is amended to the fill'
            )
    elif model.repair and model.repair_peak is not None:
        peak = model.repair_peak
        note = f'amended STOP_LOSS; target is {trail} under the high since the repair fill'
    elif model.position_peak is not None:
        peak = model.position_peak
        note = f'amended STOP_LOSS; target is {trail} under the high since the fill'
    else:
        peak = model.close
        note = f'amended STOP_LOSS; target is {trail} under the completed close until a fill is recorded'
    apply = snapshot.get('stop_price_percent_band') is True
    plan = clamp_stop(peak * (D(1) - model.trail), snapshot,
                      existing=getattr(model, '_stop_floor', D(0)), apply=apply)
    if plan['clamped']:
        note += '; the confirmed percent band is above the 28% target, so the resting stop uses that floor'
    order = {
        'symbol': 'BTCUSDT',
        'side': 'SELL',
        'type': 'STOP_LOSS',
        'stopPrice': plan['placed'],
        'stop_target': plan['target'],
        'band_floor': plan['band_floor'],
        'clamped': plan['clamped'],
        'rule_enabled': apply,
        'note': note,
    }
    if quantity is not None:
        order['quantity'] = _step(quantity, BASE_STEP)
    if plan['unplaceable_reason']:
        order['unplaceable_reason'] = plan['unplaceable_reason']
        order['placeable'] = False
    _annotate_venue(order, model, snapshot)
    return order


def _tick(snapshot, tick=None) -> D:
    if tick is not None:
        return number(tick, 'tick', positive=True)
    if snapshot.get('tick_size') not in (None, ''):
        return number(snapshot['tick_size'], 'tick', positive=True)
    return PRICE_STEP


def _format_price(value: D, tick: D) -> str:
    if floor_step(value, tick) == value:
        return format(value.quantize(tick), 'f')
    return format(value, 'f')


def sell_percent_bounds(snapshot: dict, tick=None) -> dict:
    """Lowest buffered sell stop and the tightest raw sell ceiling.

    ``avgPriceMins`` 0 uses the last price. Any other minute count uses the
    fresh average. The floor is ``reference * askMultiplierDown * (1 + buffer)``,
    rounded up to the tick so it cannot fall back through the raw multiplier.
    When both percent filters are present, the higher floor and the lower
    ceiling bind. No filter leaves both bounds empty, so the placed price stays
    the 28% target unless an existing stop is already higher.
    """
    step = _tick(snapshot, tick)
    floors = []
    ceilings = []
    for spec in (snapshot.get('percent_price_by_side'), snapshot.get('percent_price')):
        if not isinstance(spec, dict):
            continue
        reference = snapshot.get('last_price') if spec.get('avg_price_mins') == 0 else snapshot.get('avg_price')
        if reference is None:
            continue
        try:
            ref = number(reference, 'percent reference', positive=True)
        except Blocked:
            continue
        down = spec.get('ask_multiplier_down')
        up = spec.get('ask_multiplier_up')
        if down is not None:
            floors.append(ceil_step(ref * D(down) * (D(1) + STOP_BAND_BUFFER), step))
        if up is not None:
            ceilings.append(ref * D(up))
    return {
        'floor': max(floors) if floors else None,
        'ceiling': min(ceilings) if ceilings else None,
        'tick': step,
    }


def clamp_stop(target, snapshot: dict, *, existing=D(0), tick=None, apply=True) -> dict:
    """Option A price: max(28% target, buffered band floor, existing stop).

    ``apply`` is the confirmed rule. When it is false the placed price is the
    28% target, still never below an existing stop. The band floor is reported
    either way. Multipliers come from the snapshot, which the venue reads from
    the symbol's exchangeInfo. Nothing here hard-codes 0.8.
    """
    bounds = sell_percent_bounds(snapshot, tick)
    step = bounds['tick']
    target_px = floor_step(number(target, 'stop target', positive=True), step)
    held = number(existing, 'existing stop', nonnegative=True)
    floor = bounds['floor']
    ceiling = bounds['ceiling']
    placed = max(target_px, held, floor or D(0)) if apply else max(target_px, held)
    reason = None
    if apply and floor is not None and ceiling is not None and floor > ceiling:
        reason = (
            f'PERCENT_PRICE band floor {_format_price(floor, step)} is above the sell ceiling '
            f'{format(ceiling, "f")}; the clamped stop was not sent and the position is unprotected'
        )
    elif apply and ceiling is not None and placed > ceiling:
        reason = (
            f'PERCENT_PRICE clamped stop {_format_price(placed, step)} is above the sell ceiling '
            f'{format(ceiling, "f")}; the stop was not sent and the position is unprotected'
        )
    return {
        'target': _format_price(target_px, step),
        'band_floor': None if floor is None else _format_price(floor, step),
        'placed': _format_price(placed, step),
        'clamped': apply and floor is not None and floor > target_px and placed == floor,
        'unplaceable_reason': reason,
    }


def stop_band_violation(snapshot: dict, stop_price) -> str | None:
    """Why a sell STOP_LOSS stopPrice is outside the exchange percent band.

    Used only after ``stop_price_percent_band`` is confirmed. Binance documents
    the filter against order ``price``. It does not say a STOP_LOSS trigger is
    included, and ``askMultiplierDown`` is per symbol. The reference is the
    weighted average, or the last price when ``avgPriceMins`` is 0.
    """
    try:
        stop = number(stop_price, 'stopPrice', positive=True)
    except Blocked:
        return None
    reasons = []
    for spec in (snapshot.get('percent_price_by_side'), snapshot.get('percent_price')):
        if not isinstance(spec, dict):
            continue
        mins = spec.get('avg_price_mins')
        reference = snapshot.get('last_price') if mins == 0 else snapshot.get('avg_price')
        if reference is None:
            continue
        ref = D(reference)
        name = spec.get('filter') or 'PERCENT_PRICE'
        down = spec.get('ask_multiplier_down')
        up = spec.get('ask_multiplier_up')
        if down is not None and stop < ref * D(down):
            floor = ref * D(down)
            reasons.append(
                f'{name} askMultiplierDown {down} times reference {ref} '
                f'requires stopPrice >= {floor}')
        if up is not None and stop > ref * D(up):
            ceiling = ref * D(up)
            reasons.append(
                f'{name} askMultiplierUp {up} times reference {ref} '
                f'requires stopPrice <= {ceiling}')
    if not reasons:
        return None
    return ('SELL STOP_LOSS stopPrice ' + format(stop, 'f') + ' is outside '
            + '; '.join(reasons)
            + '; order was not sent and the position is unprotected')


def _annotate_venue(order: dict, model: Model, snapshot: dict) -> None:
    """Check filters for the fixed STOP_LOSS price and owned quantity."""
    known = any(snapshot.get(key) is not None for key in (
        'min_price', 'max_price', 'avg_price', 'min_qty',
    )) or snapshot.get('percent_price_by_side') or snapshot.get('percent_price')
    if not known:
        return
    stop = D(order['stopPrice'])
    price_ok = True
    prior = order.get('unplaceable_reason')
    band = (stop_band_violation(snapshot, order['stopPrice'])
            if snapshot.get('stop_price_percent_band') is True else None)
    if band:
        price_ok = False
        order['unplaceable_reason'] = band
    elif prior:
        price_ok = False
    if snapshot.get('min_price') is not None and stop < D(snapshot['min_price']):
        price_ok = False
    if snapshot.get('max_price') is not None and stop > D(snapshot['max_price']):
        price_ok = False
    notional_ok = True
    reference = snapshot.get('avg_price') or model.close
    if 'quantity' in order and reference is not None:
        notional_ok = D(order['quantity']) * D(reference) >= D(snapshot.get('min_notional') or MIN_NOTIONAL)
        maximum = snapshot.get('max_notional')
        if maximum is not None and D(order['quantity']) * D(reference) > D(maximum):
            notional_ok = False
    qty_ok = _qty_ok(order.get('quantity'), snapshot)
    order['placeable'] = bool(price_ok and notional_ok and qty_ok)
    if order['placeable'] is False and 'unplaceable_reason' not in order:
        order['unplaceable_reason'] = (
            'desired protection fails venue filters; the position is unprotected')


def _qty_ok(quantity, snapshot: dict) -> bool:
    if quantity is None:
        return True
    qty = D(quantity)
    if qty % BASE_STEP:
        return False
    market_step = snapshot.get('market_step')
    if market_step is not None and qty % D(market_step):
        return False
    for key in ('min_qty', 'market_min_qty'):
        if snapshot.get(key) is not None and qty < D(snapshot[key]):
            return False
    for key in ('max_qty', 'market_max_qty'):
        if snapshot.get(key) is not None and qty > D(snapshot[key]):
            return False
    return True


def _step(value: D, step: D) -> str:
    return format(floor_step(value, step).quantize(step), 'f')


def _flat(reason: str) -> dict:
    return {'action': 'flat', 'reason': reason, 'order': None, 'protection': None}


def portfolio(views: dict, owned: dict, snapshot: dict, *, entries_enabled: bool,
              capital_limit: D | None) -> dict:
    """One SMA40 position; a new buy uses current free cash, never expected sale proceeds."""
    if set(views) != {40}:
        raise Unknown('the book requires the single SMA40 sleeve')
    model = views[40]
    if model.close is None:
        return dict(_flat('no completed daily close is available'), sleeves={}, orders=[], protections=[])
    price = D(snapshot.get('last_price') or model.close)
    quantity = D(owned.get(40, 0))
    coins = floor_step(quantity, BASE_STEP)
    minimum = D(snapshot.get('min_notional') or MIN_NOTIONAL)
    if getattr(model, '_owned_dust', False):
        if coins >= BASE_STEP and coins * D(snapshot['avg_price']) >= minimum:
            raise Unknown('retained close residual is now placeable; owner reconciliation required')
        coins = D(0)
    if (D(snapshot['btc']) - quantity) * price >= MIN_NOTIONAL:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    budget = D(snapshot['usdt_free'])
    if capital_limit is not None:
        budget = min(budget, max(capital_limit - D(snapshot['btc']) * price, D(0)))
    item = _decide(model, snapshot, entries_enabled=entries_enabled, owned_btc=coins, budget=budget)
    if snapshot.get('open_orders') and item['action'] == 'enter':
        item = _flat('open order blocks a new buy')
    orders = []
    if item['action'] == 'exit' and coins * D(snapshot['avg_price']) >= minimum:
        orders.append(dict(symbol='BTCUSDT', side='SELL', type='MARKET',
                           quantity=_step(coins, BASE_STEP), sleeves=[40]))
    elif item['action'] == 'enter':
        orders.append(dict(item['order'], sleeves=[40]))
    out = {'orders': orders, 'sleeves': {'40': item}}
    _summarize(out)
    return out


def _summarize(out):
    """Use the final single-sleeve result after every entry and protection filter."""
    item = out['sleeves']['40']
    out['action'], out['reason'] = item['action'], item['reason']
    out['order'] = out['orders'][0] if out['orders'] else None
    item['order'] = ({key: value for key, value in out['order'].items() if key != 'sleeves'}
                     if out['order'] else None)
    out['protections'] = ([dict(item['protection'], sleeves=[40])]
                          if item['action'] in ('enter', 'hold') and item.get('protection') else [])
    if item['action'] == 'exit':
        out['loss_capped'] = False
        out['untradeable'] = not any(order['side'] == 'SELL' for order in out['orders'])
        item['untradeable'] = out['untradeable']


def decision_view(model, position, owners):
    """Copy whose stop floor is the 28% target or a higher confirmed native stop."""
    view = copy.copy(model)
    from .execution import TERMINAL
    def same_position(owner):
        first_ms = int(position['first_ms'])
        recorded = owner.get('position_first_ms')
        if recorded is not None:
            return recorded.get(str(view.sma_window)) == first_ms
        created = owner.get('native_created_ms')
        if type(created) is int and created >= first_ms:
            return True
        if owner.get('native_status') in ('NEW', 'PARTIALLY_FILLED'):
            raise Unknown('active native protection has no proven position identity')
        return False
    proven = [D(o['order']['stopPrice']) for o in owners.values()
              if position and view.sma_window in o['sleeves'] and o['order']['type'] == 'STOP_LOSS'
              and o.get('native_status') in (TERMINAL - {'REJECTED'}) | {'NEW', 'PARTIALLY_FILLED', 'ABSENT'}
              and same_position(o)]
    view._stop_floor = max(proven, default=D(0))
    view.protection = 'resting'
    return view


def decision(views, owned, snapshot, *, positions, owners, entries_enabled, capital_limit,
             crowding_source=None, decision_ms=None):
    """28% trail target, then the percent band when enabled; exits before a new buy."""
    from .crowding import evaluate
    price_views = views
    views = {w: decision_view(v, positions.get(w), owners) for w, v in views.items()}
    out = portfolio(views, owned, snapshot, entries_enabled=entries_enabled, capital_limit=capital_limit)
    forced = False
    for w in views:
        sleeve = out['sleeves'][str(w)]
        stop = sleeve.get('protection')
        if (stop and 'quantity' in stop and D(stop['stopPrice']) >= D(snapshot.get('last_price') or snapshot['avg_price'])
                and sleeve['action'] != 'exit'):
            sleeve.update(action='exit', reason='the protection price is already crossed; the session sells',
                          protection=None, order=None, rearm=False)
            forced = True
    if forced:
        group = [w for w in views if out['sleeves'][str(w)]['action'] == 'exit']
        quantity = sum((floor_step(D(owned[w]), BASE_STEP) for w in group), D(0))
        out['orders'] = [o for o in out['orders'] if o['side'] != 'SELL']
        if quantity * D(snapshot['avg_price']) >= D(snapshot.get('min_notional') or MIN_NOTIONAL):
            out['orders'].append(dict(symbol='BTCUSDT', side='SELL', type='MARKET',
                                      quantity=str(quantity), sleeves=group))
    sells = any(o['side'] == 'SELL' for o in out['orders'])
    free = D(snapshot['usdt_free'])
    cap_remaining = max(D(0), capital_limit - D(snapshot['btc']) * D(snapshot.get('last_price') or snapshot['avg_price'])) if capital_limit is not None else free
    for order in list(out['orders']):
        if order['side'] != 'BUY':
            continue
        quote = floor_step(min(D(order['quoteOrderQty']), free, cap_remaining), QUOTE_STEP)
        if sells or quote < D(snapshot.get('min_notional') or MIN_NOTIONAL):
            out['orders'].remove(order)
            reason = ('a sale must be reconciled before a new buy' if sells
                      else 'available cash or capital ceiling is below the venue minimum after rounding')
            for w in order['sleeves']:
                if out['sleeves'][str(w)]['action'] == 'enter':
                    out['sleeves'][str(w)].update(_flat(reason))
            continue
        order['quoteOrderQty'] = str(quote)
        for w in order['sleeves']:
            sleeve_order = out['sleeves'][str(w)].get('order')
            if sleeve_order and sleeve_order['side'] == 'BUY':
                sleeve_order['quoteOrderQty'] = str(quote / len(order['sleeves']))
        free -= quote
        cap_remaining -= quote
    diagnostics = []
    for order in list(out['orders']):
        if order['side'] != 'BUY':
            continue
        factor, diagnostic = evaluate(crowding_source, price_views[next(iter(price_views))], decision_ms)
        def held(w):
            position = positions.get(w) or {}
            closed_dust = position.get('dust') is True and getattr(views[w], '_owned_dust', False)
            return D(owned[w]) >= BASE_STEP or (D(owned[w]) > 0 and not closed_dust)
        if any(held(w) for w in order['sleeves']):
            factor = D(0)
            diagnostic['blocked_reason'] = 'held_sleeve_no_topup'
        quote = floor_step(D(order['quoteOrderQty']) * factor, QUOTE_STEP)
        cause = diagnostic['blocked_reason']
        if quote < D(snapshot.get('min_notional') or MIN_NOTIONAL):
            out['orders'].remove(order)
            cause = cause or 'below_minimum_after_rounding'
            for w in order['sleeves']:
                out['sleeves'][str(w)].update(_flat('new buy is blocked: ' + cause))
            quote = D(0)
        else:
            order['quoteOrderQty'] = str(quote)
            for w in order['sleeves']:
                sleeve_order = out['sleeves'][str(w)].get('order')
                if sleeve_order and sleeve_order['side'] == 'BUY':
                    sleeve_order['quoteOrderQty'] = str(quote / len(order['sleeves']))
        diagnostic.update(resulting_quote=quote, blocked_reason=cause)
        diagnostics.append(diagnostic)
    out['crowding'] = serial(diagnostics)
    _summarize(out)
    return out
