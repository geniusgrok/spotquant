"""Two independent, fixed exit expressions on the real offline Spot session.

Reasons are bound when a MARKET reduction is prepared, never inferred from a
later price. A native STOP_LOSS has its own durable order type. Models remember
only terminal, allocated fills; pending/unknown orders retain the core recovery.
"""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research import edge_spot as edge
from spotquant import execution, preview, session
from spotquant.model import DAY, Model
from spotquant.state import client_id
from spotquant.types import Blocked, Unknown, floor_step, serial

POLICIES = ('reason-stop-reentry', 'sma-half-hold')
SMA_REASON = 'completed daily close is not above its SMA'
TERMINAL_FILL = execution.TERMINAL - {'REJECTED'}


def cause_for(reason):
    if reason == SMA_REASON or reason.startswith(SMA_REASON + ';'):
        return 'sma-trend-exit'
    if reason.startswith('the resting stop is already through'):
        return 'safety-stop-through'
    if reason.startswith('completed daily close is at least'):
        return 'adverse-close'
    if reason.startswith('completed daily close is extended'):
        return 'extended-close'
    return 'unknown'


def model_for(expression):
    if expression not in POLICIES:
        raise ValueError('unregistered exit expression')

    class ReasonModel(Model):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.exit_memory = None
            self.half_memory = None

        def update(self, stamp, high, low, close):
            result = super().update(stamp, high, low, close)
            memory = self.exit_memory
            if memory is not None and not self.bull:
                memory['trend_intact'] = False
            if (expression == 'reason-stop-reentry' and memory is not None
                    and memory['cause'] == 'native-stop-loss' and memory['trend_intact']
                    and stamp >= memory['completed_day'] + 7 * DAY
                    and self.bull and self.streak >= 2 and self.crash_ok
                    and not self.extended and not self.cap_enter
                    and len(self.closes) >= 6
                    and self.close > max(list(self.closes)[-6:-1])):
                self.need_reset = False
                self.enter = True
            return result

        def checkpoint(self):
            saved = super().checkpoint()
            # An owned close changes need_reset between bars, as in the prior
            # participation consumer; serialize the base restore invariant.
            saved['body']['enter'] = bool(self.streak >= self.confirm and self.crash_ok
                                         and not (self.fresh and self.need_reset))
            saved['body']['reason_exit'] = dict(expression=expression,
                exit_memory=deepcopy(self.exit_memory), half_memory=deepcopy(self.half_memory))
            saved['sha256'] = hashlib.sha256(json.dumps(saved['body'], sort_keys=True).encode()).hexdigest()
            return saved

        @classmethod
        def restore(cls, saved):
            try:
                extra = saved['body']['reason_exit']
                if extra['expression'] != expression:
                    raise ValueError('expression')
                result = Model.restore.__func__(cls, saved)
                for name in ('exit_memory', 'half_memory'):
                    memory = extra[name]
                    if memory is not None:
                        if (type(memory) is not dict or type(memory['first_ms']) is not int
                                or memory['first_ms'] < 0 or type(memory['fill_ms']) is not int
                                or memory['fill_ms'] < memory['first_ms']
                                or type(memory['order_id']) is not int or memory['order_id'] <= 0):
                            raise ValueError('fill memory')
                        if name == 'exit_memory' and (
                                type(memory['completed_day']) is not int or memory['completed_day'] % DAY
                                or result.last is None or memory['completed_day'] > result.last
                                or type(memory['trend_intact']) is not bool
                                or memory['cause'] not in ('native-stop-loss', 'sma-trend-exit',
                                    'safety-stop-through', 'adverse-close', 'extended-close', 'unknown')):
                            raise ValueError('exit memory')
                        if name == 'half_memory' and (not D(memory['executed_group_quantity']).is_finite()
                                or D(memory['executed_group_quantity']) <= 0):
                            raise ValueError('half memory')
                    setattr(result, name, deepcopy(memory))
                return result
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                raise Blocked('unbound exit-reason checkpoint') from exc

    return ReasonModel


def _completed(owner, position, order_id):
    """Actual terminal execution and previously folded gross fills must agree."""
    executed = owner.get('native_executed_qty')
    applied = position.get('sell_applied', {}).get(str(order_id)) if position else None
    if owner.get('native_status') not in TERMINAL_FILL or executed is None or applied is None:
        return False
    executed, applied, intended = D(executed), D(applied), D(owner['order']['quantity'])
    return (executed.is_finite() and applied.is_finite()
            and 0 < executed <= intended and executed == applied)


def fold_owned(original, policy, models, positions, follows, accounted, open_ms,
               trades, history, owners=None):
    result = original(models, positions, follows, accounted, open_ms, trades, history, owners=owners)
    if owners is None:
        return result
    fresh = [trade for trade in trades if not trade['buyer'] and trade['id'] not in accounted
             and trade['id'] in result[2] and trade['time'] // DAY * DAY == open_ms]
    final_by_order = {trade['order_id']: trade for trade in sorted(fresh, key=lambda row: (row['time'], row['id']))}
    for trade in final_by_order.values():
        owner = owners.get(str(trade['order_id']))
        if owner is None or owner['order']['side'] != 'SELL':
            continue
        meta = owner.get('reason_exit', {})
        for window in owner['sleeves']:
            prior, current = positions.get(window), result[0].get(window)
            if prior is None or prior.get('dust') or trade['time'] < prior['first_ms']:
                continue
            model = models[window]
            allocation = meta.get('sleeves', {}).get(str(window), {})
            full = sum((floor_step(D(v), preview.BASE_STEP) for v in owner['weights'].values()), D(0))
            is_full = (window in result[3] and owner.get('native_status') == 'FILLED'
                       and owner.get('native_executed_qty') is not None
                       and D(owner['native_executed_qty']) == D(owner['order']['quantity']) == full)
            if is_full:
                # Partial-final fills may leave no position; the core's full
                # rounded allocation and terminal native readback establish close.
                cause = ('native-stop-loss' if owner['order']['type'] == 'STOP_LOSS'
                         else allocation.get('cause', 'unknown'))
                model.exit_memory = dict(cause=cause, completed_day=model.last,
                    trend_intact=bool(model.bull), first_ms=prior['first_ms'],
                    fill_ms=trade['time'], order_id=trade['order_id'],
                    original_preview_reason=allocation.get('reason'))
                model.half_memory = None
                policy.journal.append(serial(dict(event='confirmed-reason-close', sleeve=window,
                    **model.exit_memory)))
            elif (policy.expression == 'sma-half-hold' and allocation.get('half') is True
                    and allocation.get('first_ms') == prior['first_ms']
                    and current is not None and not current.get('dust')
                    and _completed(owner, current, trade['order_id'])):
                # A canceled/expired terminal partial also consumed this one
                # reduction. It keeps the actual remainder, never assumes half.
                model.half_memory = dict(first_ms=prior['first_ms'], fill_ms=trade['time'],
                    order_id=trade['order_id'], executed_group_quantity=owner['native_executed_qty'])
                policy.journal.append(serial(dict(event='confirmed-sma-reduction', sleeve=window,
                    remaining_btc=current['qty'], **model.half_memory)))
    return result


class Policy(edge.Policy):
    def __init__(self, expression, venue, features, binding):
        super().__init__('crowding-interaction', venue, features)
        self.expression = expression
        self.identity.update(reason_exit_expression=expression, reason_exit_binding=deepcopy(binding),
            reason_exit_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())

    def __call__(self, views, owned, snapshot, **kwargs):
        positions = kwargs['positions']
        awaiting_protection = []
        if self.expression == 'sma-half-hold':
            for w, view in views.items():
                position = positions.get(w)
                memory = getattr(view, 'half_memory', None)
                if (position is None or position.get('dust') or memory is None
                        or memory['first_ms'] != position['first_ms']):
                    continue
                covered = D(0)
                for owner in kwargs['owners'].values():
                    order = owner['order']
                    if (w not in owner['sleeves'] or order['type'] != 'STOP_LOSS'
                            or order['side'] != 'SELL' or owner.get('native_status') not in ('NEW','PARTIALLY_FILLED')
                            or owner.get('native_executed_qty') is None
                            or owner['signal_ms'] < position['first_ms'] // DAY * DAY - DAY):
                        continue
                    weights = {int(k):D(v) for k,v in owner['weights'].items()}
                    remaining = D(order['quantity']) - D(owner['native_executed_qty'])
                    covered += remaining * weights[w] / sum(weights.values(), D(0))
                if covered < floor_step(D(owned[w]), preview.BASE_STEP):
                    awaiting_protection.append(w)
        original = preview._position_decision
        reductions = {}
        def decide(view, snap, qty, price):
            result = original(view, snap, qty, price)
            if self.expression != 'sma-half-hold' or result['action'] != 'exit' or cause_for(result['reason']) != 'sma-trend-exit':
                return result
            position = positions.get(view.sma_window)
            if position is None or position.get('dust'):
                return result
            memory = getattr(view, 'half_memory', None)
            if memory is not None and memory['first_ms'] == position['first_ms']:
                return dict(action='hold', reason='confirmed SMA reduction retains its protected owned remainder',
                    order=None, protection=preview._protection(view, qty, snap))
            if awaiting_protection:
                # The core prioritizes sells before stop replacement. Defer
                # another ordinary half so a previous actual remainder first
                # regains confirmed coverage; hard/safety exits remain original.
                return dict(action='hold', reason='ordinary SMA reduction waits for confirmed remainder protection',
                    order=None, protection=preview._protection(view, qty, snap))
            half = floor_step(qty / 2, preview.BASE_STEP)
            remaining = qty - half
            # Do not create a mechanically unprotected dust tranche. If either
            # rounded half cannot rest a genuine stop, retain the original exit.
            stop = preview._protection(view, remaining, snap)
            if (half < preview.BASE_STEP or half * D(snap['avg_price']) < preview.MIN_NOTIONAL
                    or remaining * D(snap['avg_price']) < preview.MIN_NOTIONAL
                    or stop.get('placeable') is False or D(stop['stopPrice']) >= D(snap['avg_price'])):
                return result
            reductions[view.sma_window] = half
            return dict(result, order=dict(result['order'], quantity=str(half)) if result['order'] else None)
        with patch.object(preview, '_position_decision', decide):
            decision = super().__call__(views, owned, snapshot, **kwargs)
        orders = []
        for order in decision['orders']:
            if order['side'] != 'SELL':
                orders.append(order)
                continue
            quantities, metadata = {}, {}
            for w in order['sleeves']:
                sleeve = decision['sleeves'][str(w)]
                reason = sleeve['reason']
                # ATR's current-market stop-through may override an ordinary
                # half proposal. Such a forced exit must remain a full sale.
                forced = (sleeve['action'] == 'exit' and sleeve.get('order') is None
                          and cause_for(reason) == 'unknown')
                half = w in reductions and not forced
                amount = reductions[w] if half else floor_step(D(owned[w]), preview.BASE_STEP)
                quantities[w] = amount
                if half:
                    sleeve['order'] = dict(sleeve['order'], quantity=str(amount))
                metadata[str(w)] = dict(reason=reason,
                    cause='safety-stop-through' if forced else cause_for(reason), half=half,
                    first_ms=positions[w]['first_ms'], owned_before=str(owned[w]), requested_quantity=str(amount))
            # Core fill attribution uses original owned weights. A mixed full /
            # half order would allocate the total sale to the wrong sleeves;
            # even all-half floors need not be proportional to those weights.
            # Keep full exits on the original grouped close and issue each half
            # as one sleeve's stable intent. Core act still sends one then folds.
            full_group = [w for w in order['sleeves'] if not metadata[str(w)]['half']]
            groups = ([full_group] if full_group else []) + [[w] for w in order['sleeves'] if metadata[str(w)]['half']]
            for group in groups:
                quantity = sum((quantities[w] for w in group), D(0))
                orders.append(dict(order, sleeves=group, quantity=str(quantity),
                    reason_exit=dict(expression=self.expression, sleeves={str(w):metadata[str(w)] for w in group})))
        decision['orders'] = orders
        decision['order'] = decision['orders'][0] if decision['orders'] else None
        self.journal[-1]['accepted_orders'] = serial(decision['orders'])
        self.journal.append(serial(dict(event='reason-exit-intents', decision_ms=self.venue.now_ms,
            completed_bar_ms=views[30].last, actual_sell_intents=[o for o in decision['orders'] if o['side'] == 'SELL'])))
        return decision


def _prepare(original, lifecycle, order, bar, positions, follows):
    """Original stable identity and allocation, with one atomic reason binding.

    BUY and STOP_LOSS use the unchanged prepare. Only MARKET SELL carries the
    additional research metadata; send, recover, cancel and ownership stay core.
    """
    if order['side'] != 'SELL' or order['type'] != 'MARKET':
        return original(lifecycle, order, bar, positions, follows)
    group = sorted(order['sleeves'])
    metadata = order.get('reason_exit')
    if (not group or any(w not in session.SLEEVES for w in group)
            or type(metadata) is not dict or set(metadata.get('sleeves', {})) != {str(w) for w in group}):
        raise Blocked('MARKET exit lacks its original preview reason binding')
    raw = {key: order[key] for key in execution.FIELDS if key in order}
    if raw.get('symbol') != 'BTCUSDT':
        raise Blocked('invalid P4 order')
    payload = serial(dict(order=raw, sleeves=group,
        weights={str(w): positions[str(w)]['qty'] for w in group}, signal_ms=bar,
        repair={str(w): bool((follows.get(str(w)) or {}).get('repair')) for w in group},
        reason_exit=metadata))
    operation = raw['side'] + '-' + raw['type'] + '-' + ','.join(map(str, group))
    identity = client_id(lifecycle.state.identity, bar, operation)
    prior = next((row for row in lifecycle.rows() if row[0] == identity), None)
    if prior is None:
        lifecycle.save(identity, payload, 'prepared', {})
    elif prior[1] != payload and prior[2] == 'prepared':
        raise Blocked('prepared identity cannot change parameters or exit reason')
    return identity


def _guard_intents(state, expression):
    for encoded, in state.db.execute("SELECT payload FROM intents WHERE kind='p4'"):
        payload = json.loads(encoded)
        order = payload['order']
        if order['side'] != 'SELL' or order['type'] != 'MARKET':
            continue
        try:
            metadata = payload['reason_exit']
            group = payload['sleeves']
            if metadata['expression'] != expression or set(metadata['sleeves']) != {str(w) for w in group}:
                raise ValueError('binding')
            total = D(0)
            for w in group:
                item = metadata['sleeves'][str(w)]
                amount, before = D(item['requested_quantity']), D(item['owned_before'])
                if (type(item['half']) is not bool or type(item['reason']) is not str
                        or type(item['first_ms']) is not int or item['first_ms'] < 0
                        or item['cause'] not in ('sma-trend-exit', 'safety-stop-through',
                            'adverse-close', 'extended-close', 'unknown')
                        or not amount.is_finite() or not before.is_finite()
                        or not 0 < amount <= before or D(payload['weights'][str(w)]) != before
                        or (not item['half'] and amount != floor_step(before, preview.BASE_STEP))
                        or (item['half'] and (metadata['expression'] != 'sma-half-hold'
                            or item['cause'] != 'sma-trend-exit'
                            or amount != floor_step(floor_step(before, preview.BASE_STEP) / 2, preview.BASE_STEP)))):
                    raise ValueError('allocation')
                total += amount
            if total != D(order['quantity']):
                raise ValueError('actual intended quantity')
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise Blocked('unbound or malformed exit-reason intent before recovery') from exc


@contextmanager
def configured(expression, *, venue, features, binding, risk=None, journal=None):
    if expression not in POLICIES or type(binding) is not dict or not binding:
        raise ValueError('registered exit expression and binding required')
    if getattr(venue, 'offline', False) is not True:
        raise ValueError('exit expression refuses account adapters before recovery')
    if risk not in (None, {'scale': '1', 'sha256': None}):
        raise ValueError('single-component exit expressions require original initial budget')
    policy = Policy(expression, venue, features, binding)
    if journal is not None:
        policy.journal = journal
    original_fold, original_prepare, original_guard = session.apply_day, execution.Lifecycle.prepare, session._guard_state
    def guard(state):
        result = original_guard(state)
        _guard_intents(state, expression)
        return result
    def fold(*args, **kwargs):
        return fold_owned(original_fold, policy, *args, **kwargs)
    def prepare(lifecycle, *args, **kwargs):
        return _prepare(original_prepare, lifecycle, *args, **kwargs)
    with patch.object(edge, 'Model', model_for(expression)), patch.object(session, '_guard_state', guard), \
            patch.object(session, 'apply_day', fold), patch.object(execution.Lifecycle, 'prepare', prepare), edge.configured(policy):
        def run(config, selected, **kwargs):
            if selected is not venue or getattr(selected, 'offline', False) is not True:
                raise ValueError('exit expression refuses account adapters before recovery')
            return session.run(config, selected, **kwargs)
        yield SimpleNamespace(run=run, policy=policy)
