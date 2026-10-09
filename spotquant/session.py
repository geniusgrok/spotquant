"""One bounded session: read-only by default, explicit owner-authorized execution."""
from __future__ import annotations

import time
import json
import os
import hashlib
from copy import copy
from decimal import Decimal as D

from .follow import advance, apply_day, day_open, unexplained
from .model import DAY, ORIGIN, SLEEVES, Model
from .preview import MIN_NOTIONAL, BASE_STEP, decision
from .state import State, client_id
from .types import Blocked, Unknown, floor_step, number

from .crowding import RULE

# Matching single-SMA40 v7 policies retain their owned account and order state.
KNOWN_OLD_RULE = '2026-10-03-atr-stop-crowding-interaction-v1'
COMPATIBLE_RULES = (KNOWN_OLD_RULE, '2026-10-09-sma40-touch-entry-guard-v1')
SAFETY_PREDECESSOR = '2026-10-09-verified-realtime-peak-v1'

# Dropped when a cycle fails so the previous success cannot be read as current.
STALE_REPORT_FIELDS = (
    'actual', 'model_preview', 'market_through', 'model_bull', 'entries_enabled',
    'followed_position', 'followed_sleeves',
)

RECORDED_LIMITS = {
    'adverse_exit': 'next_session',
    'adverse_loss_capped': False,
    'execution': 'Demo and live writes require explicit UID authorization and a positive capital ceiling',
    'public_features': 'funding lag/expiry8h; paired UTC closes lag60s; missing blocks new BUY',
}


def clear_stale(report: dict) -> None:
    for key in STALE_REPORT_FIELDS:
        report.pop(key, None)


def cycle(venue, state: State, config, *, execute=False) -> dict:
    state._recovery_only = False
    _guard_state(state)
    _guard_venue(venue, config)
    lifecycle = None
    if execute:
        from .execution import Lifecycle
        lifecycle = Lifecycle(state, venue, config)
    else:
        from .execution import allocation_owners
        allocated = list(state.db.execute("SELECT payload,result FROM intents WHERE kind='p4'"))
        state._execution_owners = (allocation_owners((json.loads(payload), json.loads(result))
                                                    for payload, result in allocated) if allocated else None)
    for _ in range(12 if execute else 1):
        if lifecycle:
            lifecycle.recover()
            state._execution_owners = lifecycle.owners()
        current = _cycle(venue, state, config, lifecycle=lifecycle)
        if not lifecycle or not lifecycle.act(current['model_preview'], current['market_through'], current['actual']):
            return dict(current, write_attempted=_writes(venue),
                        status=f'{config.environment}_execution' if execute else 'read_only')
    raise Unknown('bounded execution cycle exhausted; reconcile on the next cycle')


def _writes(venue):
    return bool(getattr(venue, 'write_attempted', False)
                or len(getattr(venue, 'sent', ())) > getattr(venue, '_session_sent_start', 0))


def _guard_venue(venue, config):
    if getattr(venue, 'environment', None) != config.environment:
        raise Blocked('exchange adapter and configuration differ in environment')
    if getattr(venue, 'capital_limit', None) != config.capital_limit:
        raise Blocked('exchange adapter and configuration differ in capital limit')


def _now_ms(venue):
    return venue._timestamp() if hasattr(venue, '_timestamp') else int(venue.clock() * 1000)


def _cycle(venue, state: State, config, *, lifecycle=None, crowding_source=None, features_loaded=False) -> dict:
    """Commit a verified spot observation and refresh it after optional entry inputs.

    A failed observation never commits its partial model or positions. The
    verified observation before a public entry read remains available for recovery.
    """
    prior_rule = state.get('rule')
    compatible = prior_rule in COMPATIBLE_RULES or prior_rule == SAFETY_PREDECESSOR
    state._recovery_only = compatible
    models, enabled, fresh = _load_models(state, venue)
    snapshot = venue.snapshot(config.account_uid)
    actual_snapshot = snapshot
    if lifecycle:
        lifecycle.verify(snapshot)
        # Confirmed owned STOP_LOSS orders reserve BTC, but do not consume the cash pool.
        snapshot = dict(snapshot, open_orders=sum(row['type'] != 'STOP_LOSS' for row in snapshot['orders']))
    positions, follows, exit_through = _fold(state, venue, models, snapshot)
    if compatible:
        if lifecycle is None:
            state._recovery_only = True
            raise Blocked('matching legacy checkpoint needs owned account reconciliation before strategy takeover')
        if prior_rule in COMPATIBLE_RULES:
            positions = _legacy_peaks(state, positions)
    positions = _observe_quotes(positions, snapshot, _now_ms(venue))
    if fresh:
        for model in models.values():
            model.note_flat()
        enabled = False
    if _entries_blocked(models, exit_through):
        enabled = False
    views = {}
    owned = {}
    for window, model in models.items():
        views[window], owned[window] = _view(model, positions[window])
    proposed = decision(views, owned, snapshot,
        entries_enabled=enabled, capital_limit=config.capital_limit,
        positions=positions, owners=getattr(state, '_execution_owners', None) or {},
        crowding_source=crowding_source, decision_ms=_now_ms(venue))
    follows = _follow_after(proposed, models, positions, follows, exit_through)
    reference = models[SLEEVES[0]]
    _commit(state, models, positions, follows, fresh, reference.last, expire_buys=compatible)
    if compatible:
        lifecycle._rows = None
        state._recovery_only = False
    if (not features_loaded and hasattr(venue, 'crowding_features')
            and any(item.get('blocked_reason') != 'held_sleeve_no_topup' for item in proposed['crowding'])):
        # Keep the verified account observation, then refresh spot after the public read.
        source = venue.crowding_features()
        return _cycle(venue, state, config, lifecycle=lifecycle,
                      crowding_source=source, features_loaded=True)
    return {
        'status': 'read_only',
        'model_preview': proposed,
        'actual': _public_snapshot(actual_snapshot),
        'risk_state': _risk_state(state, actual_snapshot, venue),
        'execution_evidence': _execution_evidence(state, actual_snapshot),
        'market_through': reference.last,
        'model_bull': {str(window): model.bull for window, model in models.items()},
        'entries_enabled': enabled,
        'followed_position': any(item is not None for item in positions.values()),
        'followed_sleeves': [window for window, item in positions.items() if item is not None],
        'observation_current': True,
        'recorded_limits': dict(RECORDED_LIMITS),
        'write_attempted': False,
    }


def _observe_quotes(positions, snapshot, now_ms):
    """Raise only the actual position's target from a verified post-fill quote."""
    if snapshot.get('last_price') is None:
        return positions
    price = number(snapshot['last_price'], 'observed last price', positive=True)
    stamp = snapshot.get('quote_observed_ms', now_ms)
    if type(stamp) is not int or stamp > now_ms or stamp < ORIGIN:
        raise Unknown('quote observation time is invalid')
    out = dict(positions)
    for window, position in positions.items():
        if position is None or position.get('dust'):
            continue
        if stamp < position.get('quote_through_ms', stamp):
            raise Unknown('quote observation clock moved backwards')
        if stamp < position['first_ms']:
            continue
        out[window] = dict(position, peak=format(max(D(position['peak']), price), 'f'), quote_through_ms=stamp)
        if position['repair'] and position['repair_peak'] is not None:
            out[window]['repair_peak'] = format(max(D(position['repair_peak']), price), 'f')
    return out


def _legacy_peaks(state, positions):
    """Discard unproven daily peaks; confirmed native floors remain separate."""
    out = dict(positions)
    owners = getattr(state, '_execution_owners', None) or {}
    for window, position in positions.items():
        if position is None or position.get('dust'):
            continue
        prices = []
        first_proven = False
        for encoded, in state.db.execute('SELECT payload FROM fills WHERE time_ms>=?', (position['first_ms'],)):
            fill = json.loads(encoded)
            owner = owners.get(str(fill['order_id'])) or {}
            if (fill['buyer'] and window in owner.get('sleeves', [])
                    and owner.get('order', {}).get('side') == 'BUY'):
                prices.append(number(fill['price'], 'owned fill price', positive=True))
                first_proven |= fill['time'] == position['first_ms']
        if not first_proven:
            state._recovery_only = True
            raise Blocked('legacy position has no proven first BUY fill; retain native protection and reconcile')
        out[window] = dict(position, peak=format(max(prices), 'f'))
        out[window].pop('quote_through_ms', None)
        if position['repair']:
            out[window]['repair_peak'] = out[window]['peak']
    return out


def run(config, venue, *, execute=False, monotonic=time.monotonic, wait=time.sleep, stopping=lambda: False):
    """Run until the deadline or Ctrl-C with explicitly scoped execution permission."""
    venue.write_attempted = False
    venue._session_sent_start = len(getattr(venue, 'sent', ()))
    started = monotonic()
    deadline = started + config.session_seconds
    report = {
        'status': 'read_only',
        'cycles': 0,
        'write_attempted': False,
        'errors': [],
        'exchange': 'Binance',
        'environment': config.environment,
        'symbol': 'BTCUSDT',
        'market': 'spot',
        'leverage': '0',
        'sleeves': list(SLEEVES),
        'stop_reason': 'deadline',
        'session_started_at_ms': _now_ms(venue),
        'runtime_identity': {'rule': RULE, 'source_sha': os.environ.get('SPOTQUANT_SOURCE_SHA')},
        'session_ended': False,
        'stops_while_down': 'this process does not amend a stop while it is stopped',
        'recorded_limits': dict(RECORDED_LIMITS),
    }
    venue._monotonic = monotonic
    venue._risk_deadline_at = deadline
    venue._deadline_at = deadline + (30 if execute else 0)
    stop_at = None
    def requested():
        nonlocal stop_at
        if stop_at is None and stopping():
            stop_at = monotonic()
            venue._risk_deadline_at = min(deadline, stop_at)
            venue._deadline_at = min(venue._deadline_at, stop_at + (30 if execute else 0))
        return stop_at is not None
    venue._risk_stop = lambda: monotonic() >= deadline or requested()
    if hasattr(venue, '_stop'):
        venue._stop = lambda: monotonic() >= venue._deadline_at
    with State(config.state_dir, config.scope) as state:
        closeout = False
        try:
            while monotonic() < deadline and not requested():
                report['cycles'] += 1
                report['observation_current'] = False
                report.pop('risk_state', None)
                report.pop('execution_evidence', None)
                try:
                    current = cycle(venue, state, config, execute=execute)
                    report.update(current)
                    report['observation_current'] = True
                    report.pop('reason', None)
                except (Blocked, Unknown) as exc:
                    report.update(
                        status='unknown' if isinstance(exc, Unknown) else 'blocked',
                        reason=str(exc),
                        observation_current=False,
                    )
                    report['errors'] = (report['errors'] + [{'cycle': report['cycles'], 'reason': str(exc)}])[-10:]
                    clear_stale(report)
                    if execute:
                        try:
                            report['risk_state'] = _risk_state(state, venue.snapshot(config.account_uid), venue)
                        except (Blocked, Unknown, OSError, ValueError, KeyError, TypeError):
                            report['risk_state'] = {'direction': 'unknown', 'unprotected_btc': None,
                                                    'manual_takeover': True, 'observation_current': False}
                    if 'rate limit' in str(exc) or 'session deadline' in str(exc):
                        report['stop_reason'] = 'rate_limit' if 'rate limit' in str(exc) else 'deadline'
                        break
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
                    report.update(status='unknown', reason='Invalid observation or state', observation_current=False)
                    report['errors'] = (report['errors'] + [{
                        'cycle': report['cycles'], 'reason': 'Invalid observation or state',
                    }])[-10:]
                    clear_stale(report)
                report['pending_intents'] = len(state.pending())
                if report['pending_intents']:
                    report.update(status='unknown', reason='Durable intents require recovery', observation_current=False)
                    clear_stale(report)
                    _mark_unknown_exposure(report, state)
                state.report(report)
                remaining = deadline - monotonic()
                if remaining > 0 and not requested():
                    wait(min(config.poll_seconds, remaining))
            if requested():
                report['stop_reason'] = 'requested'
                closeout = True
            elif deadline <= monotonic() < venue._deadline_at and (state.pending()
                    or execute and (not report.get('observation_current')
                                    or report.get('risk_state', {}).get('manual_takeover'))):
                # A settled BUY can outlive its failed observation without a
                # pending intent. Natural expiry needs the same protection recovery.
                closeout = True
        except KeyboardInterrupt:
            report['stop_reason'] = 'interrupted'
            stop_at = monotonic() if stop_at is None else stop_at
            closeout = True
        finally:
            if execute and closeout:
                venue._risk_stop = lambda: True
                venue._risk_deadline_at = min(deadline, stop_at if stop_at is not None else deadline)
                venue._deadline_at = min(deadline + 30, (stop_at if stop_at is not None else deadline) + 30)
                try:
                    _closeout(config, venue, state, report, monotonic, wait)
                except KeyboardInterrupt:
                    report.update(status='unknown', reason='protective closeout interrupted; reconcile before resuming',
                                  observation_current=False, closeout_interrupted=True,
                                  risk_state={'direction': 'unknown', 'unprotected_btc': None,
                                              'manual_takeover': True, 'observation_current': False})
                    clear_stale(report)
            report['elapsed_seconds'] = max(0, monotonic() - started)
            report['pending_intents'] = len(state.pending())
            if report['pending_intents']:
                report.update(status='unknown', reason='Durable execution requires recovery', observation_current=False)
                clear_stale(report)
                _mark_unknown_exposure(report, state)
            report.update(account_uid=config.account_uid,
                          environment=config.environment, capital_limit_usdt=str(config.capital_limit),
                          execution_enabled=execute)
            report['write_attempted'] = _writes(venue)
            report['session_ended'] = True
            report['observation_current'] = False
            report.setdefault('risk_state', {'direction': 'unknown', 'unprotected_btc': None,
                                              'manual_takeover': execute, 'observation_current': False})
            report['risk_state']['observation_current'] = False
            if execute:
                report['manual_takeover'] = bool(report['risk_state'].get('manual_takeover'))
            if getattr(state, '_recovery_only', False):
                report['recovery_only'] = True
            state.report(report)
    return report


def _closeout(config, venue, state, report, monotonic, wait):
    """Reconcile and protect after a graceful stop; the venue has new BUY disabled."""
    report['closeout_attempted'] = True
    # A settled BUY may not yet be folded into positions. Do not rely only on
    # pending intents to decide whether protective recovery is necessary.
    for _ in range(12):
        if monotonic() >= venue._deadline_at:
            break
        report['cycles'] += 1
        report.pop('risk_state', None)
        report.pop('execution_evidence', None)
        try:
            report.update(cycle(venue, state, config, execute=True))
            report.pop('reason', None)
            if not report['risk_state']['manual_takeover']:
                break
        except (Blocked, Unknown, OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            report.update(status='unknown', reason=str(exc), observation_current=False)
            # Repeated closeout failures must not erase the original refusal.
            if not report['errors'] or report['errors'][-1]['reason'] != str(exc):
                report['errors'] = (report['errors'] + [{'cycle': report['cycles'], 'reason': str(exc)}])[-10:]
            clear_stale(report)
            try:
                report['risk_state'] = _risk_state(state, venue.snapshot(config.account_uid), venue)
            except (Blocked, Unknown, OSError, ValueError, KeyError, TypeError, ArithmeticError):
                report['risk_state'] = {'direction': 'unknown', 'unprotected_btc': None,
                                        'manual_takeover': True, 'observation_current': False}
            if isinstance(exc, Blocked) or 'rate limit' in str(exc) or 'session deadline' in str(exc):
                break
        remaining = venue._deadline_at - monotonic()
        if remaining > 0:
            wait(min(config.poll_seconds, remaining))


def _mark_unknown_exposure(report, state):
    if any(row['status'] != 'prepared' for row in state.pending()):
        risk = report.setdefault('risk_state', {})
        risk.setdefault('last_observed_direction', risk.get('direction'))
        risk.update(direction='unknown', unprotected_btc=None, manual_takeover=True,
                    observation_current=False)
        report['manual_takeover'] = True


def _guard_state(state):
    """Read-only migration boundary, before any lifecycle recovery or venue request."""
    if any(state.get(key) is not None for key in (
            'lifecycle_identity', 'alpha_identity', 'edge_identity', 'adoption_risk')):
        raise Blocked('state belongs to an incompatible strategy')
    saved, rule = state.get('models'), state.get('rule')
    legacy = rule in COMPATIBLE_RULES
    compatible = legacy or rule == SAFETY_PREDECESSOR
    allowed_rule = rule == RULE or compatible
    if rule is not None and not allowed_rule:
        raise Blocked('state was written for another rule; a new directory is not a flat account')
    if saved is None:
        if (rule is not None or state.get('positions') is not None or state.get('follows') is not None
                or state.db.execute('SELECT 1 FROM intents LIMIT 1').fetchone()):
            raise Blocked('missing model checkpoint for durable state')
        return None
    if not allowed_rule or type(saved) is not dict or set(saved) != {str(w) for w in SLEEVES}:
        raise Blocked('state rule or sleeve checkpoint identity mismatch')
    model = Model.restore(saved[str(SLEEVES[0])])
    if model.sma_window != SLEEVES[0]:
        raise Blocked('model checkpoint sleeve mismatch')
    try:
        positions, follows = state.get('positions'), state.get('follows')
        keys = {str(w) for w in SLEEVES}
        anchor = state.get('entries_after')
        last = model.last
        if (type(positions) is not dict or set(positions) != keys
                or type(follows) is not dict or set(follows) != keys
                or type(anchor) is not int or last is None or anchor > last or anchor != day_open(anchor)):
            raise ValueError('state layout')
        for key in keys:
            position, follow = positions[key], follows[key]
            if position is not None:
                values = [D(position[k]) for k in ('qty', 'entry_fill', 'peak')]
                if 'entry_gross_qty' in position:
                    values.append(D(position['entry_gross_qty']))
                if position['repair_peak'] is not None:
                    values.append(D(position['repair_peak']))
                if (any(not value.is_finite() or value <= 0 for value in values)
                        or position['entry_open_ms'] != day_open(position['first_ms'])
                        or any(type(position[k]) is not bool for k in ('repair', 'adverse'))
                        or ('dust' in position and (type(position['dust']) is not bool
                            or position['dust'] and D(position['qty']) >= BASE_STEP))
                        or ('sell_stop_breached' in position and type(position['sell_stop_breached']) is not bool)
                        or ('quote_through_ms' in position and (type(position['quote_through_ms']) is not int
                            or position['quote_through_ms'] < position['first_ms']))
                        or (position['through'] is not None and (type(position['through']) is not int
                            or position['through'] > last or position['through'] != day_open(position['through'])))):
                    raise ValueError('position')
            if follow is not None and (type(follow['repair']) is not bool
                    or type(follow['signal_ms']) is not int or follow['signal_ms'] > last
                    or follow['signal_ms'] != day_open(follow['signal_ms'])):
                raise ValueError('follow')
    except (KeyError, TypeError, ValueError, ArithmeticError, Unknown) as exc:
        raise Blocked('incomplete or malformed position checkpoint') from exc
    # Pending dispatch/recovery must never run on malformed or foreign allocations.
    from .execution import FIELDS, _no_fill_failure
    try:
        for identity, kind, encoded, status in state.db.execute(
                "SELECT id,kind,payload,status FROM intents WHERE status NOT IN ('settled','rejected') OR ?",
                (compatible,)):
            payload = json.loads(encoded)
            group, order = payload['sleeves'], payload['order']
            allowed = {'prepared', 'unknown', 'resting', 'canceling'}
            if compatible:
                allowed |= {'settled', 'rejected'}
            if (kind != 'p4' or status not in allowed
                    or type(group) is not list or not group or group != sorted(set(group))
                    or any(type(w) is not int or w not in SLEEVES for w in group)
                    or set(payload['weights']) != {str(w) for w in group}
                    or set(payload['repair']) != {str(w) for w in group}
                    or any(type(v) is not bool for v in payload['repair'].values())
                    or type(payload['signal_ms']) is not int
                    or payload['signal_ms'] != day_open(payload['signal_ms'])
                    or not set(order) <= set(FIELDS) or order['symbol'] != 'BTCUSDT'
                    or order['side'] not in ('BUY', 'SELL') or order['type'] not in ('MARKET', 'STOP_LOSS')
                    or (order['type'] == 'STOP_LOSS' and order['side'] != 'SELL')):
                raise ValueError('allocation')
            rearm = payload.get('rearm')
            if 'rearm' in payload and (type(rearm) is not dict or set(rearm) != {str(w) for w in group}
                    or any(type(v) is not bool for v in rearm.values())
                    or (order['side'] != 'SELL' or order['type'] != 'MARKET') and any(rearm.values())):
                raise ValueError('rearm permission')
            first = payload.get('position_first_ms')
            if 'position_first_ms' in payload and (order['type'] != 'STOP_LOSS'
                    or type(first) is not dict or set(first) != {str(w) for w in group}
                    or any(type(stamp) is not int or stamp < ORIGIN for stamp in first.values())):
                raise ValueError('protection position identity')
            quantities = list(payload['weights'].values())
            quantities.append(order['quoteOrderQty'] if order['side'] == 'BUY' else order['quantity'])
            if order['type'] == 'STOP_LOSS':
                quantities.append(order['stopPrice'])
            if any(not D(v).is_finite() or D(v) <= 0 for v in quantities):
                raise ValueError('quantity')
            cancel_id = payload.get('cancel_id')
            if cancel_id is not None and (order['type'] != 'STOP_LOSS' or cancel_id != 'sq-' +
                    hashlib.sha256((identity + '|cancel').encode()).hexdigest()[:30]):
                raise ValueError('cancellation identity')
            reduction = payload.get('reduction_after')
            operation = order['side'] + '-' + order['type'] + '-' + ','.join(map(str, group))
            if reduction is not None:
                base = client_id(state.identity, payload['signal_ms'], operation)
                prior = state.db.execute('SELECT kind,payload,status FROM intents WHERE id=?', (base,)).fetchone()
                if (order['side'] != 'SELL' or order['type'] != 'MARKET' or reduction != base
                        or prior is None or prior[0] != 'p4' or prior[2] not in ('settled', 'rejected')):
                    raise ValueError('reduction parent')
                original = json.loads(prior[1])
                if (original['sleeves'] != group or original['signal_ms'] != payload['signal_ms']
                        or original['order']['symbol'] != 'BTCUSDT'
                        or original['order']['side'] != 'SELL' or original['order']['type'] != 'MARKET'
                        or not D(original['order']['quantity']).is_finite()
                        or D(original['order']['quantity']) <= D(order['quantity'])):
                    raise ValueError('reduction amount')
                operation += '-remainder-' + hashlib.sha256(
                    (base + '|' + format(D(order['quantity']), 'f')).encode()).hexdigest()[:16]
            retry = payload.get('retry_after_reject')
            if retry is not None:
                parent = client_id(state.identity, payload['signal_ms'], operation)
                prior = state.db.execute('SELECT kind,payload,status,result FROM intents WHERE id=?',
                                         (parent,)).fetchone()
                if (order['side'] != 'SELL' or order['type'] != 'MARKET' or retry != parent
                        or prior is None or prior[0] != 'p4'):
                    raise ValueError('retry parent')
                original, result = json.loads(prior[1]), json.loads(prior[3])
                if (not _no_fill_failure((parent, original, prior[2], result))
                        or number(result.get('executedQty') or 0) != 0
                        or original.get('retry_after_reject')
                        or original['sleeves'] != group or original['signal_ms'] != payload['signal_ms']
                        or original['order']['symbol'] != 'BTCUSDT'
                        or original['order']['side'] != 'SELL' or original['order']['type'] != 'MARKET'
                        or D(original['order']['quantity']) != D(order['quantity'])):
                    raise ValueError('retry requires a confirmed zero-fill failure')
                operation += '-after-reject'
            if (reduction is not None or retry is not None) and identity != client_id(
                    state.identity, payload['signal_ms'], operation):
                raise ValueError('reduction or retry identity')
            if legacy:
                fields = {'symbol', 'side', 'type', 'quoteOrderQty' if order['side'] == 'BUY' else 'quantity'}
                if order['type'] == 'STOP_LOSS':
                    fields.add('stopPrice')
                if set(order) != fields:
                    raise ValueError('native order fields')
                if order['type'] == 'STOP_LOSS':
                    operation += '-' + hashlib.sha256(json.dumps(order, sort_keys=True).encode()).hexdigest()[:16]
                if identity != client_id(state.identity, payload['signal_ms'], operation):
                    raise ValueError('durable order identity')
    except (KeyError, TypeError, ValueError, ArithmeticError, Unknown) as exc:
        raise Blocked('incompatible durable pending allocation') from exc
    return {SLEEVES[0]: model}


def _load_models(state: State, venue):
    state._staged = None
    state._seen_trades = []
    saved = state.get('models')
    anchor = state.get('entries_after')
    models = _guard_state(state) or {window: Model(window) for window in SLEEVES}
    last = models[SLEEVES[0]].last
    for open_ms, open_price, high, low, close in venue.completed_daily(None if saved is None else last):
        _consume_bar(state, venue, models, open_ms, open_price, high, low, close)
    if models[SLEEVES[0]].last is None:
        raise Unknown('no completed daily bar is available to anchor the model')
    open_ms = models[SLEEVES[0]].last + DAY
    if open_ms > _now_ms(venue):
        raise Unknown('daily open has not arrived')
    if any(model.shadow_open_ms != open_ms for model in models.values()):
        observed_ms, open_price = venue.daily_open(open_ms)
        if observed_ms != open_ms:
            raise Unknown('daily open response does not match the model clock')
        for model in models.values():
            model.advance_open(observed_ms, open_price)
    enabled = saved is not None and anchor is not None and models[SLEEVES[0]].last > int(anchor)
    return models, enabled, saved is None


def _entries_blocked(models: dict, exit_through: dict) -> bool:
    """True when every sleeve is still inside the bar that consumed its last entry."""
    if not exit_through:
        return False
    for window, model in models.items():
        blocked = exit_through.get(str(window))
        if blocked is None or model.last is None or model.last > int(blocked):
            return False
    return True


def _consume_bar(state, venue, models, open_ms, open_price, high, low, close):
    """Fills of this day land on the model as it stood before the bar."""
    if open_ms + DAY > _now_ms(venue):
        raise Unknown('daily bar is not completed')
    for model in models.values():
        model.advance_open(open_ms, open_price)
    positions, follows, accounted, exit_through, cursor = _stored(state)
    trades = _trades_for(state, venue, positions, follows, cursor)
    positions, follows, accounted, closed = _apply_book(
        state, models, positions, follows, accounted, open_ms, trades,
        lambda: (bar for bar in venue.completed_daily(None) if bar[0] < open_ms),
    )
    for window in closed:
        exit_through[str(window)] = models[window].last
    step_models = {}
    for window, model in models.items():
        model.update(open_ms, high, low, close)
        step_models[window] = {
            'open_ms': open_ms,
            'high': high,
            'low': low,
            'close': close,
            'bull': model.bull,
            'cap_high': model._view_cap_high(),
        }
    for window, item in positions.items():
        if item is not None and not item.get('dust'):
            positions[window] = advance(item, step_models[window], models[window])
    cursor, accounted = _cursor_after(trades, accounted, cursor)
    state._staged = (positions, follows, accounted, exit_through, cursor)


def _book_owners(state):
    owners = getattr(state, '_execution_owners', None)
    return {} if owners is None else owners


def _apply_book(state, models, positions, follows, accounted, open_ms, trades, history):
    """Fills without a durable allocation stay external and drop any armed follow."""
    try:
        return apply_day(models, positions, follows, accounted, open_ms, trades, history,
                         owners=_book_owners(state))
    except Unknown as exc:
        if 'no durable order allocation' in str(exc):
            state.set('follows', {str(window): None for window in SLEEVES})
        raise


def _fold(state, venue, models, snapshot):
    """Fills after the last completed bar, then the balance check.

    Bars themselves are consumed in ``_load_models`` so a sell and the later
    bars of one catch-up see the model in time order. The stash is memory on
    the state object until ``_commit``.
    """
    positions, follows, accounted, exit_through, cursor = _stored(state)
    trades = _trades_for(state, venue, positions, follows, cursor)
    last = models[SLEEVES[0]].last
    later = sorted({
        day_open(trade['time']) for trade in trades
        if trade['id'] not in accounted and (last is None or day_open(trade['time']) > last)
    })
    for open_ms in later:
        positions, follows, accounted, closed = _apply_book(
            state, models, positions, follows, accounted, open_ms, trades,
            lambda: venue.completed_daily(None),
        )
        for window in closed:
            exit_through[str(window)] = models[window].last
    mark = models[SLEEVES[0]].close
    if any(item is not None for item in positions.values()) or any(follows.values()):
        unexplained(positions, D(snapshot['btc']), mark)
    elif mark is not None and D(snapshot['btc']) * max(mark, D(snapshot.get('last_price') or mark)) >= MIN_NOTIONAL:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    cursor, accounted = _cursor_after(trades, accounted, cursor)
    state._staged = (positions, follows, accounted, exit_through, cursor)
    return positions, follows, exit_through


def _stored(state: State):
    staged = getattr(state, '_staged', None)
    if staged is not None:
        return staged
    positions = {window: (state.get('positions') or {}).get(str(window)) for window in SLEEVES}
    follows = {window: (state.get('follows') or {}).get(str(window)) for window in SLEEVES}
    accounted = set(state.get('accounted_ids') or [])
    exit_through = dict(state.get('exit_through') or {})
    return positions, follows, accounted, exit_through, state.get('trade_cursor_ms')


def _trades_for(state, venue, positions, follows, cursor):
    """Read fills from the cursor, not from the epoch.

    A flat account that has already recorded trades keeps the cursor at the
    latest accounted millisecond and reads that overlap again. Same-millisecond
    trades are kept by id. The cursor does not move past a trade this cycle
    has not accounted, so a buy that arrives before its follow is not dropped.
    """
    starts = [int(item['first_ms']) for item in positions.values() if item is not None]
    starts += [
        int(item['signal_ms']) + DAY for item in follows.values()
        if item and item.get('signal_ms') is not None
    ]
    if cursor is not None:
        starts.append(int(cursor))
    if not starts:
        return []
    trades = state.trades(venue, min(starts))
    if cursor is not None:
        trades = [trade for trade in trades if trade['time'] >= int(cursor)]
    seen = getattr(state, '_seen_trades', [])
    state._seen_trades = seen + trades
    return trades


def _cursor_after(trades, accounted, cursor):
    """Move the cursor to the newest accounted trade that is not past an open one."""
    done = [trade for trade in trades if trade['id'] in accounted]
    if not done:
        return cursor, set(accounted)
    pending = [trade['time'] for trade in trades if trade['id'] not in accounted]
    if pending:
        limit = min(pending)
        done = [trade for trade in done if trade['time'] < limit]
        if not done:
            return cursor, set(accounted)
    new_cursor = max(trade['time'] for trade in done)
    if cursor is not None:
        new_cursor = max(int(cursor), new_cursor)
    keep = {trade['id'] for trade in trades if trade['id'] in accounted and trade['time'] >= new_cursor}
    return new_cursor, keep


def _commit(state, models, positions, follows, fresh, last, *, expire_buys=False):
    _, _, accounted, exit_through, cursor = _stored(state)
    values = {
        'rule': RULE,
        'models': {str(window): model.checkpoint() for window, model in models.items()},
        'positions': {str(window): item for window, item in positions.items()},
        'follows': {str(window): item for window, item in follows.items()},
        'accounted_ids': sorted(accounted),
        'exit_through': {str(key): value for key, value in exit_through.items()},
        'trade_cursor_ms': cursor,
    }
    if fresh:
        values['entries_after'] = last
    with state.db:
        if expire_buys:
            for identity, encoded in state.db.execute(
                    "SELECT id,payload FROM intents WHERE kind='p4' AND status='prepared'"):
                if json.loads(encoded)['order']['side'] == 'BUY':
                    state.db.execute("UPDATE intents SET status='settled',result=?,updated=? WHERE id=?",
                                     (json.dumps({'not_sent': True, 'reason': 'entry policy upgraded'}),
                                      time.time(), identity))
        state.set_many(values)
    state._staged = None


def _follow_after(decision: dict, models: dict, positions: dict, follows: dict, exit_through: dict) -> dict:
    """Remember a previewed entry until its fill is recorded or the signal is gone.

    A sleeve that exited on the current completed bar does not arm again until
    a newer bar. New entries cannot reuse the exit bar.
    """
    out = {}
    for window, model in models.items():
        sleeve = decision['sleeves'].get(str(window), {})
        follow = follows.get(window)
        blocked = exit_through.get(str(window))
        if positions[window] is not None and not positions[window].get('dust'):
            out[window] = None
        elif blocked is not None and model.last is not None and model.last <= int(blocked):
            out[window] = None
        elif sleeve.get('action') == 'enter' and model.last is not None:
            if follow and follow.get('signal_ms') is not None:
                out[window] = {'signal_ms': follow['signal_ms'], 'repair': bool(sleeve['repair'])}
            else:
                out[window] = {
                    'signal_ms': model.last,
                    'repair': bool(sleeve['repair']),
                }
        elif not (model.enter or model.cap_enter):
            out[window] = None
        else:
            out[window] = follow
    return out


def _view(model: Model, position):
    if position is None:
        return model, D(0)
    if position.get('dust'):
        # The closed strategy is flat; every residual coin still stays owned.
        view = copy(model)
        view._owned_dust = True
        return view, D(position['qty'])
    view = copy(model)
    view.entry = D(position['entry_fill'])
    view.position_peak = D(position['peak'])
    view.repair = bool(position['repair'])
    view.repair_peak = None if position['repair_peak'] is None else D(position['repair_peak'])
    view.adverse = bool(position['adverse'])
    view.protection = position.get('protection', 'resting')
    return view, D(position['qty'])


def _public_snapshot(snapshot: dict) -> dict:
    """Balances for the report. Filter multipliers stay on the preview, not here."""
    out = {key: snapshot.get(key) for key in (
        'account_uid', 'btc', 'usdt_free', 'usdt_locked', 'open_orders', 'orders',
        'environment', 'last_price', 'avg_price',
    )}
    out['orders'] = list(out['orders'] or [])
    return out


def _risk_state(state, snapshot, venue):
    """Conservative local BTC exposure summary."""
    btc = D(snapshot['btc'])
    open_orders = {row['order_id']: row for row in snapshot.get('orders') or []}
    covered = D(0)
    confirmed = set()
    for identity, encoded, result in state.db.execute(
            "SELECT id,payload,result FROM intents WHERE kind='p4' AND status='resting'"):
        payload = json.loads(encoded)
        order = payload['order']
        native = json.loads(result)
        row = open_orders.get(native.get('orderId'))
        names = {identity} | ({payload['cancel_id']} if payload.get('cancel_id') else set())
        if (order.get('symbol') != 'BTCUSDT' or order.get('side') != 'SELL'
                or order.get('type') != 'STOP_LOSS' or row is None
                or row.get('client_id') not in names or row.get('side') != 'SELL'
                or row.get('type') != 'STOP_LOSS'
                or row.get('status') != 'NEW'):
            continue
        try:
            quantity = number(order.get('quantity'), 'owned stop quantity', positive=True)
            stop = number(order.get('stopPrice'), 'owned stop price', positive=True)
            original = number(row.get('orig_qty'), 'observed stop quantity', positive=True)
            executed = number(row.get('executed_qty'), 'observed stop execution', nonnegative=True)
            if (original != quantity or executed != 0
                    or number(row.get('stop_price'), 'observed stop price', positive=True) != stop):
                continue
        except Blocked:
            continue
        covered += original - executed
        confirmed.add(row['order_id'])
    unprotected = max(D(0), btc - covered)
    price = snapshot.get('last_price')
    sellable = floor_step(btc, BASE_STEP)
    gap = max(D(0), sellable - min(covered, sellable))
    minimum = D(snapshot.get('min_notional') or MIN_NOTIONAL)
    # A small missing slice can be restored by replacing the whole stop. Only
    # an entire lot below the venue minimum is an untradeable residual.
    reference = snapshot.get('avg_price') or price
    below_minimum = ((reference is not None and sellable * D(reference) < minimum)
                     or (snapshot.get('min_qty') is not None and sellable < D(snapshot['min_qty'])))
    tradable_gap = gap > 0 and not below_minimum
    unconfirmed = sorted(set(open_orders) - confirmed)
    unvalued = state.get('third_asset_fees_unvalued') or []
    risk = {'account_uid': snapshot['account_uid'], 'environment': snapshot['environment'],
            'symbol': 'BTCUSDT', 'direction': 'long' if btc else 'flat',
            'btc': btc, 'usdt': D(snapshot['usdt_free']) + D(snapshot['usdt_locked']),
            'last_price': snapshot.get('last_price'), 'covered_btc': min(btc, covered),
            'unprotected_btc': unprotected,
            'residual_btc': D(0) if tradable_gap else unprotected,
            'manual_takeover': tradable_gap or getattr(state, '_recovery_only', False),
            'unconfirmed_order_ids': unconfirmed,
            'recovery_only': getattr(state, '_recovery_only', False),
            'fee_valuation_complete': not bool(unvalued), 'unvalued_fee_assets': unvalued,
            'observed_at_ms': _now_ms(venue), 'observation_current': True}
    if unconfirmed:
        risk.update(last_observed_direction=risk['direction'], direction='unknown',
                    unprotected_btc=None, manual_takeover=True, observation_current=False)
    _mark_unknown_exposure({'risk_state': risk}, state)
    return risk


def _execution_evidence(state, snapshot):
    """Small local readback for later fill, fee, and slippage review."""
    owners = {}
    for payload, result in state.db.execute("SELECT payload,result FROM intents WHERE kind='p4'"):
        native = json.loads(result)
        if type(native.get('orderId')) is int:
            owners[native['orderId']] = json.loads(payload)
    fills = []
    seen = set()
    for trade in reversed(getattr(state, '_seen_trades', [])):
        if trade['id'] in seen:
            continue
        seen.add(trade['id'])
        owner = owners.get(trade['order_id']) or {}
        order = owner.get('order') or {}
        reference = order.get('stopPrice') if order.get('type') == 'STOP_LOSS' else (owner.get('quote_reference') or {}).get('last_price')
        fill_price = D(trade['price']) if 'price' in trade else D(trade['quote']) / D(trade['qty'])
        slippage = None if reference is None else (
            (fill_price / D(reference) - 1) if trade['buyer'] else (1 - fill_price / D(reference))) * 100
        fills.append({'trade_id': trade['id'], 'order_id': trade['order_id'],
                      'price': fill_price, 'quantity': trade['qty'], 'quote': trade['quote'],
                      'commission': trade['commission'], 'commission_asset': trade['commission_asset'],
                      'reference_price': reference, 'slippage_pct': slippage})
        if len(fills) == 20:
            break
    return {'quote': {'last_price': snapshot.get('last_price'), 'avg_price': snapshot.get('avg_price')},
            'fee_status': snapshot.get('fee_status'),
            'fills': list(reversed(fills)),
            'protection_orders': [row for row in snapshot.get('orders') or [] if row['type'] == 'STOP_LOSS']}
