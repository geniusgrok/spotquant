"""One bounded session: read-only by default, explicit owner Demo execution."""
from __future__ import annotations

import time
from decimal import Decimal as D

from .follow import advance, apply_day, day_open, unexplained
from .model import DAY, SLEEVES, Model
from .preview import MIN_NOTIONAL, portfolio
from .state import State
from .types import Blocked, Unknown

# A state directory written by another rule is not reused over an open position.
RULE = '2026-09-29-static-stop'

# Dropped when a cycle fails so the previous success cannot be read as current.
STALE_REPORT_FIELDS = (
    'actual', 'model_preview', 'market_through', 'model_bull', 'entries_enabled',
    'followed_position', 'followed_sleeves',
)

RECORDED_LIMITS = {
    'adverse_exit': 'next_open',
    'adverse_loss_capped': False,
    'path_convention': 'stop_from_prior_peak',
    'execution': 'shared_session_lifecycle; native Demo unverified; live blocked',
    'selection': 'full_sample',
    'sleeves': list(SLEEVES),
    'economic_targets_met': False,
    'skip_stress_targets_met': False,
}


def clear_stale(report: dict) -> None:
    for key in STALE_REPORT_FIELDS:
        report.pop(key, None)


def cycle(venue, state: State, config, *, execute=False) -> dict:
    lifecycle = None
    if execute:
        from .execution import Lifecycle
        lifecycle = Lifecycle(state, venue, config)
    else:
        import json
        allocated = list(state.db.execute("SELECT payload,result FROM intents WHERE kind='p4'"))
        state._execution_owners = ({str(json.loads(result)['orderId']): json.loads(payload)
                                   for payload, result in allocated if 'orderId' in json.loads(result)}
                                  if allocated else None)
    for _ in range(12 if execute else 1):
        if lifecycle:
            lifecycle.recover()
            state._execution_owners = lifecycle.owners()
        current = _cycle(venue, state, config, lifecycle=lifecycle)
        if not lifecycle or not lifecycle.act(current['model_preview'], current['market_through']):
            return dict(current, write_attempted=_writes(venue),
                        status='offline_execution' if getattr(venue, 'offline', False) and execute else
                        'demo_execution' if execute else 'read_only')
    raise Unknown('bounded execution cycle exhausted; reconcile on the next cycle')


def _writes(venue):
    return bool(getattr(venue, 'write_attempted', False)
                or len(getattr(venue, 'sent', ())) > getattr(venue, '_session_sent_start', 0))


def _cycle(venue, state: State, config, *, lifecycle=None) -> dict:
    """Derive the whole observation in memory, then commit it once.

    A failed snapshot or preview leaves the previous checkpoint and positions
    where they were, so the next cycle replays the same bars onto both.
    """
    if getattr(venue, 'environment', None) != config.environment:
        raise Blocked('exchange adapter and configuration differ in environment')
    if getattr(venue, 'capital_limit', None) != config.capital_limit:
        raise Blocked('exchange adapter and configuration differ in capital limit')
    models, enabled, fresh = _load_models(state, venue)
    snapshot = venue.snapshot(config.account_uid)
    actual_snapshot = snapshot
    if lifecycle:
        lifecycle.verify(snapshot)
        # Confirmed owned STOP_LOSS orders reserve BTC, but do not consume the cash pool.
        snapshot = dict(snapshot, open_orders=sum(row['type'] != 'STOP_LOSS' for row in snapshot['orders']))
    positions, follows, accounted, exit_through, _cursor = _fold(state, venue, models, snapshot)
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
    decision = portfolio(
        views, owned, snapshot, entries_enabled=enabled, capital_limit=config.capital_limit)
    follows = _follow_after(decision, models, positions, follows, exit_through)
    reference = models[SLEEVES[0]]
    _commit(state, models, positions, follows, accounted, exit_through, fresh, reference.last)
    return {
        'status': 'read_only',
        'model_preview': decision,
        'actual': _public_snapshot(actual_snapshot),
        'market_through': reference.last,
        'model_bull': {str(window): model.bull for window, model in models.items()},
        'entries_enabled': enabled,
        'followed_position': any(item is not None for item in positions.values()),
        'followed_sleeves': [window for window, item in positions.items() if item is not None],
        'observation_current': True,
        'recorded_limits': dict(RECORDED_LIMITS),
        'write_attempted': False,
    }


def run(config, venue, *, execute=False, monotonic=time.monotonic, wait=time.sleep, stopping=lambda: False):
    """Run until the deadline or Ctrl-C; execution needs the explicit Demo gate."""
    venue.write_attempted = False
    venue._session_sent_start = len(getattr(venue, 'sent', ()))
    started = monotonic()
    deadline = started + config.session_seconds
    report = {
        'status': 'read_only',
        'cycles': 0,
        'write_attempted': False,
        'errors': [],
        'qualification': 'NOT_QUALIFIED',
        'exchange': 'Binance',
        'environment': config.environment,
        'symbol': 'BTCUSDT',
        'market': 'spot',
        'leverage': '0',
        'sleeves': list(SLEEVES),
        'stop_reason': 'deadline',
        'session_started_at_ms': int(venue.clock() * 1000),
        'session_ended': False,
        'stops_while_down': 'this process does not amend a stop while it is stopped',
        'recorded_limits': dict(RECORDED_LIMITS),
    }
    if hasattr(venue, '_stop'):
        venue._stop = lambda: monotonic() >= deadline
    with State(config.state_dir, config.scope) as state:
        try:
            while monotonic() < deadline and not stopping():
                report['cycles'] += 1
                report['observation_current'] = False
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
                state.report(report)
                remaining = deadline - monotonic()
                if remaining > 0 and not stopping():
                    wait(min(config.poll_seconds, remaining))
            if stopping():
                report['stop_reason'] = 'requested'
        except KeyboardInterrupt:
            report['stop_reason'] = 'interrupted'
        finally:
            report['elapsed_seconds'] = max(0, monotonic() - started)
            report['pending_intents'] = len(state.pending())
            if report['pending_intents']:
                report.update(status='unknown', reason='Durable execution requires recovery', observation_current=False)
                clear_stale(report)
            import hashlib
            from pathlib import Path
            digest = hashlib.sha256()
            for path in sorted(Path(__file__).parent.glob('*.py')):
                digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
            report.update(execution_code_sha256=digest.hexdigest(), account_uid=config.account_uid,
                          environment=config.environment, capital_limit_usdt=str(config.capital_limit),
                          execution_enabled=execute, native_execution_verified=False)
            report['write_attempted'] = _writes(venue)
            report['session_ended'] = True
            report['observation_current'] = False
            report['stops_while_down'] = 'this process does not amend a stop while it is stopped'
            state.report(report)
    return report


def _load_models(state: State, venue):
    state._staged = None
    state._seen_trades = []
    saved = state.get('models')
    anchor = state.get('entries_after')
    if saved is not None and set(saved) != {str(window) for window in SLEEVES}:
        raise Blocked('state was written for other sleeves; use a new state directory')
    stored_rule = state.get('rule')
    positions = state.get('positions') or {}
    held = any(positions.get(str(window)) is not None for window in SLEEVES)
    if held and stored_rule != RULE:
        raise Blocked('state was written for another rule; a new directory is not a flat account')
    models = {}
    for window in SLEEVES:
        models[window] = Model(window) if saved is None else Model.restore(saved[str(window)])
        if models[window].sma_window != window:
            raise Blocked('model checkpoint does not match this sleeve')
    last = models[SLEEVES[0]].last
    if saved is not None and any(model.last != last for model in models.values()):
        raise Blocked('sleeve checkpoints are not on the same daily bar')
    if saved is not None and stored_rule != RULE and not held:
        # A directory written before this rule has no position to protect.
        # Flatten the restored entry so an already-bullish regime waits for a fresh cross.
        for model in models.values():
            model.note_flat()
        exit_through = {
            str(window): model.last for window, model in models.items() if model.last is not None
        }
        _stash(state, {window: None for window in SLEEVES}, {window: None for window in SLEEVES},
               set(state.get('accounted_ids') or []), exit_through, state.get('trade_cursor_ms'))
    for open_ms, high, low, close in venue.completed_daily(None if saved is None else last):
        _consume_bar(state, venue, models, open_ms, high, low, close)
    if models[SLEEVES[0]].last is None:
        raise Unknown('no completed daily bar is available to anchor the model')
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


def _consume_bar(state, venue, models, open_ms, high, low, close):
    """Fills of this day land on the model as it stood before the bar."""
    positions, follows, accounted, exit_through, cursor = _stored(state)
    trades = _trades_for(state, venue, positions, follows, cursor)
    positions, follows, accounted, closed = apply_day(
        models, positions, follows, accounted, open_ms, trades, lambda: venue.completed_daily(None),
        owners=getattr(state, '_execution_owners', None),
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
        if item is not None:
            positions[window] = advance(item, step_models[window], models[window])
    cursor, accounted = _cursor_after(trades, accounted, cursor)
    _stash(state, positions, follows, accounted, exit_through, cursor)


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
        positions, follows, accounted, closed = apply_day(
            models, positions, follows, accounted, open_ms, trades, lambda: venue.completed_daily(None),
            owners=getattr(state, '_execution_owners', None),
        )
        for window in closed:
            exit_through[str(window)] = models[window].last
    mark = models[SLEEVES[0]].close
    if any(item is not None for item in positions.values()) or any(follows.values()):
        unexplained(positions, D(snapshot['btc']), mark)
    elif mark is not None and D(snapshot['btc']) * mark >= MIN_NOTIONAL:
        raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
    cursor, accounted = _cursor_after(trades, accounted, cursor)
    _stash(state, positions, follows, accounted, exit_through, cursor)
    return positions, follows, accounted, exit_through, cursor


def _stored(state: State):
    staged = getattr(state, '_staged', None)
    if staged is not None:
        return staged
    positions = {window: (state.get('positions') or {}).get(str(window)) for window in SLEEVES}
    follows = {window: (state.get('follows') or {}).get(str(window)) for window in SLEEVES}
    accounted = set(state.get('accounted_ids') or [])
    exit_through = dict(state.get('exit_through') or {})
    return positions, follows, accounted, exit_through, state.get('trade_cursor_ms')


def _stash(state, positions, follows, accounted, exit_through, cursor):
    state._staged = (positions, follows, accounted, exit_through, cursor)


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
    trades = list(venue.trades(min(starts)))
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


def _commit(state, models, positions, follows, accounted, exit_through, fresh, last):
    _positions, _follows, accounted, exit_through, cursor = _stored(state)
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
    state.set_many(values)
    state._staged = None


def _follow_after(decision: dict, models: dict, positions: dict, follows: dict, exit_through: dict) -> dict:
    """Remember a previewed entry until its fill is recorded or the signal is gone.

    A sleeve that exited on the current completed bar does not arm again until
    a newer bar. That is the same-day rule the meter already uses.
    """
    out = {}
    for window, model in models.items():
        sleeve = decision['sleeves'].get(str(window), {})
        follow = follows.get(window)
        blocked = exit_through.get(str(window))
        if positions[window] is not None:
            out[window] = None
        elif blocked is not None and model.last is not None and model.last <= int(blocked):
            out[window] = None
        elif sleeve.get('action') == 'enter' and model.last is not None:
            if follow and follow.get('signal_ms') is not None:
                out[window] = {'signal_ms': follow['signal_ms'], 'repair': bool(follow.get('repair'))}
            else:
                out[window] = {'signal_ms': model.last, 'repair': bool(model.cap_enter)}
        elif not (model.enter or model.cap_enter):
            out[window] = None
        else:
            out[window] = follow
    return out


def _view(model: Model, position):
    if position is None:
        return model, D(0)
    view = Model.restore(model.checkpoint())
    view.entry = D(position['entry_fill'])
    view.position_peak = D(position['peak'])
    view.repair = bool(position['repair'])
    view.repair_peak = None if position['repair_peak'] is None else D(position['repair_peak'])
    view.adverse = bool(position['adverse'])
    view.protection = position.get('protection', 'resting')
    return view, D(position['qty'])


def _public_snapshot(snapshot: dict) -> dict:
    """Balances for the report. Filter multipliers stay on the preview, not here."""
    return {
        'account_uid': snapshot.get('account_uid'),
        'btc': snapshot.get('btc'),
        'usdt_free': snapshot.get('usdt_free'),
        'usdt_locked': snapshot.get('usdt_locked'),
        'open_orders': snapshot.get('open_orders'),
        'orders': list(snapshot.get('orders') or []),
        'environment': snapshot.get('environment'),
    }
