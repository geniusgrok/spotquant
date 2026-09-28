"""Causal daily BTCUSDT spot regime. Long or cash. No leverage and no short.

A completed UTC daily close above its simple moving average is bullish.
Entry needs two consecutive bullish closes, a fresh cross since the last
exit, and a close no more than half below the 252-day highest close.
A close at least 60% above that average is a blow-off: the account sells
the next open. A reversal after an 8% down day, while the close is still
at least half under the 400-day highest close, may enter on the next open
and keeps the 28% stop until the close is back above the average and within
20% of that 400-day high. Any other open position sells the next open when
a completed close finishes 4% or more under its entry fill. The account
may buy only on a later open.
Protection is a stop 28% under the running high. That is wider than Binance
spot trailingDelta (2000 bips), so the session would amend a STOP_LOSS
price. The economic meter walks each daily range high-before-low.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal as D
import hashlib
import json

from .types import Blocked, number

# 2019-01-01T00:00:00Z. Warmup for the 252-day high is inside this history.
ORIGIN = 1546300800000
DAY = 86_400_000
# Full-sample search. The selected book keeps the SMA 40 / confirm 2 / fresh /
# crash 0.50 / trail 0.28 core, sells a close 60% above that average, may enter
# the day after a crash-reversal that is still 50% under the 400-day high, and
# sells a non-repair position the next open after a close 4% under the fill.
SMA_WINDOW = 40
TRAIL = D('0.28')
CONFIRM = 2
CRASH = D('0.50')
HIGH_WINDOW = 252
FRESH = True
EXTEND = D('0.60')
CAP_DROP = D('0.08')
CAP_BOUNCE = D('0.06')
CAP_DEPTH = D('0.50')
CAP_HAND = D('0.20')
CAP_WINDOW = 400
ADVERSE = D('0.04')
VERSION = 3


class Model:
    def __init__(self, sma_window: int = SMA_WINDOW, trail: D | str = TRAIL,
                 confirm: int = CONFIRM, crash: D | str = CRASH,
                 high_window: int = HIGH_WINDOW, fresh: bool = FRESH,
                 extend: D | str = EXTEND, cap_drop: D | str = CAP_DROP,
                 cap_bounce: D | str = CAP_BOUNCE, cap_depth: D | str = CAP_DEPTH,
                 cap_hand: D | str = CAP_HAND, cap_window: int = CAP_WINDOW,
                 adverse_stop: D | str = ADVERSE):
        if type(sma_window) is not int or sma_window < 2 or sma_window > 400:
            raise Blocked('SMA window out of range')
        if type(confirm) is not int or not 1 <= confirm <= 20:
            raise Blocked('confirmation out of range')
        if type(high_window) is not int or not 2 <= high_window <= 400:
            raise Blocked('high window out of range')
        if type(cap_window) is not int or not 4 <= cap_window <= 500:
            raise Blocked('capitulation window out of range')
        if type(fresh) is not bool:
            raise Blocked('fresh flag must be a boolean')
        trail, crash = D(trail), D(crash)
        extend, cap_drop = D(extend), D(cap_drop)
        cap_bounce, cap_depth, cap_hand = D(cap_bounce), D(cap_depth), D(cap_hand)
        adverse_stop = D(adverse_stop)
        if not trail.is_finite() or not D('0.02') <= trail <= D('0.50'):
            raise Blocked('trail out of range')
        if not crash.is_finite() or not D(0) <= crash < D(1):
            raise Blocked('crash filter out of range')
        if not extend.is_finite() or not D(0) <= extend <= D('2'):
            raise Blocked('extension out of range')
        if not cap_drop.is_finite() or not D(0) <= cap_drop < D(1):
            raise Blocked('capitulation drop out of range')
        if not cap_bounce.is_finite() or not D(0) <= cap_bounce < D(1):
            raise Blocked('capitulation bounce out of range')
        if not cap_depth.is_finite() or not D(0) <= cap_depth < D(1):
            raise Blocked('capitulation depth out of range')
        if not cap_hand.is_finite() or not D(0) <= cap_hand < D(1):
            raise Blocked('capitulation handoff out of range')
        if not adverse_stop.is_finite() or not D(0) <= adverse_stop <= D('0.20'):
            raise Blocked('adverse stop out of range')
        self.sma_window = sma_window
        self.trail = trail
        self.confirm = confirm
        self.crash = crash
        self.high_window = high_window
        self.fresh = fresh
        self.extend = extend
        self.cap_drop = cap_drop
        self.cap_bounce = cap_bounce
        self.cap_depth = cap_depth
        self.cap_hand = cap_hand
        self.cap_window = cap_window
        self.adverse_stop = adverse_stop
        self.closes: deque[D] = deque(maxlen=max(sma_window, high_window, cap_window))
        self.last: int | None = None
        self.close: D | None = None
        self.prev_close: D | None = None
        self.older_close: D | None = None
        self.sma: D | None = None
        self.bull = False
        self.enter = False
        self.extended = False
        self.cap_enter = False
        self.repair = False
        self.adverse = False
        self.entry: D | None = None
        self.streak = 0
        self.need_reset = False
        self.crash_ok = crash == 0
        self.peak: D | None = None
        self.repair_peak: D | None = None

    def note_exit(self) -> None:
        """A fill closed the position. The next entry waits for a fresh cross."""
        self.need_reset = True
        self.repair = False
        self.repair_peak = None
        self.entry = None
        self.adverse = False

    def note_entry(self, price) -> None:
        """Record the fill. A later close 4% under it exits a non-repair position."""
        self.entry = number(price, 'entry', positive=True)
        self.adverse = False

    def note_cap_entry(self) -> None:
        """This fill is a crash reversal. Hold the stop until the handoff close."""
        self.repair = True
        self.adverse = False

    def update(self, open_time: int, high, low, close) -> bool:
        """Consume one completed daily bar. Returns whether the close is bullish."""
        if type(open_time) is not int:
            raise Blocked('daily open time must be an integer millisecond timestamp')
        if self.last is None:
            if open_time != ORIGIN:
                raise Blocked('history must start at the fixed 2019-01-01 origin or a matching checkpoint')
        elif open_time != self.last + DAY:
            raise Blocked('missing or duplicate daily bar')
        high, low, close = number(high, 'high'), number(low, 'low'), number(close, 'close')
        if not (high.is_finite() and low.is_finite() and close.is_finite()):
            raise Blocked('nonfinite daily bar')
        if not D(0) < low <= close <= high:
            raise Blocked('invalid daily bar')
        self.older_close = self.prev_close
        self.prev_close = self.close
        self.closes.append(close)
        self.last = open_time
        self.close = close
        if len(self.closes) < self.sma_window:
            self.sma = None
            self.bull = False
        else:
            window = list(self.closes)[-self.sma_window:]
            self.sma = sum(window, D(0)) / self.sma_window
            self.bull = close > self.sma
        if self.bull:
            self.streak += 1
            self.peak = high if self.peak is None else max(self.peak, high)
        else:
            self.streak = 0
            self.need_reset = False
            self.peak = None
        if self.crash == 0:
            self.crash_ok = True
        elif len(self.closes) < self.high_window:
            self.crash_ok = False
        else:
            highest = max(list(self.closes)[-self.high_window:])
            self.crash_ok = close >= highest * (D(1) - self.crash)
        self.extended = bool(
            self.extend > 0 and self.sma is not None and close >= self.sma * (D(1) + self.extend)
        )
        cap_high = None
        if len(self.closes) >= self.cap_window:
            cap_high = max(list(self.closes)[-self.cap_window:])
        self.cap_enter = False
        if (self.cap_drop > 0 and self.cap_bounce > 0 and cap_high is not None
                and self.prev_close is not None and self.older_close is not None
                and self.older_close > 0 and self.prev_close > 0):
            yday = self.prev_close / self.older_close
            today = close / self.prev_close
            self.cap_enter = bool(
                yday <= D(1) - self.cap_drop
                and today >= D(1) + self.cap_bounce
                and close <= cap_high * (D(1) - self.cap_depth)
            )
        if self.repair:
            self.repair_peak = high if self.repair_peak is None else max(self.repair_peak, high)
            handed = (
                self.bull and cap_high is not None and self.cap_hand > 0
                and close >= cap_high * (D(1) - self.cap_hand)
            )
            if handed:
                self.repair = False
                self.repair_peak = None
        self.adverse = bool(
            not self.repair and self.entry is not None and self.adverse_stop > 0
            and close <= self.entry * (D(1) - self.adverse_stop)
        )
        blocked = self.fresh and self.need_reset
        self.enter = self.streak >= self.confirm and self.crash_ok and not blocked
        return self.bull

    def stop_price(self, peak_high: D) -> D:
        """Stop price from the running high. Not a trailingDelta: 28% exceeds 2000 bips."""
        peak = number(peak_high, 'peak', positive=True)
        return peak * (D(1) - self.trail)

    def checkpoint(self) -> dict:
        body = {
            'version': VERSION,
            'sma_window': self.sma_window,
            'trail': format(self.trail, 'f'),
            'confirm': self.confirm,
            'crash': format(self.crash, 'f'),
            'high_window': self.high_window,
            'fresh': self.fresh,
            'extend': format(self.extend, 'f'),
            'cap_drop': format(self.cap_drop, 'f'),
            'cap_bounce': format(self.cap_bounce, 'f'),
            'cap_depth': format(self.cap_depth, 'f'),
            'cap_hand': format(self.cap_hand, 'f'),
            'cap_window': self.cap_window,
            'adverse_stop': format(self.adverse_stop, 'f'),
            'last': self.last,
            'closes': [format(item, 'f') for item in self.closes],
            'prev_close': None if self.prev_close is None else format(self.prev_close, 'f'),
            'older_close': None if self.older_close is None else format(self.older_close, 'f'),
            'sma': None if self.sma is None else format(self.sma, 'f'),
            'bull': self.bull,
            'enter': self.enter,
            'extended': self.extended,
            'cap_enter': self.cap_enter,
            'repair': self.repair,
            'adverse': self.adverse,
            'entry': None if self.entry is None else format(self.entry, 'f'),
            'streak': self.streak,
            'need_reset': self.need_reset,
            'crash_ok': self.crash_ok,
            'peak': None if self.peak is None else format(self.peak, 'f'),
            'repair_peak': None if self.repair_peak is None else format(self.repair_peak, 'f'),
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return {'body': body, 'sha256': digest}

    @classmethod
    def restore(cls, saved: dict) -> 'Model':
        try:
            body = saved['body']
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if saved['sha256'] != digest or body['version'] != VERSION:
                raise ValueError('identity')
            model = cls(
                body['sma_window'], body['trail'], body['confirm'], body['crash'],
                body['high_window'], body['fresh'], body['extend'], body['cap_drop'],
                body['cap_bounce'], body['cap_depth'], body['cap_hand'], body['cap_window'],
                body['adverse_stop'],
            )
            model.last = body['last']
            if model.last is not None and (type(model.last) is not int or model.last < ORIGIN or (model.last - ORIGIN) % DAY):
                raise ValueError('clock')
            limit = max(model.sma_window, model.high_window, model.cap_window)
            if len(body['closes']) > limit:
                raise ValueError('closes')
            model.closes = deque((D(item) for item in body['closes']), maxlen=limit)
            if any(not item.is_finite() or item <= 0 for item in model.closes):
                raise ValueError('closes')
            model.sma = None if body['sma'] is None else D(body['sma'])
            model.bull = body['bull']
            model.enter = body['enter']
            model.extended = body['extended']
            model.cap_enter = body['cap_enter']
            model.repair = body['repair']
            model.adverse = body['adverse']
            model.entry = None if body['entry'] is None else D(body['entry'])
            model.streak = body['streak']
            model.need_reset = body['need_reset']
            model.crash_ok = body['crash_ok']
            model.peak = None if body['peak'] is None else D(body['peak'])
            model.repair_peak = None if body['repair_peak'] is None else D(body['repair_peak'])
            model.close = model.closes[-1] if model.closes else None
            model.prev_close = None if body['prev_close'] is None else D(body['prev_close'])
            model.older_close = None if body['older_close'] is None else D(body['older_close'])
            if type(model.streak) is not int or type(model.need_reset) is not bool or type(model.crash_ok) is not bool:
                raise ValueError('flags')
            if type(model.extended) is not bool or type(model.cap_enter) is not bool or type(model.repair) is not bool:
                raise ValueError('flags')
            if type(model.adverse) is not bool:
                raise ValueError('flags')
            if model.entry is not None and (not model.entry.is_finite() or model.entry <= 0):
                raise ValueError('entry')
            expected_adverse = bool(
                not model.repair and model.entry is not None and model.adverse_stop > 0
                and model.close is not None
                and model.close <= model.entry * (D(1) - model.adverse_stop)
            )
            if bool(model.adverse) != expected_adverse:
                raise ValueError('adverse flag')
            if bool(model.bull) != bool(model.sma is not None and model.close > model.sma):
                raise ValueError('bull flag')
            blocked = model.fresh and model.need_reset
            if bool(model.enter) != bool(model.streak >= model.confirm and model.crash_ok and not blocked):
                raise ValueError('entry flag')
            expected_ext = bool(
                model.extend > 0 and model.sma is not None
                and model.close >= model.sma * (D(1) + model.extend)
            )
            if bool(model.extended) != expected_ext:
                raise ValueError('extend flag')
            return model
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise Blocked('model checkpoint does not match this origin') from exc
