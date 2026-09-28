"""Causal daily BTCUSDT spot regime. Long or cash. No leverage and no short.

A completed UTC daily close above its simple moving average is bullish.
The account may buy only after that bar has closed. The resting protection
is a Binance spot ``trailingDelta`` of at most 2000 bips (20%). The economic
meter approximates that order by walking each daily range high-before-low.
Fills are not inferred from prices.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal as D
import hashlib
import json

from .types import Blocked, number

# 2019-01-01T00:00:00Z. SMA warmup is inside the history that starts here.
ORIGIN = 1546300800000
DAY = 86_400_000
# Grid rule (research/rebuild.py): no registered row met both targets.
# Among trails Binance spot can rest as trailingDelta (<= 0.20), SMA 40 had
# the highest terminal CNY. Wider trails scored higher in the same continuous
# path but are not placeable as trailingDelta.
SMA_WINDOW = 40
TRAIL = D('0.20')
VERSION = 1


class Model:
    def __init__(self, sma_window: int = SMA_WINDOW, trail: D | str = TRAIL):
        if type(sma_window) is not int or sma_window < 2 or sma_window > 400:
            raise Blocked('SMA window out of range')
        trail = D(trail)
        if not trail.is_finite() or not D('0.02') <= trail <= D('0.50'):
            raise Blocked('trail out of range')
        self.sma_window = sma_window
        self.trail = trail
        self.closes: deque[D] = deque(maxlen=sma_window)
        self.last: int | None = None
        self.close: D | None = None
        self.sma: D | None = None
        self.bull = False

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
            self.sma = sum(self.closes, D(0)) / self.sma_window
            self.bull = close > self.sma
        return self.bull

    def stop_price(self, peak_high: D) -> D:
        """Sell stop placed at the session, from highs through the last completed day."""
        peak = number(peak_high, 'peak', positive=True)
        return peak * (D(1) - self.trail)

    def checkpoint(self) -> dict:
        body = {
            'version': VERSION,
            'sma_window': self.sma_window,
            'trail': format(self.trail, 'f'),
            'last': self.last,
            'closes': [format(item, 'f') for item in self.closes],
            'sma': None if self.sma is None else format(self.sma, 'f'),
            'bull': self.bull,
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
            model = cls(body['sma_window'], body['trail'])
            model.last = body['last']
            if model.last is not None and (type(model.last) is not int or model.last < ORIGIN or (model.last - ORIGIN) % DAY):
                raise ValueError('clock')
            model.closes = deque((D(item) for item in body['closes']), maxlen=model.sma_window)
            if len(body['closes']) > model.sma_window or any(not item.is_finite() or item <= 0 for item in model.closes):
                raise ValueError('closes')
            model.sma = None if body['sma'] is None else D(body['sma'])
            model.bull = body['bull']
            model.close = model.closes[-1] if model.closes else None
            if bool(model.bull) != bool(model.sma is not None and model.close > model.sma):
                raise ValueError('bull flag')
            return model
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise Blocked('model checkpoint does not match this origin') from exc
