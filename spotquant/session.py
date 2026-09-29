"""One manually started read-only session. No order is submitted."""
from __future__ import annotations

import time
from decimal import Decimal as D

from .follow import advance, reconcile
from .model import DAY, SLEEVES, Model
from .preview import MIN_NOTIONAL, portfolio
from .state import State
from .types import Blocked, Unknown

# Dropped when a cycle fails so the previous success cannot be read as current.
STALE_REPORT_FIELDS = (
    'actual', 'model_preview', 'market_through', 'model_bull', 'entries_enabled',
    'followed_position', 'followed_sleeves',
)

RECORDED_LIMITS = {
    'adverse_exit': 'next_open',
    'adverse_loss_capped': False,
    'path_convention': 'high_before_low',
    'selection': 'full_sample',
    'sleeves': list(SLEEVES),
    'economic_targets_met': False,
    'skip_stress_targets_met': False,
}


def clear_stale(report: dict) -> None:
    for key in STALE_REPORT_FIELDS:
        report.pop(key, None)


def cycle(venue, state: State, config) -> dict:
    models, enabled, steps = _sync_models(state, venue)
    snapshot = venue.snapshot(config.account_uid)
    if getattr(venue, 'environment', None) != config.environment:
        raise Blocked('exchange adapter and configuration differ in environment')
    if getattr(venue, 'capital_limit', None) != config.capital_limit:
        raise Blocked('exchange adapter and configuration differ in capital limit')
    positions, follows, ledger = _positions(state, venue, models, snapshot, steps)
    # The model checkpoint already moved to the new bar. Keep the positions in step with it
    # even when the preview below fails.
    state.set_many({
        'positions': {str(window): item for window, item in positions.items()},
        'follows': {str(window): item for window, item in follows.items()},
        'ledger_ms': ledger,
    })
    views = {}
    owned = {}
    for window, model in models.items():
        views[window], owned[window] = _view(model, positions[window])
    decision = portfolio(
        views, owned, snapshot, entries_enabled=enabled, capital_limit=config.capital_limit)
    follows = _follow_after(decision, models, positions, follows)
    state.set_many({
        'positions': {str(window): item for window, item in positions.items()},
        'follows': {str(window): item for window, item in follows.items()},
        'ledger_ms': ledger,
    })
    reference = models[SLEEVES[0]]
    return {
        'status': 'read_only',
        'model_preview': decision,
        'actual': _public_snapshot(snapshot),
        'market_through': reference.last,
        'model_bull': {str(window): model.bull for window, model in models.items()},
        'entries_enabled': enabled,
        'followed_position': any(item is not None for item in positions.values()),
        'followed_sleeves': [window for window, item in positions.items() if item is not None],
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
        'sleeves': list(SLEEVES),
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


def _sync_models(state: State, venue):
    saved = state.get('models')
    anchor = state.get('entries_after')
    if saved is not None and set(saved) != {str(window) for window in SLEEVES}:
        raise Blocked('state was written for other sleeves; use a new state directory')
    models = {}
    for window in SLEEVES:
        models[window] = Model(window) if saved is None else Model.restore(saved[str(window)])
        if models[window].sma_window != window:
            raise Blocked('model checkpoint does not match this sleeve')
    last = models[SLEEVES[0]].last
    if saved is not None and any(model.last != last for model in models.values()):
        raise Blocked('sleeve checkpoints are not on the same daily bar')
    steps = {window: [] for window in SLEEVES}
    for open_ms, high, low, close in venue.completed_daily(None if saved is None else last):
        for window, model in models.items():
            model.update(open_ms, high, low, close)
            steps[window].append({
                'open_ms': open_ms,
                'high': high,
                'close': close,
                'bull': model.bull,
                'cap_high': model._view_cap_high(),
            })
    last = models[SLEEVES[0]].last
    if last is None:
        raise Unknown('no completed daily bar is available to anchor the model')
    checkpoints = {str(window): model.checkpoint() for window, model in models.items()}
    if saved is None:
        state.set_many({'models': checkpoints, 'entries_after': last})
        return models, False, steps
    state.set('models', checkpoints)
    enabled = anchor is not None and last > int(anchor)
    return models, enabled, steps


def _positions(state: State, venue, models: dict, snapshot: dict, steps: dict):
    stored = state.get('positions') or {}
    pending = state.get('follows') or {}
    positions = {window: stored.get(str(window)) for window in models}
    follows = {window: pending.get(str(window)) for window in models}
    ledger = state.get('ledger_ms')
    balance = D(snapshot['btc'])
    mark = models[SLEEVES[0]].close
    for window, item in positions.items():
        if item is not None:
            for step in steps[window]:
                item = advance(item, step, models[window])
            positions[window] = item
    active = any(item is not None for item in positions.values()) or any(follows.values())
    if not active:
        if mark is not None and balance * mark >= MIN_NOTIONAL:
            raise Unknown('BTC balance has no recorded spotquant fill; refusing new risk')
        return positions, follows, ledger
    starts = [int(item['first_ms']) for item in positions.values() if item is not None]
    starts += [int(item['signal_ms']) + DAY for item in follows.values() if item and item.get('signal_ms') is not None]
    trades = venue.trades(min(starts))
    flagged = {
        window for window, item in positions.items()
        if item is not None and not item['repair']
        and (item['adverse'] or models[window].extended or not models[window].bull)
    }
    positions, follows, ledger, closed = reconcile(
        positions, follows, trades, balance, mark, ledger, flagged,
        lambda: venue.completed_daily(None),
    )
    if closed:
        for window in closed:
            models[window].note_flat()
        state.set('models', {str(window): model.checkpoint() for window, model in models.items()})
    return positions, follows, ledger


def _follow_after(decision: dict, models: dict, positions: dict, follows: dict) -> dict:
    """Remember a previewed entry until its fill is recorded or the signal is gone."""
    out = {}
    for window, model in models.items():
        sleeve = decision['sleeves'].get(str(window), {})
        follow = follows.get(window)
        if positions[window] is not None:
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
