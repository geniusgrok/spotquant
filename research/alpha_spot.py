"""Registered Spot alpha/beta variants through the unchanged finite Lifecycle."""
import argparse
import bisect
import copy
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

from research import complete_spot as complete
from research.market import load_daily, file_digest
from research.rebuild import END_MS, START_MS, source_identity
from spotquant import execution, follow, model, session
from spotquant.model import DAY
from spotquant.preview import (BASE_STEP, MIN_NOTIONAL, PRICE_STEP, QUOTE_STEP,
                               portfolio, _merge_protections)
from spotquant.state import State
from spotquant.types import Blocked, Unknown, floor_step, serial

TACTICAL = (30, 40, 50)
CANDIDATES = ('consensus', 'trend-reentry', 'target-participation', 'atr-close',
              'atr-stop', 'core-permanent', 'core-slow')
SCENARIOS = complete.SCENARIOS
SPEC = Path(__file__).with_name('alpha_beta_spec.json')
CUTOFF = 1640995200000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def components_for(candidate, combo=()):
    parts = tuple(combo) if candidate == 'combo' else (() if candidate in ('consensus', 'core0') else (candidate,))
    if (candidate not in (*CANDIDATES, 'combo', 'core0') or
            any(p not in CANDIDATES[1:] for p in parts) or len(set(parts)) != len(parts)
            or sum(p.startswith('core-') for p in parts) > 1 or (candidate == 'combo' and not parts)):
        raise ValueError('unregistered or incompatible candidate components')
    return tuple(p for p in CANDIDATES if p in parts)


def calibration(path, candidate):
    if path is None:
        return {'scale': '1', 'sha256': None}
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    try:
        profile = data['profiles'][candidate]
        scale = D(profile['scale'])
        source_hash = profile['base_bundle_sha256']
        if (data['format'] != 1 or data['cutoff_ms'] != CUTOFF
                or data['spec_sha256'] != digest(SPEC.read_bytes())
                or profile['effective_from_ms'] != CUTOFF or profile['calibration_end_ms'] != CUTOFF
                or profile['training_end_day_exclusive'] != '2022-01-01'
                or profile['baseline_candidate'] != 'consensus' or not scale.is_finite()
                or not 0 <= scale <= 1 or not isinstance(profile['scale'], str)
                or len(source_hash) != 64 or any(c not in '0123456789abcdef' for c in source_hash)):
            raise ValueError('calibration identity or training boundary')
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise ValueError('invalid risk calibration') from exc
    return dict(profile, sha256=digest(raw))


def signal_key(side, bar, group):
    return f'{side}:{bar}:' + ','.join(map(str, sorted(group)))


def cached_fills(state):
    # No adapter requests: Lifecycle.verify has populated this immutable cache.
    rows = []
    for payload, in state.db.execute('SELECT payload FROM fills ORDER BY time_ms,id'):
        row = json.loads(payload)
        for key in ('qty', 'quote', 'price', 'commission'):
            row[key] = D(row[key])
        rows.append(row)
    return rows


def subpools(anchor, fills, owners, fraction, snapshot):
    """Two independently compounding ledgers, including all fees and residual dust."""
    pools = {'core': {'cash': D(anchor['cash']) * fraction, 'btc': D(0)},
             'tactical': {'cash': D(anchor['cash']) * (1 - fraction), 'btc': D(anchor['btc'])}}
    for row in fills:
        owner = owners.get(str(row['order_id']))
        if owner is None:
            raise Unknown('fill lacks a durable subpool owner')
        group = owner['sleeves']
        if 200 in group and group != [200]:
            raise Unknown('core and tactical allocation cannot mix')
        if not group or any(w not in (*TACTICAL, 200) for w in group):
            raise Unknown('invalid subpool allocation')
        pool = pools['core' if group == [200] else 'tactical']
        fee, asset = row['commission'], row['commission_asset']
        if fee and asset not in ('BTC', 'USDT'):
            raise Unknown('unreconciled subpool commission')
        sign = 1 if row['buyer'] else -1
        pool['btc'] += sign * row['qty'] - (fee if asset == 'BTC' else D(0))
        pool['cash'] -= sign * row['quote'] + (fee if asset == 'USDT' else D(0))
        if min(pool.values()) < -D('1e-20'):
            raise Unknown('subpool borrowed cash or BTC')
    if (abs(sum(p['cash'] for p in pools.values()) - D(snapshot['usdt_free'])
            - D(snapshot['usdt_locked'])) > D('1e-8')
            or abs(sum(p['btc'] for p in pools.values()) - D(snapshot['btc'])) > D('1e-8')):
        raise Unknown('subpool ledgers do not conserve the real account')
    return pools


class Policy:
    def __init__(self, candidate, venue, features=None, *, combo=(), risk=None, core_fraction=D('.20')):
        self.candidate, self.venue = candidate, venue
        self.parts = components_for(candidate, combo)
        self.core_mode = next((p for p in self.parts if p.startswith('core-')), None)
        self.fraction = D(core_fraction) if self.core_mode else D(0)
        if self.fraction not in (D(0), D('.20')):
            raise ValueError('only registered core ratio or test no-op is allowed')
        self.core = bool(self.fraction)
        self.risk = risk or {'scale': '1', 'sha256': None}
        if not D(self.risk['scale']).is_finite() or not 0 <= D(self.risk['scale']) <= 1:
            raise ValueError('invalid fixed risk scale')
        self.identity = {'candidate': candidate, 'components': list(self.parts),
                         'core_mode': self.core_mode, 'core_fraction': str(self.fraction),
                         'spec_sha256': digest(SPEC.read_bytes()), 'calibration_sha256': self.risk['sha256'],
                         'risk_scale': self.risk['scale']}
        self.bound = bool(self.parts or self.risk['sha256'] or D(self.risk['scale']) != 1)
        self.state = None
        self.filters = {'blocked': 0, 'missing': 0}
        self.journal, self.last_journal = [], None
        self.final_pools = None
        self.bar_times = [r[0] for r in venue.all_bars]
        self.atr_cache = {}

    def atr(self, last):
        if last not in self.atr_cache:
            index = bisect.bisect_right(self.bar_times, last) - 1
            if index < 14:
                return None
            bars = self.venue.all_bars
            ranges = [max(bars[i][2] - bars[i][3], abs(bars[i][2] - bars[i-1][4]),
                          abs(bars[i][3] - bars[i-1][4])) for i in range(index - 13, index + 1)]
            self.atr_cache[last] = sum(ranges, D(0)) / 14
        return self.atr_cache[last]

    def records(self, fills, owners, signals):
        records = {}
        for row in fills:
            owner = owners.get(str(row['order_id']))
            if owner is None:
                raise Unknown('unattributed research fill')
            key = str(row['order_id'])
            meta = signals.get(signal_key(owner['order']['side'], owner['signal_ms'], owner['sleeves']), {})
            event = records.setdefault(key, {'owner': owner, 'order_id': key, 'qty': D(0), 'quote': D(0),
                'net_btc': D(0),
                'first_ms': row['time'], 'last_ms': row['time'], 'meta': meta})
            event['qty'] += row['qty']
            event['quote'] += row['quote']
            event['net_btc'] += follow._base_delta(row)
            event['last_ms'] = max(event['last_ms'], row['time'])
        return list(records.values())

    def recovery(self, view, window, records, bulls, exit_through):
        if not (view.bull and view.streak >= 2 and view.crash_ok and not view.extended and bulls >= 2):
            return False
        episode = view.last - (view.streak - 1) * DAY
        relevant = [r for r in records if window in r['owner']['sleeves'] and r['last_ms'] >= episode]
        if any(window in r['meta'].get('reentry_sleeves', []) and r['owner']['order']['side'] == 'BUY'
               for r in relevant):
            return False
        sells = [r for r in relevant if r['owner']['order']['side'] == 'SELL']
        if not sells:
            return False
        sale = max(sells, key=lambda r: r['last_ms'])
        kind = 'stop' if sale['owner']['order']['type'] == 'STOP_LOSS' else sale['meta'].get('exit_types', {}).get(str(window))
        # Close timestamps must be after the actual fill; exit-day close may be first.
        exit_day = sale['last_ms'] // DAY * DAY
        return (kind in ('stop', 'extended') and view.last >= exit_day + DAY
                and view.last > int(exit_through.get(str(window), -1))
                and view.close >= sale['quote'] / sale['qty'])

    def adjust_stop(self, view, position, owners, atr):
        view.trail = min(D('.30'), max(D('.10'), 4 * atr / view.close))
        proven = [D(o['order']['stopPrice']) for o in owners.values()
                  if position and view.sma_window in o['sleeves'] and o['order']['type'] == 'STOP_LOSS'
                  and o.get('native_status') in (execution.TERMINAL - {'REJECTED'}) | {'NEW', 'PARTIALLY_FILLED'}
                  and o['signal_ms'] >= int(position['first_ms']) // DAY * DAY - DAY]
        floor = max(proven, default=D(0))
        view.stop_price = lambda peak: max(floor, D(peak) * (1 - view.trail))
        # Catch-up bars update peaks, not fictitious uninstalled ATR stops.
        view.protection = 'resting'

    def opportunity(self, window, view, mechanism, records):
        trigger = None
        exit_link = {}
        if window == 200:
            # Actual allocated net fills reconstruct the campaign, including BTC
            # fees and retained dust. A partial sale does not create a new entry.
            balance = D(0)
            exit_boundary = 0
            for record in sorted(records, key=lambda r: (r['last_ms'], int(r['order_id']))):
                if record['owner']['sleeves'] != [200]:
                    continue
                prior = balance
                balance += record['net_btc']
                if record['owner']['order']['side'] == 'SELL' and prior >= BASE_STEP and balance < BASE_STEP:
                    exit_boundary = record['last_ms'] // DAY * DAY
                    exit_link = {'exit_order_id': record['order_id'], 'exit_fill_ms': record['last_ms']}
            if self.core_mode == 'core-permanent' or view.bull:
                trigger = max(int(self.state.get('entries_after') or view.last) + DAY, exit_boundary)
                if self.core_mode == 'core-slow':
                    trigger = max(trigger, view.last - max(0, view.streak - 1) * DAY)
        elif mechanism == 'target-participation':
            trigger = view.last
        elif view.cap_enter:
            trigger = view.last
        elif mechanism == 'trend-reentry':
            exits = [r for r in records if window in r['owner']['sleeves'] and r['owner']['order']['side'] == 'SELL']
            if exits:
                trigger = max(r['last_ms'] for r in exits) // DAY * DAY + DAY
        elif view.enter:
            trigger = view.last - max(0, view.streak - view.confirm) * DAY
        if trigger is None or trigger > view.last:
            return {'id': None, 'trigger_bar_ms': None, 'trigger_close': None, 'age_ms': None, **exit_link}
        index = bisect.bisect_left(self.bar_times, trigger)
        close = self.venue.all_bars[index][4] if index < len(self.bar_times) and self.bar_times[index] == trigger else None
        return {'id': f'{self.candidate}:{window}:{trigger}', 'trigger_bar_ms': trigger,
                'trigger_close': close, 'age_ms': self.venue.now_ms - trigger - DAY, **exit_link}

    def __call__(self, views, owned, snapshot, **kwargs):
        # Historical research owns its decision context; canonical context is explicit.
        for key in ('positions', 'owners', 'allocation_scale', 'crowding_source', 'decision_ms'):
            kwargs.pop(key, None)
        original_views = views
        views = {w: copy.copy(v) for w, v in views.items()}
        last = views[30].last
        signals = dict(self.state.get('alpha_signals') or {}) if self.bound else dict(getattr(self, 'last_signals', {}))
        fills = cached_fills(self.state)
        owners = getattr(self.state, '_execution_owners', {}) or {}
        records = self.records(fills, owners, signals)
        positions, _, _, exit_through, _ = session._stored(self.state)
        atr = self.atr(last)
        recovery = []
        for w in TACTICAL:
            view = views[w]
            if 'atr-close' in self.parts and atr is not None and view.entry is not None:
                view.adverse_stop = min(D('.10'), max(D('.02'), 2 * atr / view.entry))
                view.adverse = not view.repair and view.close <= view.entry * (1 - view.adverse_stop)
            if 'atr-stop' in self.parts and atr is not None:
                self.adjust_stop(view, positions.get(w), owners, atr)
            if ('trend-reentry' in self.parts and view.need_reset and not view.enter and not view.cap_enter
                    and (positions.get(w) is None or positions[w].get('dust'))
                    and self.recovery(view, w, records, sum(bool(views[x].bull) for x in TACTICAL), exit_through)):
                view.enter = True
                recovery.append(w)
        tactical_views = {w: views[w] for w in TACTICAL}
        tactical_owned = {w: owned[w] for w in TACTICAL}
        tactical_snapshot = snapshot
        tactical_kwargs = dict(kwargs)
        pools = None
        if self.core:
            pools = subpools(self.state.get('execution_anchor'), fills, owners, self.fraction, snapshot)
            self.final_pools = serial(pools)
            tactical_snapshot = dict(snapshot, btc=str(pools['tactical']['btc']),
                                      usdt_free=str(min(pools['tactical']['cash'], D(snapshot['usdt_free']))),
                                      usdt_locked='0')
            if kwargs['capital_limit'] is not None:
                tactical_kwargs['capital_limit'] = max(D(0), kwargs['capital_limit'] - pools['core']['btc'] * D(snapshot['avg_price']))
        decision = portfolio(tactical_views, tactical_owned, tactical_snapshot, **tactical_kwargs)
        exit_types = {str(w): self.exit_type(views[w]) for w in TACTICAL
                      if decision['sleeves'][str(w)]['action'] == 'exit'}
        mechanism = 'trend-reentry' if any(w in recovery for o in decision['orders'] if o['side'] == 'BUY' for w in o['sleeves']) else 'consensus'
        if 'target-participation' in self.parts and not decision['orders']:
            held = [w for w in TACTICAL if views[w].bull and D(owned[w]) > BASE_STEP
                    and not getattr(views[w], '_owned_dust', False)
                    and decision['sleeves'][str(w)]['action'] == 'hold']
            bought = any(r['meta'].get('mechanism') == 'target-participation'
                         and r['owner']['signal_ms'] == last for r in records)
            cash, btc = D(tactical_snapshot['usdt_free']), D(tactical_snapshot['btc'])
            mark = D(snapshot['avg_price'])
            equity = cash + btc * mark
            if (held and sum(bool(views[w].bull) for w in TACTICAL) >= 2 and kwargs['entries_enabled'] and not snapshot.get('open_orders')
                    and not bought and equity > 0 and btc * mark / equity < D('.85')):
                spend = min(cash, max(D(0), D('.90') * equity - btc * mark))
                if tactical_kwargs['capital_limit'] is not None:
                    spend = min(spend, max(D(0), tactical_kwargs['capital_limit'] - btc * mark))
                spend = floor_step(spend, QUOTE_STEP)
                if spend >= MIN_NOTIONAL:
                    decision['orders'].append(dict(symbol='BTCUSDT', side='BUY', type='MARKET',
                                                    quoteOrderQty=str(spend), sleeves=held))
                    mechanism = 'target-participation'
        if self.core:
            core = views[200]
            # Policy-only overrides; the stored SMA200 model keeps honest flags.
            core.bull = self.core_mode == 'core-permanent' or core.bull
            core.enter = core.bull
            core.extended = core.adverse = core.cap_enter = core.repair = False
            blocked = core.last <= int(exit_through.get('200', -1))
            core_snapshot = dict(snapshot, btc=str(pools['core']['btc']),
                                 usdt_free=str(min(pools['core']['cash'], D(snapshot['usdt_free']))), usdt_locked='0')
            cap = kwargs['capital_limit']
            if cap is not None:
                cap = max(D(0), cap - pools['tactical']['btc'] * D(snapshot['avg_price']))
            core_decision = portfolio({200: core}, {200: owned[200]}, core_snapshot,
                                     entries_enabled=kwargs['entries_enabled'] and not blocked,
                                     capital_limit=cap, consensus=False)
            decision['sleeves'].update(core_decision['sleeves'])
            decision['orders'] += core_decision['orders']
            decision['protections'] += core_decision['protections']
            if core_decision['sleeves']['200']['action'] == 'exit':
                exit_types['200'] = self.exit_type(core)
        # ATR stop through the actual decision mark is a legal market reduction.
        if 'atr-stop' in self.parts:
            forced = False
            for w in TACTICAL:
                sleeve = decision['sleeves'][str(w)]
                stop = sleeve.get('protection')
                if (stop and 'quantity' in stop and D(stop['stopPrice']) >= D(snapshot['avg_price'])
                        and sleeve['action'] != 'exit'):
                    sleeve.update(action='exit', protection=None, order=None)
                    exit_types[str(w)] = 'stop-through'
                    forced = True
            if forced:
                group = [w for w in TACTICAL if decision['sleeves'][str(w)]['action'] == 'exit']
                quantity = sum((floor_step(D(owned[w]), BASE_STEP) for w in group), D(0))
                decision['orders'] = [o for o in decision['orders'] if o['side'] != 'SELL' or o['sleeves'] == [200]]
                if quantity * D(snapshot['avg_price']) >= MIN_NOTIONAL:
                    decision['orders'].append(dict(symbol='BTCUSDT', side='SELL', type='MARKET', quantity=str(quantity), sleeves=group))
            tactical_decisions = {w: decision['sleeves'][str(w)] for w in TACTICAL}
            decision['protections'] = _merge_protections(tactical_decisions, tactical_views, snapshot, views[30]) + [
                p for p in decision['protections'] if p['sleeves'] == [200]]
        # Exits have priority. Independently funded core never spends expected proceeds.
        sells = any(o['side'] == 'SELL' for o in decision['orders'])
        scale = D(self.risk['scale']) if self.venue.now_ms >= CUTOFF else D(1)
        desired = copy.deepcopy(decision['orders'])
        constraints = []
        if self.core or scale != 1 or 'target-participation' in self.parts or 'atr-stop' in self.parts:
            free = D(snapshot['usdt_free'])
            cap_remaining = max(D(0), kwargs['capital_limit'] - D(snapshot['btc']) * D(snapshot['avg_price'])) if kwargs['capital_limit'] is not None else free
            for order in list(decision['orders']):
                if order['side'] != 'BUY':
                    continue
                budget = min(free, cap_remaining)
                if pools:
                    budget = min(budget, pools['core' if order['sleeves'] == [200] else 'tactical']['cash'])
                quote = floor_step(min(D(order['quoteOrderQty']), budget) * scale, QUOTE_STEP)
                if sells or quote < MIN_NOTIONAL:
                    constraints.append({'sleeves': order['sleeves'], 'reason': 'exit_priority' if sells else 'cash_cap_or_risk_scale_below_minimum'})
                    decision['orders'].remove(order)
                    for w in order['sleeves']:
                        if decision['sleeves'][str(w)]['action'] == 'enter':
                            decision['sleeves'][str(w)].update(action='flat', order=None, protection=None)
                    continue
                if quote < D(order['quoteOrderQty']):
                    constraints.append({'sleeves': order['sleeves'], 'reason': 'free_cash_capital_ceiling_or_fixed_risk_scale'})
                order['quoteOrderQty'] = str(quote)
                for w in order['sleeves']:
                    sleeve_order = decision['sleeves'][str(w)].get('order')
                    if sleeve_order and sleeve_order['side'] == 'BUY':
                        sleeve_order['quoteOrderQty'] = str(quote / len(order['sleeves']))
                free -= quote
                cap_remaining -= quote
        for order in decision['orders']:
            key = signal_key(order['side'], last, order['sleeves'])
            signals.setdefault(key, {'mechanism': self.core_mode if order['sleeves'] == [200] else mechanism,
                                     'exit_types': exit_types, 'reentry_sleeves': [w for w in order['sleeves'] if w in recovery],
                                     'decision_ms': self.venue.now_ms,
                                     'decision_price': str(snapshot['avg_price'])})
        if self.bound:
            self.state._alpha_values = {'alpha_identity': self.identity, 'alpha_signals': signals}
        self.last_signals = signals
        decision['order'] = decision['orders'][0] if decision['orders'] else None
        # Deduplicate unchanged polling observations without advancing the venue clock.
        event = serial({'event': 'decision', 'completed_bar_ms': last,
                        'opportunity_id': f'{self.candidate}:{last}',
                        'opportunity_age_ms': self.venue.now_ms - last - DAY,
                        'trigger_close': original_views[30].close, 'decision_price': snapshot['avg_price'],
                        'entries_enabled': kwargs['entries_enabled'], 'risk_scale': scale,
                        'idle_cash_usdt': snapshot['usdt_free'], 'capital_limit': kwargs['capital_limit'],
                        'pool_ledgers': pools, 'constraints': constraints, 'desired_orders': desired, 'accepted_orders': decision['orders'],
                        'exit_types': exit_types, 'mechanism': mechanism,
                        'sleeves': {str(w): {'action': s['action'], 'reason': s['reason'],
                                            'bull': original_views[w].bull, 'owned_btc': owned[w],
                                            'opportunity': self.opportunity(w, original_views[w],
                                                mechanism if w in recovery or mechanism == 'target-participation' else 'consensus', records)}
                                    for w, s in ((int(k), v) for k, v in decision['sleeves'].items())}})
        key = json.dumps({k: v for k, v in event.items() if k != 'opportunity_age_ms'}, sort_keys=True)
        if key != self.last_journal:
            self.journal.append(dict(event, decision_ms=self.venue.now_ms))
            self.last_journal = key
        return decision

    @staticmethod
    def exit_type(view):
        if getattr(view, 'protection', 'resting') in ('breached', 'through_close') or (
                view.position_peak is not None and view.stop_price(view.position_peak) >= view.close):
            return 'stop-through'
        if view.adverse:
            return 'adverse'
        if view.extended:
            return 'extended'
        return 'sma'


@contextmanager
def configured(policy):
    originals = [(module, name, getattr(module, name)) for module, names in (
        (model, ('SLEEVES', 'Model')), (session, ('SLEEVES', 'Model', 'RULE', 'RECORDED_LIMITS', 'portfolio', 'State')),
        (follow, ('Model',)), (execution, ('SLEEVES', '_protection'))) for name in names]
    base_model = model.Model
    base_protection = execution._protection

    def remainder_protection(view, quantity, snapshot):
        atr = policy.atr(view.last)
        if view.sma_window in TACTICAL and atr is not None:
            view = copy.copy(view)
            position = (policy.state.get('positions') or {}).get(str(view.sma_window))
            policy.adjust_stop(view, position, getattr(policy.state, '_execution_owners', {}) or {}, atr)
        return base_protection(view, quantity, snapshot)

    class ResearchModel(base_model):
        def __init__(self, window=40, *args, **kwargs):
            if window == 200 and not args:
                kwargs.update(extend=0, cap_drop=0, cap_bounce=0, adverse_stop=0)
            super().__init__(window, *args, **kwargs)

        def checkpoint(self):
            body = {'model': super().checkpoint(), 'identity': policy.identity}
            return dict(body, sha256=digest(json.dumps(body, sort_keys=True).encode()))

        @classmethod
        def restore(cls, saved):
            try:
                body = {'model': saved['model'], 'identity': saved['identity']}
                if saved['identity'] != policy.identity or saved['sha256'] != digest(json.dumps(body, sort_keys=True).encode()):
                    raise ValueError('research identity')
                return base_model.restore.__func__(cls, saved['model'])
            except (KeyError, TypeError, ValueError) as exc:
                raise Blocked('research checkpoint candidate, core mode, ratio or calibration mismatch') from exc

    class ResearchState(State):
        def __enter__(self):
            result = super().__enter__()
            prior = result.get('alpha_identity')
            if (prior is not None and prior != policy.identity) or (policy.bound and result.get('models') is not None and prior is None):
                super().__exit__(None, None, None)
                raise Blocked('research account identity mismatch')
            policy.state = result
            return result

        def set_many(self, values):
            if 'models' in values and policy.bound:
                values = dict(values, **getattr(self, '_alpha_values', {}))
            return super().set_many(values)

    try:
        session.portfolio, session.State = policy, ResearchState
        if policy.bound:
            model.Model = session.Model = follow.Model = ResearchModel
            session.RULE = 'alpha-spot:' + digest(json.dumps(policy.identity, sort_keys=True).encode())
            session.RECORDED_LIMITS = dict(session.RECORDED_LIMITS, research_identity=policy.identity)
        if 'atr-stop' in policy.parts:
            execution._protection = remainder_protection
        if policy.core:
            model.SLEEVES = session.SLEEVES = execution.SLEEVES = (*TACTICAL, 200)
            session.RECORDED_LIMITS = dict(session.RECORDED_LIMITS, sleeves=list(session.SLEEVES))
        yield
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


def measure(candidate, scenario, bars, starts, fx, features=None, *, limit=None, risk=None, combo=(), core_fraction=D('.20')):
    """Reuse complete_spot's actual account meter and monetary/archive gates."""
    old_policy, old_configured = complete.Policy, complete.configured
    policies = []
    def make_policy(c, venue, features):
        policy = Policy(c, venue, features, combo=combo, risk=risk, core_fraction=core_fraction)
        policies.append(policy)
        return policy
    try:
        complete.Policy, complete.configured = make_policy, configured
        result = complete.measure(candidate, scenario, bars, starts, fx, features or {}, limit=limit)
    finally:
        complete.Policy, complete.configured = old_policy, old_configured
    policy = policies[0]
    result.update(research_identity=policy.identity, opportunity_ledger=policy.journal,
                  subpools=policy.final_pools, risk_calibration=policy.risk)
    owners = execution.allocation_owners((json.loads(row[1]), json.loads(row[3])) for row in result['allocations'])
    final_fills = [dict(f, **{k: D(f[k]) for k in ('qty', 'quote', 'price', 'commission')}) for f in result['fills']]
    if policy.core:
        result['subpools'] = serial(subpools({'cash': policy.venue.initial_cash, 'btc': '0'}, final_fills, owners,
            policy.fraction, {'usdt_free': result['cash_usdt'], 'usdt_locked': '0', 'btc': result['btc']}))
    for fill in result['fills']:
        owner = owners.get(str(fill['order_id']))
        meta = policy.last_signals.get(signal_key(owner['order']['side'], owner['signal_ms'], owner['sleeves']), {}) if owner else {}
        result['opportunity_ledger'].append(dict(event='fill', **fill,
            sleeves=owner['sleeves'] if owner else None,
            signal_ms=owner['signal_ms'] if owner else None,
            exit_type='stop' if owner and owner['order']['type'] == 'STOP_LOSS' else meta.get('exit_types'),
            mechanism=meta.get('mechanism'), decision_ms=meta.get('decision_ms'),
            decision_price=meta.get('decision_price')))
    return serial(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--candidate', choices=(*CANDIDATES, 'combo'))
    parser.add_argument('--scenario', choices=SCENARIOS)
    parser.add_argument('--risk-calibration', type=Path)
    parser.add_argument('--combo', help='comma-separated registered components, fixed by selection')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1)
    args = parser.parse_args(argv)
    source = source_identity()
    if source['dirty'] or args.out.exists() or (args.limit is not None and (
            args.limit < 1 or not str(args.out.resolve()).startswith('/tmp/'))):
        parser.error('commit source; choose a new output; positive partial accounts only in /tmp')
    parts = tuple(args.combo.split(',')) if args.combo else ()
    candidate = 'combo' if parts else args.candidate
    if args.candidate and parts and args.candidate != 'combo':
        parser.error('--combo requires candidate combo or no --candidate')
    try:
        components_for(candidate or 'consensus', parts)
    except ValueError as exc:
        parser.error(str(exc))
    market_root = Path('/tmp/spotquant-market/klines')
    bars = load_daily(market_root, END_MS, require_through=END_MS)
    schedule_path = Path('../coinquant/research/session_schedule.json').resolve()
    starts = json.loads(schedule_path.read_text())['primary']['starts_ms']
    fx_path = Path('../starquant/data/usdcny_frankfurter.json').resolve()
    jobs = [(c, s) for c in ([candidate] if candidate else CANDIDATES)
            for s in ([args.scenario] if args.scenario else SCENARIOS)]
    risks = {c: calibration(args.risk_calibration, c) for c, _ in jobs}
    results = {}
    def record(c, s, result):
        results[c + '-' + s] = result
        progress = args.out.with_suffix('.progress.json')
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(json.dumps({'complete': False, 'source': source, 'results': results}, separators=(',', ':')) + '\n')
    if args.workers == 1:
        for c, s in jobs:
            record(c, s, measure(c, s, bars, starts, complete.PriorFX(fx_path), limit=args.limit, risk=risks[c], combo=parts))
    else:
        # ponytail: process-local scoped hooks; isolated processes only, never threads.
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(measure, c, s, bars, starts, complete.PriorFX(fx_path),
                                   limit=args.limit, risk=risks[c], combo=parts): (c, s) for c, s in jobs}
            for future in as_completed(pending):
                record(*pending[future], future.result())
    report = {'format': 1, 'source': source, 'spec_sha256': digest(SPEC.read_bytes()),
              'protocol_sha256': digest(SPEC.with_name('alpha-beta-PROTOCOL.md').read_bytes()),
              'market_sha256': file_digest(market_root),
              'schedule_sha256': digest(schedule_path.read_bytes()), 'fx_sha256': digest(fx_path.read_bytes()),
              'risk_calibration_sha256': digest(args.risk_calibration.read_bytes()) if args.risk_calibration else None,
              'results': {c + '-' + s: results[c + '-' + s] for c, s in jobs},
              'native_execution_verified': False,
              'limitations': ['daily OHLC linear high-before-low proxy; no observed intraday tape or queue',
                              'actual bounded session/Lifecycle, fixed published research cap USDT5m',
                              'first protection and cancel-replace are non-atomic',
                              'previously studied history; no prospective alpha proof']}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        json.dump(report, stream, separators=(',', ':'), allow_nan=False)
        stream.write('\n')
    print(json.dumps({'complete_accounts': sum(r['complete'] for r in results.values()), 'accounts': len(results)}))


if __name__ == '__main__':
    main()
