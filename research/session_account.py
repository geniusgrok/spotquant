"""Historical OHLC proxy behind the actual spot bounded session and lifecycle."""
import bisect
from decimal import Decimal as D

from spotquant.model import DAY
from spotquant.offline import P4Venue
from spotquant.types import Blocked, Unknown, NotSent


class HistoricalVenue(P4Venue):
    def __init__(self, bars, start, cash, fx, *, fee=D('.001'), slip=D('.0005'),
                 stop_slip=D('.001'), uid='1'):
        completed = [(t, h, l, c) for t, o, h, l, c, q in bars if t + DAY <= start]
        super().__init__(completed, cash=str(cash), capital_limit='5000000', uid=uid)
        self.all_bars = bars
        self.starts = [row[0] for row in bars]
        self.table = {row[0]: row for row in bars}
        self.now_ms, self.fx = start, fx
        self.fee, self.slip, self.stop_slip = fee, slip, stop_slip
        self.price = self.price_at(start)
        self.initial_cash = cash
        self.curve, self.peak, self.mdd = [], cash * fx(start) / D('.999'), D(0)
        self.daily = {}
        self.read_latency_ms, self.write_latency_ms = 200, 1000
        self._stop = lambda: False
        self.client_events = []
        self._mark()

    def price_at(self, stamp):
        index = bisect.bisect_right(self.starts, stamp) - 1
        if index < 0:
            raise ValueError('historical quote lacks coverage')
        t, o, h, l, c, q = self.all_bars[index]
        offset = min(D(DAY), D(stamp - t))
        # ponytail: explicit high-before-low daily proxy, replace with verified
        # minute/print input when available; never describe it as observed LOB.
        nodes = (o, h, l, c)
        section = min(2, int(offset // (DAY // 3)))
        part = (offset - section * (DAY // 3)) / (DAY // 3)
        return nodes[section] + (nodes[section + 1] - nodes[section]) * part

    def _mark(self):
        equity = self.cash + self.btc * self.price
        value = equity * self.fx(self.now_ms) * D('.999')
        self.peak = max(self.peak, value)
        self.mdd = max(self.mdd, 1 - value / self.peak)
        self.daily[str((self.now_ms - 1) // DAY * DAY)] = {
            'timestamp_ms': self.now_ms, 'equity_usdt': str(equity), 'equity_cny': str(value),
            'cash_usdt': str(self.cash), 'btc': str(self.btc), 'price_usdt': str(self.price),
            'gross_notional_usdt': str(abs(self.btc * self.price)),
            'net_notional_usdt': str(self.btc * self.price)}

    def advance(self, target):
        if target < self.now_ms:
            raise ValueError('historical clock cannot reverse')
        while self.now_ms < target:
            day = self.now_ms // DAY * DAY
            knots = [day + DAY // 3, day + 2 * DAY // 3, day + DAY]
            end = min(target, next(t for t in knots if t > self.now_ms))
            a, b = self.price_at(self.now_ms), self.price_at(end - (1 if end % DAY == 0 else 0))
            crossed = sorted([r for r in self.orders.values() if r['type'] == 'STOP_LOSS'
                              and r['status'] == 'NEW' and b <= D(r['stopPrice'])],
                             key=lambda r: D(r['stopPrice']), reverse=True)
            for row in crossed:
                stop = D(row['stopPrice'])
                self.now_ms = end
                self.price = min(a, stop) * (1 - self.stop_slip)
                before = self.btc
                qty = D(row['quantity'])
                if qty > self.btc:
                    raise ValueError('historical protection exceeds coins')
                self.btc -= qty
                self.cash += qty * self.price * (1 - self.fee)
                row.update(status='FILLED', executedQty=str(qty),
                           quote=str(qty * self.price * (1 - self.fee)))
                self._fill(row, before)
                self._mark()
            self.now_ms = end
            self.price = self.price_at(end) if end < self.starts[-1] + DAY else self.all_bars[-1][4]
            self._mark()

    def wait(self, seconds):
        self.advance(self.now_ms + max(1, int(seconds * 1000)))

    def _read(self):
        if hasattr(self, '_stop') and self._stop():
            raise Unknown('session deadline reached')
        self.advance(self.now_ms + self.read_latency_ms)

    def completed_daily(self, after):
        self._read()
        end = bisect.bisect_right(self.starts, self.now_ms - DAY)
        begin = 0 if after is None else bisect.bisect_right(self.starts, after)
        return [(t, h, l, c) for t, o, h, l, c, q in self.all_bars[begin:end]]

    def snapshot(self, expected_uid):
        self._read()
        return super().snapshot(expected_uid)

    def trades(self, since):
        self._read()
        return super().trades(since)

    def query(self, identity):
        self._read()
        return super().query(identity)

    def submit(self, identity, payload):
        if hasattr(self, '_stop') and self._stop():
            raise NotSent('session deadline reached before dispatch')
        sent_ms = self.now_ms
        self.advance(self.now_ms + self.write_latency_ms)
        self.client_events.append({'method': 'POST', 'client_id': identity,
                                   'sent_ms': sent_ms, 'received_ms': self.now_ms,
                                   'order': dict(payload)})
        market = self.price
        if payload['type'] == 'MARKET':
            self.price *= 1 + self.slip if payload['side'] == 'BUY' else 1 - self.slip
        try:
            return super().submit(identity, payload)
        finally:
            self.price = market
            self._mark()

    def cancel(self, identity):
        if hasattr(self, '_stop') and self._stop():
            raise NotSent('session deadline reached before dispatch')
        sent_ms = self.now_ms
        self.advance(self.now_ms + self.write_latency_ms)
        self.client_events.append({'method': 'DELETE', 'client_id': identity,
                                   'sent_ms': sent_ms, 'received_ms': self.now_ms})
        row = self.orders.get(identity)
        if row is None:
            raise Unknown('cancel dispatched but original venue order is unknown')
        if row['status'] == 'NEW':
            row['status'] = 'CANCELED'
            self.events.append({'event': 'canceled', 'order': dict(row)})
        return row


def audit(venue):
    cash, btc, fees = venue.initial_cash, D(0), D(0)
    identities = set()
    for trade in venue.fills:
        if trade['id'] in identities:
            raise ValueError('duplicate historical fill')
        identities.add(trade['id'])
        sign = 1 if trade['buyer'] else -1
        btc += sign * trade['qty'] - (trade['commission'] if trade['commission_asset'] == 'BTC' else 0)
        cash -= sign * trade['quote'] + (trade['commission'] if trade['commission_asset'] == 'USDT' else 0)
        fees += trade['commission'] * trade['price'] if trade['commission_asset'] == 'BTC' else trade['commission']
    return {'passed': abs(cash - venue.cash) <= D('1e-8') and abs(btc - venue.btc) <= D('1e-8'),
            'cash_from_fills': str(cash), 'btc_from_fills': str(btc), 'fees_usdt': str(fees),
            'no_deposits': True}
