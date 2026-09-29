"""One manually started read-only session. No order is submitted."""
from __future__ import annotations

import time
from decimal import Decimal as D

from .follow import advance, matched_buy, replay
from .model import DAY, Model
from .preview import BASE_STEP, MIN_NOTIONAL, preview
from .state import State
from .types import Blocked, Unknown

# Dropped when a cycle fails so the previous success cannot be read as current.
STALE_REPORT_FIELDS = ('actual', 'model_preview', 'market_through', 'model_bull', 'entries_enabled')

RECORDED_LIMITS = {
    'adverse_exit': 'next_open',
    'adverse_loss_capped': False,
    'path_convention': 'high_before_low',
    'selection': 'full_sample',
    'skip_stress_targets_met': False,
}


def clear_stale(report: dict) -> None:
    for key in STALE_REPORT_FIELDS:
        report.pop(key, None)


def cycle(venue, state: State, config) -> dict:
    model, enabled, steps = _sync_model(state, venue)
    snapshot = venue.snapshot(config.account_uid)
    if getattr(venue, 'environment', None) != config.environment:
        raise Blocked('exchange adapter and configuration differ in environment')
    if getattr(venue, 'capital_limit', None) != config.capital_limit:
        raise Blocked('exchange adapter and configuration differ in capital limit')
    position, follow = _position(state, venue, model, snapshot, steps)
    view, owned = _view(model, position)
    decision = preview(
        model if position is None else view,
        snapshot, entries_enabled=enabled, capital_limit=config.capital_limit, owned_btc=owned,
    )
    follow = _follow_after(decision, model, position, follow)
    state.set('position', position)
    state.set('follow', follow)
    through = None if model.last is None else model.last
    return {
        'status': 'read_only',
        'model_preview': decision,
        'actual': _public_snapshot(snapshot),
        'market_through': through,
        'model_bull': model.bull,
        'entries_enabled': enabled,
        'followed_position': position is not None,
        'observation_current': True,
        'recorded_limits': dict(RECORDED_LIMITS),
        'write_attempted': False,
    }


def run(config, venue, *, monotonic=time.monotonic, wait=time.sleep, stopping=lambda: False):
    """Observe until the deadline or Ctrl-C. Nothing is sent to the order API."""
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
        'stop_reason': 'deadline',
        'session_started_at_ms': int(venue.clock() * 1000),
        'recorded_limits': dict(RECORDED_LIMITS),
    }
    with State(config.state_dir, config.scope) as state:
        try:
            while monotonic() < deadline and not stopping():
                report['cycles'] += 1
                report['observation_current'] = False
                try:
                    current = cycle(venue, state, config)
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
            report['write_attempted'] = False
            state.report(report)
    return report


def _sync_model(state: State, venue):
    saved = state.get('model')
    anchor = state.get('entries_after')
    model = Model() if saved is None else Model.restore(saved)
    steps = []
    for open_ms, high, low, close in venue.completed_daily(None if saved is None else model.last):
        model.update(open_ms, high, low, close)
        steps.append({
            'open_ms': open_ms,
            'high': high,
            'close': close,
            'bull': model.bull,
            'cap_high': model._view_cap_high(),
        })
    if model.last is None:
        raise Unknown('no completed daily bar is available to anchor the model')
    if saved is None:
        state.set_many({'model': model.checkpoint(), 'entries_after': model.last})
        return model, False, steps
    state.set('model', model.checkpoint())
    enabled = anchor is not None and model.last is not None and model.last > int(anchor)
    return model, enabled, steps


def _position(state: State, venue, model: Model, snapshot: dict, steps: list):
    position = state.get('position')
    follow = state.get('follow')
    balance = D(snapshot['btc'])
    mark = model.close
    material = mark is not None and balance * mark >= MIN_NOTIONAL
    if position is not None:
        if abs(balance - D(position['qty'])) > BASE_STEP:
            if material:
                raise Unknown('BTC balance does not match the recorded spotquant fill; refusing new risk')
            position = None
            model.note_flat()
            state.set('model', model.checkpoint())
        else:
            for step in steps:
                position = advance(position, step, model)
    elif material:
        if not follow or follow.get('signal_ms') is None:
            raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
        since = int(follow['signal_ms']) + DAY
        bought = matched_buy(venue.trades(since), balance, since, mark)
        if bought is None:
            raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
        built = replay(
            venue.completed_daily(None), entry_fill=bought['entry_fill'],
            first_ms=bought['first_ms'], repair=bool(follow.get('repair')),
        )
        built['qty'] = format(bought['qty'], 'f')
        position = built
    return position, follow


def _follow_after(decision: dict, model: Model, position, follow):
    if position is not None:
        return None
    if decision['action'] == 'enter' and model.last is not None:
        if follow and follow.get('signal_ms') is not None:
            return {'signal_ms': follow['signal_ms'], 'repair': bool(follow.get('repair'))}
        return {'signal_ms': model.last, 'repair': bool(model.cap_enter)}
    if not (model.enter or model.cap_enter):
        return None
    return follow


def _view(model: Model, position):
    if position is None:
        return model, D(0)
    view = Model.restore(model.checkpoint())
    view.entry = D(position['entry_fill'])
    view.position_peak = D(position['peak'])
    view.repair = bool(position['repair'])
    view.repair_peak = None if position['repair_peak'] is None else D(position['repair_peak'])
    view.adverse = bool(position['adverse'])
    return view, D(position['qty'])


def _public_snapshot(snapshot: dict) -> dict:
    """Balances for the report. Filter multipliers stay on the preview, not here."""
    return {
        'account_uid': snapshot.get('account_uid'),
        'btc': snapshot.get('btc'),
        'usdt_free': snapshot.get('usdt_free'),
        'usdt_locked': snapshot.get('usdt_locked'),
        'open_orders': snapshot.get('open_orders'),
        'environment': snapshot.get('environment'),
    }
