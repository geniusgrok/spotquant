"""Two separate BTC participation expressions in the shared offline session.

Trend reentry reuses the original fixed mechanism and original screen. The
alternative permits half of an otherwise lawful new BUY when optional public
crowding observations are unavailable. Missing observations remain missing.
"""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal as D
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research import edge_spot as edge, upgrade_spot as upgrade
from spotquant import crowding, session
from spotquant.model import DAY
from spotquant.types import floor_step, serial

POLICIES = ('trend-reentry', 'optional-crowding-half')
OPTIONAL_ABSENCE = {
    'missing_feature_source', 'missing_public_funding', 'missing_public_basis',
    'not_yet_available', 'stale_funding', 'stale_basis',
    'basis_availability_date_mismatch',
}


class OptionalPolicy(edge.Policy):
    def __call__(self, views, owned, snapshot, **kwargs):
        decision = super().__call__(views, owned, snapshot, **kwargs)
        if not decision['orders']:
            decision['action'] = 'hold' if any(D(q) >= edge.preview.BASE_STEP for q in owned.values()) else 'flat'
        return decision


def optional_half(source, view, now):
    """Keep mandatory price causality and invalid observations blocked.

    This changes only a missing optional feature gate, never balances, orders,
    position ownership or protection. The original preview establishes those
    requirements before the policy evaluates a genuine fresh BUY.
    """
    factor, detail = crowding.evaluate(source, view, now)
    if factor or detail['blocked_reason'] is None:
        return factor, detail
    inputs, momentum = detail['inputs'], detail['momentum']
    if (not momentum['causal_completed'] or len(view.closes) < 6 or
            len(inputs) != 2 or {row['name'] for row in inputs} != {'funding', 'basis'}):
        return factor, detail
    missing = [row for row in inputs if row['value'] is None]
    if not missing or any(row['cause'] not in OPTIONAL_ABSENCE for row in missing):
        return factor, detail
    # An absent earlier observation is lawful optional absence. An observation
    # or actual receipt from the future is not used as a half-entry permission.
    for row in inputs:
        for key in ('observation_ms', 'available_ms', 'receipt_ms'):
            clock = row.get(key)
            if clock is not None and (type(clock) is not int or not 0 <= clock <= now):
                return factor, detail
    return D('.5'), dict(detail, scale=D('.5'), blocked_reason=None,
                         participation='optional-crowding-half',
                         original_crowding_block=detail['blocked_reason'],
                         missing_optional_names=[row['name'] for row in missing],
                         imputed_features=False)


def policy_for(expression, venue, features, binding, *, risk=None):
    """Bind one fixed expression and independent account preregistration."""
    if expression not in POLICIES or type(binding) is not dict or not binding:
        raise ValueError('registered participation expression and binding required')
    if getattr(venue, 'offline', False) is not True:
        raise ValueError('participation refuses account adapters before recovery')
    policy = (upgrade.Policy(expression, venue, features, risk=risk)
              if expression == 'trend-reentry'
              else OptionalPolicy('crowding-interaction', venue, features, risk=risk))
    policy.identity.update(participation_expression=expression,
                           participation_binding=deepcopy(binding),
                           participation_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    return policy


def _owned_close(original, policy, models, positions, follows, accounted, open_ms,
                 trades, history, owners=None):
    """Record only a confirmed allocated full rounded close, after normal fold.

    Runtime ownership lives in positions rather than Model.entry, so the old
    ParticipationModel.note_flat cannot identify these actual owned closures.
    The marker uses the same pre-bar completed-day clock as original note_exit.
    A legitimate full rounded sale may retain proven unplaceable owned dust;
    dust alone, a partial sale or an unconfirmed order never creates a marker.
    """
    result = original(models, positions, follows, accounted, open_ms, trades, history, owners=owners)
    closed = result[3]
    if owners is None or not closed:
        return result
    for window in closed:
        prior = positions.get(window)
        if prior is None or prior.get('dust'):
            continue
        for trade in trades:
            if (trade['buyer'] or trade['id'] in accounted or trade['id'] not in result[2] or
                    trade['time']//DAY*DAY != open_ms or trade['time'] < int(prior['first_ms'])):
                continue
            owner = owners.get(str(trade['order_id']))
            if owner is None or window not in owner['sleeves']:
                continue
            order = owner['order']
            full = sum((floor_step(D(owner['weights'][str(w)]), edge.preview.BASE_STEP)
                        for w in owner['sleeves']), D(0))
            if (order['side'] != 'SELL' or owner.get('native_status') != 'FILLED' or
                    owner.get('native_executed_qty') is None or
                    D(owner['native_executed_qty']) != D(order['quantity']) or
                    D(order['quantity']) != full or full <= 0):
                continue
            models[window].last_exit_day = models[window].last
            policy.journal.append(serial(dict(event='confirmed-participation-close',
                sleeve=window, order_id=trade['order_id'], fill_ms=trade['time'],
                last_exit_day=models[window].last, owned_before=prior['qty'],
                confirmed_rounded_group_quantity=full)))
            break
    return result


@contextmanager
def configured(expression, *, venue, features, binding, risk=None, journal=None):
    """Root supplies cold independent venue/state, causal FeatureBook and clocks.

    Input bars/start/origin, funding/basis artifact, cash, fees, FX, latency and
    schedule belong to the frozen account producer. No clock or state is reset.
    """
    policy = policy_for(expression, venue, features, binding, risk=risk)
    if journal is not None:
        policy.journal = journal
    context = upgrade.configured(policy) if expression == 'trend-reentry' else edge.configured(policy)
    evaluator = edge.evaluate if expression == 'trend-reentry' else optional_half
    original = session.apply_day
    def apply_day(*args, **kwargs):
        return _owned_close(original, policy, *args, **kwargs)
    closure = apply_day if expression == 'trend-reentry' else original
    with patch.object(edge, 'evaluate', evaluator), patch.object(session, 'apply_day', closure), context:
        def run(config, selected, **kwargs):
            if selected is not venue or getattr(selected, 'offline', False) is not True:
                raise ValueError('participation refuses account adapters before recovery')
            return session.run(config, selected, **kwargs)
        yield SimpleNamespace(run=run, policy=policy)
