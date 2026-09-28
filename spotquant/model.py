"""Causal daily BTCUSDT spot regime. Long or cash. No leverage and no short.

A completed UTC daily close above its simple moving average is bullish.
Entry needs two consecutive bullish closes, a fresh cross since the last
exit, and a close no more than half below the 252-day highest close.
The account may buy only on a later open. Protection is a stop 28% under
the running high. That is wider than Binance spot trailingDelta (2000 bips),
so the session would amend a STOP_LOSS price. The economic meter walks each
daily range high-before-low.
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
# Full-sample search over SMA entry/exit, confirmation, fresh-cross, and a
# 252-day crash filter. No row met both targets. SMA 40 / confirm 2 / fresh
# cross / crash 0.50 / trail 0.28 had the highest terminal CNY.
SMA_WINDOW = 40
TRAIL = D('0.28')
CONFIRM = 2
CRASH = D('0.50')
HIGH_WINDOW = 252
FRESH = True
VERSION = 2


class Model:
    def __init__(self, sma_window: int = SMA_WINDOW, trail: D | str = TRAIL,
                 confirm: int = CONFIRM, crash: D | str = CRASH,
                 high_window: int = HIGH_WINDOW, fresh: bool = FRESH):
        if type(sma_window) is not int or sma_window < 2 or sma_window > 400:
            raise Blocked('SMA window out of range')
        if type(confirm) is not int or not 1 <= confirm <= 20:
            raise Blocked('confirmation out of range')
        if type(high_window) is not int or not 2 <= high_window <= 400:
            raise Blocked('high window out of range')
        if type(fresh) is not bool:
            raise Blocked('fresh flag must be a boolean')
        trail = D(trail)
        crash = D(crash)
        if not trail.is_finite() or not D('0.02') <= trail <= D('0.50'):
            raise Blocked('trail out of range')
        if not crash.is_finite() or not D(0) <= crash < D(1):
            raise Blocked('crash filter out of range')
        self.sma_window = sma_window
        self.trail = trail
        self.confirm = confirm
        self.crash = crash
        self.high_window = high_window
        self.fresh = fresh
        self.closes: deque[D] = deque(maxlen=max(sma_window, high_window))
        self.last: int | None = None
        self.close: D | None = None
        self.sma: D | None = None
        self.bull = False
        self.enter = False
        self.streak = 0
        self.need_reset = False
        self.crash_ok = crash == 0
        self.peak: D | None = None

    def note_exit(self) -> None:
        """A fill closed the position. The next entry waits for a fresh cross."""
        self.need_reset = True

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
            highest = max(self.closes)
            self.crash_ok = close >= highest * (D(1) - self.crash)
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
            'last': self.last,
            'closes': [format(item, 'f') for item in self.closes],
            'sma': None if self.sma is None else format(self.sma, 'f'),
            'bull': self.bull,
            'enter': self.enter,
            'streak': self.streak,
            'need_reset': self.need_reset,
            'crash_ok': self.crash_ok,
            'peak': None if self.peak is None else format(self.peak, 'f'),
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
            model = cls(body['sma_window'], body['trail'], body['confirm'], body['crash'],
                        body['high_window'], body['fresh'])
            model.last = body['last']
            if model.last is not None and (type(model.last) is not int or model.last < ORIGIN or (model.last - ORIGIN) % DAY):
                raise ValueError('clock')
            limit = max(model.sma_window, model.high_window)
            if len(body['closes']) > limit:
                raise ValueError('closes')
            model.closes = deque((D(item) for item in body['closes']), maxlen=limit)
            if any(not item.is_finite() or item <= 0 for item in model.closes):
                raise ValueError('closes')
            model.sma = None if body['sma'] is None else D(body['sma'])
            model.bull = body['bull']
            model.enter = body['enter']
            model.streak = body['streak']
            model.need_reset = body['need_reset']
            model.crash_ok = body['crash_ok']
            model.peak = None if body['peak'] is None else D(body['peak'])
            model.close = model.closes[-1] if model.closes else None
            if type(model.streak) is not int or type(model.need_reset) is not bool or type(model.crash_ok) is not bool:
                raise ValueError('flags')
            if bool(model.bull) != bool(model.sma is not None and model.close > model.sma):
                raise ValueError('bull flag')
            blocked = model.fresh and model.need_reset
            if bool(model.enter) != bool(model.streak >= model.confirm and model.crash_ok and not blocked):
                raise ValueError('entry flag')
            return model
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise Blocked('model checkpoint does not match this origin') from exc
