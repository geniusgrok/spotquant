"""Two fixed participation hypotheses; research only until full adoption gates pass."""
import copy
import hashlib
import json
from contextlib import contextmanager
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from research import account, edge_spot as edge
from spotquant.model import DAY, Model
from spotquant.types import Blocked

CANDIDATES = ('trend-reentry', 'slow-participation')
SPEC = Path(__file__).with_name('upgrade-spec.json')


def model_for(candidate):
    if candidate not in ('baseline', *CANDIDATES):
        raise ValueError('unregistered participation candidate')

    class ParticipationModel(Model):
        def __init__(self, sma_window=40, *args, **kwargs):
            if candidate == 'slow-participation' and sma_window == 50:
                sma_window = 120
            super().__init__(sma_window, *args, **kwargs)
            self.last_exit_day = None

        def note_exit(self):
            super().note_exit()
            self.last_exit_day = self.last

        def note_flat(self):
            was_held = self.entry is not None or self.position_peak is not None
            super().note_flat()
            if was_held:
                self.last_exit_day = self.last

        def update(self, stamp, high, low, close):
            result = super().update(stamp, high, low, close)
            values = list(self.closes)
            applicable = candidate == 'trend-reentry' or (
                candidate == 'slow-participation' and self.sma_window == 120)
            if (applicable and self.last_exit_day is not None
                    and stamp >= self.last_exit_day + 7 * DAY
                    and self.bull and self.streak >= 2 and self.crash_ok
                    and not self.extended and not self.cap_enter
                    and (candidate != 'trend-reentry' or
                         len(values) >= 6 and values[-1] > max(values[-6:-1]))):
                self.need_reset = False
                self.enter = True
            return result

        def checkpoint(self):
            saved = super().checkpoint()
            # A fill can reset need_reset between completed bars. Serialize the
            # derived flag required by Model.restore without changing decisions.
            saved['body']['enter'] = bool(self.streak >= self.confirm and self.crash_ok
                                         and not (self.fresh and self.need_reset))
            saved['body']['participation'] = {
                'candidate': candidate, 'last_exit_day': self.last_exit_day}
            saved['sha256'] = hashlib.sha256(json.dumps(saved['body'], sort_keys=True).encode()).hexdigest()
            return saved

        @classmethod
        def restore(cls, saved):
            try:
                extra = saved['body']['participation']
                stamp = extra['last_exit_day']
                if (extra['candidate'] != candidate or
                        stamp is not None and (type(stamp) is not int or stamp % DAY or
                                               saved['body']['last'] is None or stamp > saved['body']['last'])):
                    raise ValueError('participation checkpoint')
                result = Model.restore.__func__(cls, saved)
                result.last_exit_day = stamp
                return result
            except (KeyError, TypeError, ValueError) as exc:
                raise Blocked('unbound participation checkpoint') from exc

    return ParticipationModel


class Policy(edge.Policy):
    def __init__(self, candidate, venue, features, *, risk=None):
        if candidate not in CANDIDATES:
            raise ValueError('unregistered candidate')
        super().__init__('crowding-interaction', venue, features, risk=risk)
        self.candidate = candidate
        self.identity.update(candidate=candidate,
                             spec_sha256=hashlib.sha256(SPEC.read_bytes()).hexdigest())


@contextmanager
def configured(policy):
    with patch.object(edge, 'Model', model_for(policy.candidate)), edge.configured(policy):
        yield


def measure(candidate, scenario, bars, starts, fx, features, *, risk=None):
    """Keep original finite sessions, ownership, ATR floors and audit machinery."""
    policies = []
    def create(unused, venue, unused_features):
        value = Policy(candidate, venue, features, risk=risk)
        policies.append(value)
        return value
    with patch.object(edge.complete, 'Policy', create), patch.object(edge.complete, 'configured', configured):
        result = edge.complete.measure(candidate, scenario, bars, starts, fx, {})
    policy = policies[0]
    result.update(research_identity=policy.identity, opportunity_ledger=policy.journal,
                  risk_calibration=policy.risk)
    return result


def screen(candidate, bars, fx, features):
    """Cheap daily proxy, solely a rejection filter; never financial acceptance."""
    base = model_for(candidate)
    class DailyProxy(base):
        def update(self, stamp, high, low, close):
            result = super().update(stamp, high, low, close)
            if self.atr14 is not None:
                self.trail = min(D('.30'), max(D('.10'), 4*self.atr14/self.close))
            from spotquant.crowding import evaluate
            factor, _ = evaluate(features, self, stamp + DAY + 60000)
            if factor == 0:
                self.enter = self.cap_enter = False
            return result
    with patch.object(account, 'Model', DailyProxy):
        return account.simulate_sleeves(bars, fx,
            start_ms=edge.complete.START_MS, end_ms=edge.complete.END_MS, cold_start=True)
