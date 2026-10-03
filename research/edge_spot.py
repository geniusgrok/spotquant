"""Frozen edge mechanisms and protected controls through the actual Spot Lifecycle."""
import argparse
import copy
from contextlib import contextmanager
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path

from research import complete_spot as complete
from research.alpha_spot import cached_fills, signal_key
from research.edge_features import FeatureBook
from research.market import file_digest, load_daily
from research.rebuild import END_MS, source_identity
from spotquant import execution, follow, model, preview, session
from spotquant.model import DAY, Model
from spotquant.state import State
from spotquant.types import Blocked, floor_step, serial

SPEC = Path(__file__).with_name('edge_spec.json')
PROTOCOL = SPEC.with_name('edge-PROTOCOL.md')
FEATURE_SHA256 = 'bf920626cc13653b8bbeafd171650cc1a76888e15aad0c20bbd1061fb41da512'
CUTOFF = 1640995200000
MECHANISMS = ('exit-confirm', 'stop-budget', 'crowding-interaction')
CONTROLS = {'cash': D(0), **{f'protected-participation-{n}': D(n) / 100 for n in (25, 50, 75, 100)}}
CANDIDATES = ('atr-stop', *MECHANISMS, *CONTROLS, 'combo')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def components_for(candidate, combo=()):
    if candidate not in CANDIDATES or (candidate != 'combo' and combo):
        raise ValueError('unregistered candidate or components')
    parts = tuple(combo) if candidate == 'combo' else ((candidate,) if candidate in MECHANISMS else ())
    if (any(p not in MECHANISMS for p in parts) or len(set(parts)) != len(parts)
            or (candidate == 'combo' and len(parts) < 2)):
        raise ValueError('unregistered or duplicate combination components')
    return tuple(p for p in MECHANISMS if p in parts)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def calibration(path, candidate):
    if path is None:
        return {'scale': '1', 'sha256': None}
    raw = Path(path).read_bytes()
    try:
        data = json.loads(raw, object_pairs_hook=unique_object)
        if (set(data) != {'format', 'project_kind', 'baseline_candidate', 'cutoff_ms', 'spec_sha256', 'profiles'}
                or type(data['format']) is not int or data['format'] != 1 or data['project_kind'] != 'spot'
                or data['baseline_candidate'] != 'atr-stop' or type(data['cutoff_ms']) is not int
                or data['cutoff_ms'] != CUTOFF or data['spec_sha256'] != digest(SPEC.read_bytes())
                or type(data['profiles']) is not dict or candidate not in data['profiles']):
            raise ValueError('edge risk document identity')
        keys = {'candidate', 'project_kind', 'scale', 'base_bundle_sha256', 'baseline_candidate',
                'effective_from_ms', 'calibration_end_ms', 'training_end_day_exclusive'}
        for name, profile in data['profiles'].items():
            if name not in ('atr-stop', *MECHANISMS, 'combo') or set(profile) != keys:
                raise ValueError('unregistered profile or fields')
            scale, source = D(profile['scale']), profile['base_bundle_sha256']
            if (profile['candidate'] != name or profile['project_kind'] != 'spot'
                    or profile['baseline_candidate'] != 'atr-stop'
                    or type(profile['effective_from_ms']) is not int or profile['effective_from_ms'] != CUTOFF
                    or type(profile['calibration_end_ms']) is not int or profile['calibration_end_ms'] != CUTOFF
                    or profile['training_end_day_exclusive'] != '2022-01-01'
                    or type(profile['scale']) is not str or not scale.is_finite() or not 0 <= scale <= 1
                    or type(source) is not str or len(source) != 64 or any(c not in '0123456789abcdef' for c in source)
                    or (name == 'atr-stop' and scale != 1)):
                raise ValueError('edge risk profile identity or training boundary')
    except (KeyError, TypeError, ArithmeticError, json.JSONDecodeError) as exc:
        raise ValueError('invalid edge risk profile') from exc
    return dict(data['profiles'][candidate], sha256=digest(raw))


def previous_sma(view):
    closes = list(view.closes)
    if len(closes) < view.sma_window + 1:
        return None
    return sum(closes[-view.sma_window - 1:-1], D(0)) / view.sma_window


def resize(decision, order, quote):
    quote = floor_step(max(D(0), quote), preview.QUOTE_STEP)
    if quote < preview.MIN_NOTIONAL:
        decision['orders'].remove(order)
        for w in order['sleeves']:
            decision['sleeves'][str(w)].update(action='flat', order=None, protection=None)
    else:
        order['quoteOrderQty'] = str(quote)
        for w in order['sleeves']:
            decision['sleeves'][str(w)]['order']['quoteOrderQty'] = str(quote / len(order['sleeves']))
    return quote


def existing_risk(owned, snapshot, positions, owners):
    """Risk of every owned coin; only active allocated native quantities count."""
    mark = D(snapshot['avg_price'])
    quantities = {w: D(q) for w, q in owned.items()}
    if (not mark.is_finite() or mark <= 0 or any(not q.is_finite() or q < 0 for q in quantities.values())
            or abs(sum(quantities.values(), D(0)) - D(snapshot['btc'])) > D('1e-8')):
        raise ValueError('missing account ownership')
    for w, qty in quantities.items():
        if qty > 0 and (positions.get(w) is None or D(positions[w]['qty']) != qty):
            raise ValueError('owned coins lack current position attribution')
    coverage = {w: [] for w in quantities}
    for owner in owners.values():
        order = owner['order']
        if order['type'] != 'STOP_LOSS' or owner.get('native_status') not in ('NEW', 'PARTIALLY_FILLED'):
            continue
        group, weights = owner['sleeves'], owner['weights']
        qty = D(order['quantity']) - D(owner['native_executed_qty'])
        stop = D(order['stopPrice'])
        values = [D(weights[str(w)]) for w in group]
        if (order['side'] != 'SELL' or not group or group != sorted(set(group))
                or any(w not in quantities for w in group) or set(weights) != {str(w) for w in group}
                or any(not v.is_finite() or v <= 0 for v in values)
                or not qty.is_finite() or qty < 0 or not stop.is_finite() or stop <= 0):
            raise ValueError('malformed allocated protection')
        total = sum(values, D(0))
        for w, weight in zip(group, values):
            position = positions.get(w)
            if (position is None or D(position['qty']) != quantities[w]
                    or type(owner['signal_ms']) is not int
                    or owner['signal_ms'] < int(position['first_ms']) // DAY * DAY - DAY):
                raise ValueError('protection lacks current sleeve ownership')
            coverage[w].append((qty * weight / total, stop))
    risk = D(0)
    for w, qty in quantities.items():
        if not qty:
            continue
        covered = sum((q for q, _ in coverage[w]), D(0))
        # Unprotected rounding dust is risk too, conservatively charged at full mark.
        if covered + preview.BASE_STEP < qty or covered > qty + preview.BASE_STEP:
            raise ValueError('missing or excessive allocated protection')
        risk += sum((q * max(D(0), mark - stop) for q, stop in coverage[w]), D(0))
        risk += max(D(0), qty - covered) * mark
    return risk


class Policy:
    def __init__(self, candidate, venue, features=None, *, combo=(), risk=None):
        self.parts = components_for(candidate, combo)
        self.candidate, self.venue, self.features = candidate, venue, features
        self.risk = risk or {'scale': '1', 'sha256': None}
        scale = D(self.risk['scale'])
        if not scale.is_finite() or not 0 <= scale <= 1:
            raise ValueError('invalid edge scale')
        self.identity = dict(candidate=candidate, components=list(self.parts), project_kind='spot',
                             spec_sha256=digest(SPEC.read_bytes()), risk_calibration=self.risk,
                             execution_source_sha256=source_identity()['python_sources_sha256'],
                             features_sha256=features.sha256 if 'crowding-interaction' in self.parts and features else None)
        self.state = None
        self.filters = {'blocked': 0, 'missing': 0}
        self.journal, self.signals = [], {}

    def __call__(self, views, owned, snapshot, **kwargs):
        last = next(iter(views.values())).last
        diagnostics = []
        positions, owners = kwargs['positions'], kwargs['owners']
        working = {w: copy.copy(v) for w, v in views.items()}
        control = self.candidate in CONTROLS
        if control:
            fills = cached_fills(self.state)
            for w, v in working.items():
                stops = [f['time'] for f in fills if not f['buyer'] and str(f['order_id']) in owners
                         and owners[str(f['order_id'])]['order']['type'] == 'STOP_LOSS'
                         and w in owners[str(f['order_id'])]['sleeves']]
                allowed = not stops or last >= max(stops) // DAY * DAY + DAY
                v.bull = True
                v.enter = allowed and self.candidate != 'cash'
                v.cap_enter = v.repair = v.adverse = v.extended = False
        original = preview._position_decision
        def position_decision(v, snap, quantity, price):
            result = original(v, snap, quantity, price)
            prior = previous_sma(v)
            if ('exit-confirm' in self.parts and result['action'] == 'exit' and not v.bull
                    and not v.repair and not v.adverse and not v.extended
                    and getattr(v, 'protection', 'resting') not in ('breached', 'through_close')
                    and not (v.position_peak is not None and v.stop_price(v.position_peak) >= price)
                    and prior is not None and v.prev_close > prior):
                diagnostics.append(dict(mechanism='exit-confirm', sleeve=v.sma_window,
                                        previous_close=v.prev_close, previous_sma=prior, current_close=v.close, current_sma=v.sma))
                return dict(action='hold', reason='ordinary exit awaits a second below-SMA completed close',
                            order=None, protection=preview._protection(v, quantity, snap))
            return result
        try:
            preview._position_decision = position_decision
            if control:
                old_view = preview.decision_view
                preview.decision_view = self.static_view_bound
            decision = preview.decision(working, owned, snapshot, **dict(kwargs, allocation_scale=D(1)))
        finally:
            preview._position_decision = original
            if control:
                preview.decision_view = old_view
        desired = copy.deepcopy(decision['orders'])
        scale = D(self.risk['scale']) if self.venue.now_ms >= CUTOFF else D(1)
        for order in list(decision['orders']):
            if order['side'] != 'BUY':
                continue
            quote = D(order['quoteOrderQty'])
            cause = None
            # Only the original follow's proven closed dust may start a new campaign.
            # A small active partial fill is still held, even below the venue step.
            def held(w):
                position = positions.get(w) or {}
                closed_dust = (position.get('dust') is True and bool(position.get('sell_applied'))
                               and getattr(views[w], '_owned_dust', False))
                return D(owned[w]) >= preview.BASE_STEP or (D(owned[w]) > 0 and not closed_dust)
            if any(held(w) for w in order['sleeves']):
                quote, cause = D(0), 'held_sleeve_no_topup'
            if control and cause is None:
                cap = kwargs['capital_limit']
                quote = min(D(snapshot['usdt_free']) * CONTROLS[self.candidate],
                            max(D(0), cap - D(snapshot['btc']) * D(snapshot['avg_price'])) if cap is not None else D(snapshot['usdt_free']))
            if 'stop-budget' in self.parts and cause is None:
                budget = D('.12') * (D(snapshot['usdt_free']) + D(snapshot.get('usdt_locked', 0)) + D(snapshot['btc']) * D(snapshot['avg_price']))
                used, loss, entry = None, None, None
                try:
                    used = existing_risk(owned, snapshot, positions, owners)
                    entry = D(snapshot['avg_price']) * (1 + self.venue.slip)
                    stops = [D(decision['sleeves'][str(w)]['protection']['stopPrice']) for w in order['sleeves']]
                    trails = [preview.decision_view(views[w], positions.get(w), owners).trail for w in order['sleeves']]
                    loss = max(max(trails), max(D(0), 1 - min(stops) / entry))
                    loss += self.venue.fee * 2 + self.venue.slip + self.venue.stop_slip + preview.PRICE_STEP / entry
                    quote = min(quote, max(D(0), budget - used) / loss)
                except (KeyError, TypeError, ArithmeticError, ValueError):
                    quote, cause = D(0), 'unproven_ownership_or_protection'
                diagnostics.append(dict(mechanism='stop-budget', budget=budget, existing_risk=used,
                                        proposed_loss_per_quote=loss, entry_estimate=entry,
                                        fee_reserve_fraction=2 * self.venue.fee,
                                        slip_reserve_fraction=self.venue.slip + self.venue.stop_slip,
                                        quote=quote, blocked_reason=cause, gap_loss_capped=False))
            if 'crowding-interaction' in self.parts and cause is None:
                inputs = []
                values = []
                for name in ('funding', 'basis'):
                    value = self.features.value(name, self.venue.now_ms) if self.features else None
                    values.append(value)
                    inputs.append(copy.deepcopy(self.features.last_lookup) if self.features else dict(name=name, cause='missing_feature_book', value=None, now_ms=self.venue.now_ms))
                closes = list(views[30].closes)
                momentum = dict(current_completed_bar_ms=last, current_close=views[30].close,
                                prior_completed_bar_ms=last - 5 * DAY if len(closes) >= 6 else None,
                                prior_close=closes[-6] if len(closes) >= 6 else None,
                                causal_completed=last + DAY <= self.venue.now_ms)
                if any(v is None for v in values) or len(closes) < 6 or last + DAY > self.venue.now_ms:
                    quote, cause = D(0), 'missing_causal_crowding_or_momentum'
                    self.filters['missing'] += 1
                elif values[0] > D('.0003') and values[1] > D('.01') and closes[-1] <= closes[-6]:
                    quote /= 2
                diagnostics.append(dict(mechanism='crowding-interaction', inputs=inputs, momentum=momentum, quote=quote, blocked_reason=cause))
            quote *= scale
            final = resize(decision, order, quote)
            if final < preview.MIN_NOTIONAL:
                self.filters['blocked'] += 1
                cause = cause or 'below_minimum_after_rounding_or_risk_scale'
            for diagnostic in diagnostics:
                if diagnostic['mechanism'] in ('stop-budget', 'crowding-interaction'):
                    diagnostic.update(resulting_quote=final, blocked_reason=cause)
            diagnostics.append(dict(mechanism='new-buy', sleeves=order['sleeves'], quote=final, blocked_reason=cause, risk_scale=scale))
        protection_views = {w: self.static_view_bound(v, positions.get(w), owners) if control else preview.decision_view(v, positions.get(w), owners) for w, v in working.items()}
        decision['protections'] = preview._merge_protections({w: decision['sleeves'][str(w)] for w in working}, protection_views, snapshot, next(iter(protection_views.values())))
        decision['order'] = decision['orders'][0] if decision['orders'] else None
        for order in decision['orders']:
            key = signal_key(order['side'], last, order['sleeves'])
            self.signals.setdefault(key, dict(decision_ms=self.venue.now_ms, decision_price=snapshot['avg_price'], mechanism=self.candidate))
        self.journal.append(serial(dict(event='decision', decision_ms=self.venue.now_ms, completed_bar_ms=last,
                                       opportunity_id=f'{self.candidate}:{last}', trigger_close=views[30].close,
                                       opportunity_age_ms=self.venue.now_ms - last - DAY,
                                       entries_enabled=kwargs['entries_enabled'],
                                       sleeves={str(w): dict(action=decision['sleeves'][str(w)]['action'], owned_btc=owned[w]) for w in views},
                                       bullish_votes={str(w): v.bull for w, v in views.items()},
                                       desired_orders=desired, accepted_orders=decision['orders'], diagnostics=diagnostics)))
        return decision


@contextmanager
def configured(policy):
    originals = [(module, name, getattr(module, name)) for module, names in (
        (model, ('Model',)), (follow, ('Model',)),
        (session, ('Model', 'State', 'RULE', 'RECORDED_LIMITS', 'portfolio', '_guard_state')),
        (execution, ('decision_view',))) for name in names]
    base_guard = session._guard_state
    base_view = preview.decision_view
    def static_view(v, p, o):
        result = base_view(v, p, o)
        result.trail = D('.28')
        result.protection = getattr(v, 'protection', 'resting')
        return result
    policy.static_view_bound = static_view
    class EdgeModel(Model):
        def checkpoint(self):
            body = dict(model=super().checkpoint(), identity=policy.identity)
            return dict(body, sha256=digest(json.dumps(body, sort_keys=True).encode()))
        @classmethod
        def restore(cls, saved):
            try:
                body = dict(model=saved['model'], identity=saved['identity'])
                if saved['identity'] != policy.identity or saved['sha256'] != digest(json.dumps(body, sort_keys=True).encode()):
                    raise ValueError('identity')
                return Model.restore.__func__(cls, saved['model'])
            except (KeyError, TypeError, ValueError) as exc:
                raise Blocked('edge checkpoint mechanism/profile mismatch') from exc
    def guard(state):
        prior = state.get('edge_identity')
        if (prior is not None and prior != policy.identity) or (state.get('models') is not None and prior is None):
            raise Blocked('unbound or mismatched edge account')
        return base_guard(state)
    class EdgeState(State):
        def __enter__(self):
            result = super().__enter__()
            try:
                guard(result)
            except BaseException:
                super().__exit__(None, None, None)
                raise
            policy.state = result
            return result
        def set_many(self, values):
            if 'models' in values:
                values = dict(values, edge_identity=policy.identity)
            return super().set_many(values)
    try:
        model.Model = session.Model = follow.Model = EdgeModel
        session.State, session.portfolio, session._guard_state = EdgeState, policy, guard
        session.RULE = 'edge-spot:' + digest(json.dumps(policy.identity, sort_keys=True).encode())
        session.RECORDED_LIMITS = dict(session.RECORDED_LIMITS, research_identity=policy.identity)
        if policy.candidate in CONTROLS:
            execution.decision_view = static_view
        yield
    finally:
        for module, name, value in originals:
            setattr(module, name, value)


def measure(candidate, scenario, bars, starts, fx, features=None, *, limit=None, initial_cny=D(10000), risk=None, combo=()):
    parts = components_for(candidate, combo)
    if scenario not in complete.SCENARIOS or (limit is not None and (type(limit) is not int or not 1 <= limit <= 795)):
        raise ValueError('unregistered scenario or partial limit')
    risk = risk or {'scale': '1', 'sha256': None}
    if candidate == 'atr-stop':
        if D(risk['scale']) != 1:
            raise ValueError('edge baseline must remain unity')
        # New edge profiles never enter the old alpha calibration validator.
        return complete.measure('atr-stop', scenario, bars, starts, fx, {}, limit=limit,
                                initial_cny=initial_cny, canonical=True)
    policies = []
    old_policy, old_configured = complete.Policy, complete.configured
    def make_policy(c, venue, unused):
        result = Policy(c, venue, features, combo=parts if c == 'combo' else (), risk=risk)
        policies.append(result)
        return result
    try:
        complete.Policy, complete.configured = make_policy, configured
        result = complete.measure(candidate, scenario, bars, starts, fx, {}, limit=limit, initial_cny=initial_cny)
    finally:
        complete.Policy, complete.configured = old_policy, old_configured
    policy = policies[0]
    owners = execution.allocation_owners((json.loads(r[1]), json.loads(r[3])) for r in result['allocations'])
    ledger = policy.journal
    for fill in result['fills']:
        owner = owners.get(str(fill['order_id']))
        meta = policy.signals.get(signal_key(owner['order']['side'], owner['signal_ms'], owner['sleeves']), {}) if owner else {}
        ledger.append(dict(event='fill', **fill, sleeves=owner['sleeves'] if owner else None,
                           signal_ms=owner['signal_ms'] if owner else None,
                           exit_type='stop' if owner and owner['order']['type'] == 'STOP_LOSS' else None, **meta))
    result.update(research_identity=policy.identity, opportunity_ledger=ledger, risk_calibration=risk)
    return serial(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', choices=CANDIDATES, required=True)
    parser.add_argument('--scenario', choices=complete.SCENARIOS, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--risk-calibration', type=Path)
    parser.add_argument('--initial-cny', type=int, choices=(2500, 5000, 7500, 10000), default=10000)
    parser.add_argument('--features', type=Path)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--combo', help='comma-separated registered components')
    args = parser.parse_args(argv)
    source = source_identity()
    if source['dirty'] or args.out.exists() or (args.limit is not None and (
            not 1 <= args.limit <= 795 or not args.out.resolve().is_relative_to('/tmp'))):
        parser.error('clean committed source, exclusive output, and partial limits 1..795 only in /tmp required')
    parts = tuple(args.combo.split(',')) if args.combo else ()
    components_for(args.candidate, parts)
    risk = calibration(args.risk_calibration, args.candidate)
    if 'crowding-interaction' in components_for(args.candidate, parts) and args.features is None:
        parser.error('crowding-interaction requires the pinned FeatureBook')
    features = FeatureBook(args.features, expected_sha256=FEATURE_SHA256) if args.features else None
    market_root = Path('/tmp/spotquant-market/klines')
    schedule_path = Path('../coinquant/research/session_schedule.json').resolve()
    fx_path = Path('../starquant/data/usdcny_frankfurter.json').resolve()
    bindings = dict(spec_sha256=digest(SPEC.read_bytes()), protocol_sha256=digest(PROTOCOL.read_bytes()),
                    feature_sha256=features.sha256 if features else None,
                    feature_consumed='crowding-interaction' in parts or args.candidate == 'crowding-interaction',
                    risk_calibration_sha256=risk['sha256'], risk_calibration=risk,
                    market_sha256=file_digest(market_root), schedule_sha256=digest(schedule_path.read_bytes()),
                    fx_sha256=digest(fx_path.read_bytes()))
    starts = json.loads(schedule_path.read_text())['primary']['starts_ms']
    row = measure(args.candidate, args.scenario, load_daily(market_root, END_MS, require_through=END_MS), starts,
                  complete.PriorFX(fx_path), features, limit=args.limit, initial_cny=D(args.initial_cny), risk=risk, combo=parts)
    if (source_identity() != source or calibration(args.risk_calibration, args.candidate) != risk
            or digest(SPEC.read_bytes()) != bindings['spec_sha256'] or digest(PROTOCOL.read_bytes()) != bindings['protocol_sha256']
            or (args.features and digest(args.features.read_bytes()) != bindings['feature_sha256'])):
        raise ValueError('source or registered inputs changed during replay')
    report = dict(format='btc-alpha-beta-edge-spot-v1', source=source, **bindings,
                  edge=dict(candidate=args.candidate, scenario=args.scenario, components=list(components_for(args.candidate, parts)),
                            complete_known_original_audits=bool(row['complete']), partial_limit=args.limit,
                            original_candidate=row['candidate'], execution='actual_finite_session_lifecycle'),
                  results={args.candidate + '-' + args.scenario: row}, native_execution_verified=False,
                  limitations=['Partial accounts are incomplete; no CAGR evidence.' if args.limit is not None else 'Original completeness/audits required.',
                               'Daily OHLC proxy; gap loss is not capped; native qualification NOT_QUALIFIED; actual account-days zero.',
                               'Protected controls use static28% stops and unconditional legal entry, no ATR/adverse/SMA/repair exits; no held topups/rebalance; not pure buyhold.',
                               'Funding/basis contain genuine unavailable intervals; missing consumed features block new risk only.'])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(serial(report), separators=(',', ':'), allow_nan=False).encode() + b'\n'
    with args.out.open('xb') as stream:
        stream.write(gzip.compress(encoded, mtime=0) if args.out.suffix == '.gz' else encoded)
    print(json.dumps(dict(output=str(args.out), complete=row['complete'], sha256=digest(args.out.read_bytes()))))


if __name__ == '__main__':
    main()
