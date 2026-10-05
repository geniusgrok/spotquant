"""Fixed BTC base/tactical cores over the existing fill-owned spot lifecycle."""
from bisect import bisect_right
from contextlib import contextmanager
from decimal import Decimal as D
from types import SimpleNamespace

DAY = 86400000
RULE = 'btc-history-spot-core-2018-2019-v1'
MODES = ('base', 'defensive-base')


def features(bars, mode):
    if mode not in MODES:
        raise ValueError('unknown registered spot history core')
    rows = []
    for i, (t, o, h, l, c) in enumerate(bars):
        slow = sum((r[4] for r in bars[i-99:i+1]), D(0))/100 if i >= 99 else None
        fast = sum((r[4] for r in bars[i-19:i+1]), D(0))/20 if i >= 19 else None
        base = D('.25') if slow is not None and (mode == 'base' or c > slow) else D(0)
        tactical = D('.65') if slow is not None and fast > slow else D(0)
        tr = [max(row[2]-row[3], abs(row[2]-bars[j-1][4]), abs(row[3]-bars[j-1][4]))
              for j, row in enumerate(bars[max(1, i-13):i+1], start=max(1, i-13))]
        atr = sum(tr, D(0))/14 if len(tr) == 14 else D(0)
        rows.append(dict(available_ms=t+DAY+60000, day_ms=t, close=c, slow=slow,
                         components={30:base, 40:tactical, 50:D(0)}, fraction=base+tactical,
                         trail=max(D('.10'), min(D('.30'), 4*atr/c)) if atr else D('.30')))
    return rows


class Signals:
    def __init__(self, bars, mode, source_sha256):
        self.mode, self.source_sha256 = mode, source_sha256
        self.rows = features(bars, mode)
        self.times = [r['available_ms'] for r in self.rows]

    def at(self, call):
        i = bisect_right(self.times, call)-1
        return self.rows[i] if i >= 0 else None


def decision(views, owned, snapshot, *, positions, owners, entries_enabled,
             capital_limit, signals, decision_ms, blocked_sleeves=None, **unused):
    from spotquant.preview import BASE_STEP, QUOTE_STEP, MIN_NOTIONAL, _step, _protection, _merge_protections
    from spotquant.core import protection_view
    from spotquant.types import Unknown, floor_step
    feature = signals.at(decision_ms)
    if feature is None:
        raise Unknown('qualified causal daily input required')
    price = D(snapshot['avg_price'])
    if not price.is_finite() or price <= 0:
        raise Unknown('invalid current price')
    coins = {w:D(owned.get(w, 0)) for w in views}
    total = sum(coins.values(), D(0))
    if abs(D(snapshot['btc'])-total)*price >= MIN_NOTIONAL:
        raise Unknown('BTC balance has no recorded fill ownership')
    equity = D(snapshot['usdt_free'])+D(snapshot.get('usdt_locked', 0))+total*price
    capital = max(D(0), min(equity, capital_limit) if capital_limit is not None else equity)
    pviews = {w:protection_view(v, positions.get(w), owners) for w, v in views.items()}
    for view in pviews.values():
        view.trail = feature['trail']
    items = {w:dict(action='hold' if coins[w] else 'flat', order=None,
                   reason='fixed base plus independent tactical component',
                   protection=_protection(pviews[w], coins[w], snapshot)
                   if floor_step(coins[w], BASE_STEP)*price >= MIN_NOTIONAL else None) for w in views}
    orders, remaining = [], D(snapshot['usdt_free'])*D('.998')
    for w in sorted(views):
        target = floor_step(capital*feature['components'][w]/price, BASE_STEP)
        delta = target-coins[w]
        significant = abs(delta)*price >= max(MIN_NOTIONAL, equity*D('.05'))
        breached = items[w]['protection'] and D(items[w]['protection']['stopPrice']) >= price
        if breached or delta < 0 and (significant or target == 0):
            qty = floor_step(coins[w] if breached else min(coins[w], -delta), BASE_STEP)
            if qty*price >= MIN_NOTIONAL:
                orders.append(dict(symbol='BTCUSDT', side='SELL', type='MARKET', quantity=_step(qty, BASE_STEP), sleeves=[w]))
                if breached or target == 0:
                    items[w].update(action='exit', protection=None, loss_capped=False)
        elif delta > 0 and significant and entries_enabled and not snapshot.get('open_orders'):
            if (blocked_sleeves or {}).get(str(w), -1) >= views[w].last:
                continue
            quote = floor_step(min(delta*price, remaining), QUOTE_STEP)
            if quote >= MIN_NOTIONAL:
                order = dict(symbol='BTCUSDT', side='BUY', type='MARKET', quoteOrderQty=_step(quote, QUOTE_STEP), sleeves=[w])
                orders.append(order)
                remaining -= quote
                items[w].update(action='enter', order=order,
                               protection=_protection(pviews[w], coins[w] or None, snapshot))
    # Reductions settle first; independent buys cannot spend unconfirmed proceeds.
    orders.sort(key=lambda order: order['side'] != 'SELL')
    return dict(action='exit' if orders and orders[0]['side'] == 'SELL' else 'enter' if orders else 'hold' if total else 'flat',
                reason='registered BTC base/tactical target with fill-owned catastrophe stops',
                order=orders[0] if orders else None, orders=orders,
                sleeves={str(w):items[w] for w in sorted(items)},
                protections=_merge_protections(items, pviews, snapshot, views[30]),
                rule=RULE+':'+signals.mode, target_fraction=str(feature['fraction']),
                target_components={str(w):str(v) for w, v in feature['components'].items()},
                feature_day_ms=feature['day_ms'], available_ms=feature['available_ms'], loss_capped=False)


@contextmanager
def runtime(signals, *, origin=1504224000000):
    """Explicit offline research scope; preserves all canonical durable guards."""
    from spotquant import model, session
    from spotquant.types import Blocked
    old = session.RULE, session.portfolio, session._entries_blocked, session.RECORDED_LIMITS, model.ORIGIN
    blocked = {}

    def record(models, exit_through):
        blocked.clear()
        blocked.update(exit_through)
        return old[2](models, exit_through)

    def portfolio(*args, **kwargs):
        kwargs.update(signals=signals, blocked_sleeves=blocked)
        return decision(*args, **kwargs)

    session.RULE = RULE+':'+signals.mode+':'+signals.source_sha256
    session.portfolio, session._entries_blocked, model.ORIGIN = portfolio, record, origin
    session.RECORDED_LIMITS = dict(old[3], new_entry_policy=session.RULE,
        public_features='qualified completed spot daily end+60s; base25/tactical65; cap90',
        path_convention='owned ATR catastrophe stops; original follow catchup; no native proof')

    def run(config, venue, **kwargs):
        if not getattr(venue, 'offline', False):
            raise Blocked('history core execution requires an offline research venue')
        return session.run(config, venue, **kwargs)

    def cycle(venue, *args, **kwargs):
        if not getattr(venue, 'offline', False):
            raise Blocked('history core execution requires an offline research venue')
        return session.cycle(venue, *args, **kwargs)

    try:
        yield SimpleNamespace(run=run, cycle=cycle)
    finally:
        session.RULE, session.portfolio, session._entries_blocked, session.RECORDED_LIMITS, model.ORIGIN = old
