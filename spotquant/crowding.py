"""BTC new-entry policy and observed public inputs; no account or order access."""
from copy import deepcopy
from decimal import Decimal as D
import hashlib
import json
import time
from urllib.request import urlopen

DAY = 86400000
FUNDING_LAG = 28800000
BASIS_LAG = 60000
RULE = '2026-10-03-atr-stop-crowding-interaction-v1'
PUBLIC_URLS = {
    'funding': 'https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=10',
    'spot_bars': 'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=2',
    'futures_bars': 'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=2',
}


def value_at(name, record, now):
    """Shared availability contract; modeled lags never replace actual receipt time."""
    cause = record.get('cause')
    observed, available = record.get('observation_ms'), record.get('available_ms')
    if not cause:
        lag = FUNDING_LAG if name == 'funding' else BASIS_LAG
        if (type(now) is not int or type(observed) is not int or type(available) is not int
                or available != observed + lag or observed < 0
                or (name == 'basis' and observed % DAY)):
            cause = 'invalid_feature_timestamps'
        elif available > now or record.get('receipt_ms', 0) > now:
            cause = 'not_yet_available'
        elif name == 'funding' and now - available >= FUNDING_LAG:
            cause = 'stale_funding'
        elif name == 'basis' and now - available > DAY:
            cause = 'stale_basis'
        elif name == 'basis' and available // DAY != now // DAY:
            cause = 'basis_availability_date_mismatch'
    value = None
    if not cause:
        try:
            value = D(record['value'])
            if not value.is_finite(): raise ValueError('nonfinite')
        except (KeyError, TypeError, ValueError, ArithmeticError):
            cause = 'invalid_feature_value'
    return (None if cause else value), cause


def evaluate(source, view, now):
    """Exact measured conjunction; missing causes block only a genuine new BUY."""
    inputs, values = [], []
    for name in ('funding', 'basis'):
        value = source.value(name, now) if source is not None else None
        item = deepcopy(source.last_lookup) if source is not None else dict(
            name=name, value=None, cause='missing_feature_source', now_ms=now)
        # Both observed public and pinned historical adapters expose this contract.
        if value is not None:
            value, cause = value_at(name, item, now)
            if cause: item.update(value=None, cause=cause)
        inputs.append(item); values.append(value)
    closes = list(view.closes)
    causal = type(now) is int and view.last is not None and view.last + DAY <= now
    momentum = dict(current_completed_bar_ms=view.last, current_close=view.close,
                    prior_completed_bar_ms=view.last - 5 * DAY if len(closes) >= 6 else None,
                    prior_close=closes[-6] if len(closes) >= 6 else None, causal_completed=causal)
    cause = None
    scale = D(1)
    if any(v is None for v in values) or len(closes) < 6 or not causal:
        scale, cause = D(0), 'missing_causal_crowding_or_momentum'
    elif values[0] > D('.0003') and values[1] > D('.01') and closes[-1] <= closes[-6]:
        scale = D('.5')
    return scale, dict(mechanism='crowding-interaction', inputs=inputs, momentum=momentum,
                       scale=scale, blocked_reason=cause)


def _decimal(value):
    if isinstance(value, bool): raise ValueError('boolean market value')
    result = D(str(value))
    if not result.is_finite(): raise ValueError('nonfinite market value')
    return result


class ObservedFeatures:
    """Build only from actually received settled rates and paired completed bars.

    Each observation supplies category, URL, request/receipt clocks, raw SHA and
    parsed body. Forward retains the exact bytes; native reports their hashes.
    A failed/malformed endpoint is explicit missing input, never a zero rate.
    """
    def __init__(self, observations):
        self.observations = observations
        self.last_lookup = None
        self.filters = {'blocked': 0, 'missing': 0}

    def value(self, name, now):
        record = dict(name=name, value=None, cause='missing_public_' + name)
        try:
            selected = [r for r in self.observations if r['category'] in
                        (('funding',) if name == 'funding' else ('spot_bars', 'futures_bars'))]
            provenance = [{k: r[k] for k in ('category', 'url', 'request_ms', 'receipt_ms', 'sha256')} for r in selected]
            record.update(provenance=provenance, receipt_ms=max((r['receipt_ms'] for r in selected), default=0))
            for r in selected:
                if (type(r['request_ms']) is not int or type(r['receipt_ms']) is not int
                        or not 0 <= r['receipt_ms'] - r['request_ms'] <= 60000
                        or not 0 <= now - r['receipt_ms'] <= 60000):
                    raise ValueError('stale/future public receipt')
                if r.get('error'): raise ValueError('public_endpoint_' + r['error'])
            if name == 'funding':
                rows = {}
                for r in selected:
                    for row in r['body']:
                        stamp = row['fundingTime']; rate = _decimal(row['fundingRate'])
                        if (row['symbol'] != 'BTCUSDT' or type(stamp) is not int
                                or not 0 <= stamp <= r['receipt_ms'] or abs(rate) >= 1):
                            raise ValueError('invalid settled funding')
                        if stamp in rows and rows[stamp] != rate: raise ValueError('conflicting settled funding')
                        rows[stamp] = rate
                eligible = [t for t in rows if t + FUNDING_LAG <= now]
                if eligible:
                    stamp = max(eligible)
                    record.update(observation_ms=stamp, available_ms=stamp + FUNDING_LAG,
                                  value=str(rows[stamp]), cause=None)
            else:
                paired = {}
                boundary = now // DAY * DAY
                for r in selected:
                    seen = set()
                    for row in r['body']:
                        if not isinstance(row, list) or len(row) != 12: raise ValueError('official kline schema')
                        start, end = row[0], row[6]
                        if (type(start) is not int or type(end) is not int or start % DAY
                                or end + 1 != start + DAY or start in seen):
                            raise ValueError('invalid daily kline boundary')
                        seen.add(start)
                        if end + 1 > r['receipt_ms']: continue
                        o, h, l, c = [_decimal(v) for v in row[1:5]]
                        if not 0 < l <= min(o, c) <= max(o, c) <= h: raise ValueError('invalid OHLC')
                        if end + 1 != boundary: continue
                        key = r['category']
                        if key in paired and paired[key] != c: raise ValueError('conflicting daily close')
                        paired[key] = c
                if set(paired) == {'spot_bars', 'futures_bars'}:
                    record.update(observation_ms=boundary, available_ms=boundary + BASIS_LAG,
                                  value=str(paired['futures_bars'] / paired['spot_bars'] - 1), cause=None)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            record.update(value=None, cause='invalid_public_' + name + ':' + str(exc))
        value, cause = value_at(name, record, now)
        self.last_lookup = dict(record, now_ms=now, value=None if value is None else str(value), cause=cause)
        return value


def _read_public(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('duplicate public field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite public number')))


def public_body(raw):
    """Unavailable public JSON is a feature failure, with raw integrity checked by callers."""
    try:
        return dict(body=_read_public(raw))
    except ValueError as exc:
        return dict(body=None, error=type(exc).__name__)


class PublicFeatures(ObservedFeatures):
    """One small public snapshot per minute; no predicted premium or archive feed."""
    def __init__(self):
        super().__init__([])
        self.fetched = None

    def refresh(self, now, stopping=None):
        if self.fetched is None or now - self.fetched >= 60000:
            observations = []
            for category, url in PUBLIC_URLS.items():
                request = time.time_ns() // 1000000
                record = dict(category=category, url=url, request_ms=request)
                try:
                    if stopping is not None and stopping(): raise ValueError('session deadline')
                    with urlopen(url, timeout=5) as response:
                        if response.geturl() != url: raise ValueError('redirect')
                        raw = response.read(1000001)
                    if len(raw) > 1000000: raise ValueError('oversized response')
                    record.update(public_body(raw), sha256=hashlib.sha256(raw).hexdigest())
                except (OSError, ValueError) as exc:
                    raw = str(type(exc).__name__).encode()
                    record.update(body=None, error=type(exc).__name__, sha256=hashlib.sha256(raw).hexdigest())
                record['receipt_ms'] = time.time_ns() // 1000000
                observations.append(record)
            self.observations, self.fetched = observations, now
