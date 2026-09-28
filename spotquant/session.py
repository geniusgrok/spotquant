"""One manually started read-only session. No order is submitted."""
from __future__ import annotations

import time

from .model import Model
from .preview import preview
from .state import State
from .types import Blocked, Unknown


def cycle(venue, state: State, config) -> dict:
    model, enabled = _sync_model(state, venue)
    snapshot = venue.snapshot(config.account_uid)
    if getattr(venue, 'environment', None) != config.environment:
        raise Blocked('exchange adapter and configuration differ in environment')
    if getattr(venue, 'capital_limit', None) != config.capital_limit:
        raise Blocked('exchange adapter and configuration differ in capital limit')
    decision = preview(
        model, snapshot, entries_enabled=enabled, capital_limit=config.capital_limit, owned_btc=0,
    )
    through = None if model.last is None else model.last
    return {
        'status': 'read_only',
        'model_preview': decision,
        'actual': snapshot,
        'market_through': through,
        'model_bull': model.bull,
        'entries_enabled': enabled,
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
                    report.pop('actual', None)
                    report.pop('model_preview', None)
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
                    report.update(status='unknown', reason='Invalid observation or state', observation_current=False)
                    report['errors'] = (report['errors'] + [{
                        'cycle': report['cycles'], 'reason': 'Invalid observation or state',
                    }])[-10:]
                    report.pop('actual', None)
                    report.pop('model_preview', None)
                report['pending_intents'] = len(state.pending())
                if report['pending_intents']:
                    report.update(status='unknown', reason='Durable intents require recovery')
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


def _sync_model(state: State, venue) -> tuple[Model, bool]:
    saved = state.get('model')
    anchor = state.get('entries_after')
    if saved is None:
        model = Model()
        for open_ms, high, low, close in venue.completed_daily(None):
            model.update(open_ms, high, low, close)
        if model.last is None:
            raise Unknown('no completed daily bar is available to anchor the model')
        state.set_many({'model': model.checkpoint(), 'entries_after': model.last})
        return model, False
    model = Model.restore(saved)
    for open_ms, high, low, close in venue.completed_daily(model.last):
        model.update(open_ms, high, low, close)
    state.set('model', model.checkpoint())
    enabled = anchor is not None and model.last is not None and model.last > int(anchor)
    return model, enabled
