"""One BTC target book over the existing fill-owned accounting allocations."""
from decimal import Decimal as D

from .target import RULE, DEADBAND, exposure
from .preview import (BASE_STEP, QUOTE_STEP, MIN_NOTIONAL, _step, _protection,
                      _merge_protections, decision_view)
from .types import Unknown, floor_step


def protection_view(model, position, owners):
    view = decision_view(model, position, owners)
    # Normal forecast exit is independent of the native catastrophe stop.
    view.trail = D('.20')
    view.repair = False
    view.repair_peak = None
    return view


def decision(views, owned, snapshot, *, positions, owners, entries_enabled,
             capital_limit, mode='trend', **unused):
    reference = views[30]
    if reference.close is None:
        raise Unknown('completed price history required')
    price = D(snapshot['avg_price'])
    if not price.is_finite() or price <= 0:
        raise Unknown('invalid current price')
    coins = {w: D(owned.get(w, 0)) for w in views}
    total = sum(coins.values(), D(0))
    if abs(D(snapshot['btc'])-total)*price >= MIN_NOTIONAL:
        raise Unknown('BTC balance has no recorded spotquant fill')
    equity = D(snapshot['usdt_free']) + D(snapshot.get('usdt_locked', 0)) + total*price
    capital = min(equity, capital_limit) if capital_limit is not None else equity
    fraction = exposure(reference.closes, mode=mode, spot=True)
    target = floor_step(max(D(0), capital)*fraction/price, BASE_STEP)
    pviews = {w: protection_view(v, positions.get(w), owners) for w, v in views.items()}
    items = {w: dict(action='hold' if coins[w] else 'flat', order=None,
                    reason='continuous BTC target',
                    protection=_protection(pviews[w], coins[w], snapshot) if floor_step(coins[w], BASE_STEP)*price >= MIN_NOTIONAL else None)
             for w in views}
    breached = [w for w in views if items[w]['protection'] and
                D(items[w]['protection']['stopPrice']) >= price]
    orders = []
    delta = target-total
    significant = abs(delta)*price >= max(MIN_NOTIONAL, equity*DEADBAND)
    if breached or (delta < 0 and (significant or target == 0)):
        group = breached or [w for w in views if coins[w] >= BASE_STEP]
        quantity = sum((coins[w] for w in group), D(0)) if breached else -delta
        quantity = floor_step(min(quantity, sum((coins[w] for w in group), D(0))), BASE_STEP)
        if quantity*price >= MIN_NOTIONAL:
            orders.append(dict(symbol='BTCUSDT', side='SELL', type='MARKET',
                               quantity=_step(quantity, BASE_STEP), sleeves=sorted(group)))
            for w in group:
                if target == 0 or w in breached:
                    items[w].update(action='exit', protection=None, loss_capped=False)
    elif delta > 0 and significant and entries_enabled and not snapshot.get('open_orders'):
        # Existing labels are allocation ownership only, not three strategy votes.
        quote = floor_step(min(delta*price, D(snapshot['usdt_free'])*D('.998')), QUOTE_STEP)
        if quote >= MIN_NOTIONAL:
            order = dict(symbol='BTCUSDT', side='BUY', type='MARKET',
                         quoteOrderQty=_step(quote, QUOTE_STEP), sleeves=[40])
            orders.append(order)
            items[40].update(action='enter', order=order,
                             protection=_protection(pviews[40], coins[40] or None, snapshot))
    action = ('exit' if orders and orders[0]['side'] == 'SELL' else 'enter' if orders
              else 'hold' if total else 'flat')
    return dict(action=action, reason='continuous target; normal reduction and catastrophe protection separate',
                order=orders[0] if orders else None, orders=orders,
                sleeves={str(w): items[w] for w in sorted(items)},
                protections=_merge_protections(items, pviews, snapshot, reference),
                rule=RULE, mode=mode, target_fraction=str(fraction), target_btc=str(target),
                deadband_equity=str(DEADBAND), loss_capped=False)
