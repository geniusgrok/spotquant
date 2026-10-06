"""Small in-memory account for real session/order recovery checks; never contacts a venue."""
from decimal import Decimal as D

from spotquant.crowding import DAY, FUNDING_LAG, BASIS_LAG
from spotquant.types import Blocked, Unknown


class KnownFeatures:
    def value(self, name, now):
        observed = now - FUNDING_LAG if name == 'funding' else now // DAY * DAY
        self.last_lookup = dict(name=name, observation_ms=observed,
                                available_ms=observed + (FUNDING_LAG if name == 'funding' else BASIS_LAG),
                                value='.0001', cause=None)
        return D('.0001')


class TestVenue:
    execution_authorized = True
    environment = 'demo'

    def __init__(self, bars):
        self.cash, self.btc, self.price = D(1000), D(0), bars[-1][3]
        self.capital_limit, self.uid = D(1000), '1'
        self.bars, self.fills, self.sent, self.orders = list(bars), [], [], {}
        self.now_ms = bars[-1][0] + DAY
        self.fraction, self.fee = D(1), D('.001')
        self.lose_ack = self.unknown_query = self.reject_stop = self.reject_stop_known = False

    def clock(self):
        return self.now_ms / 1000

    monotonic = clock

    def wait(self, seconds):
        self.now_ms += int(seconds * 1000)

    def completed_daily(self, after):
        return [bar for bar in self.bars if after is None or bar[0] > after]

    def trades(self, since):
        return [trade for trade in self.fills if trade['time'] >= since]

    def query(self, identity):
        if self.unknown_query:
            raise Unknown('order query unavailable')
        return self.orders.get(identity)

    def snapshot(self, expected_uid):
        from spotquant.binance import _order
        if expected_uid != self.uid:
            raise Blocked('UID mismatch')
        if self.unknown_query:
            raise Unknown('balances unavailable')
        active = [row for row in self.orders.values() if row['status'] == 'NEW']
        locked = sum((D(row['quantity']) for row in active), D(0))
        return dict(account_uid=self.uid, environment=self.environment, btc=self.btc,
                    btc_free=self.btc - locked, btc_locked=locked, usdt_free=self.cash,
                    usdt_locked=D(0), avg_price=self.price, last_price=self.price,
                    min_notional=D('5'), fee_mode='base_quote', fee_rate=self.fee, can_trade=True,
                    open_orders=len(active), orders=[_order(dict(row, origQty=row.get('quantity', '0')))
                                                    for row in active])

    def _fill(self, row, quantity, quote):
        buy = row['side'] == 'BUY'
        self.fills.append(dict(id=len(self.fills) + 1, order_id=row['orderId'], time=self.now_ms,
                               qty=quantity, quote=quote, price=self.price, buyer=buy,
                               commission=quantity * self.fee if buy else quote * self.fee,
                               commission_asset='BTC' if buy else 'USDT'))

    def submit(self, identity, payload):
        if identity in self.orders:
            raise Blocked('duplicate submission')
        locked = sum((D(row['quantity']) for row in self.orders.values() if row['status'] == 'NEW'), D(0))
        self.sent.append(identity)
        if payload['type'] == 'STOP_LOSS':
            if self.reject_stop:
                raise Unknown('stop rejected without confirmed response')
            if self.reject_stop_known:
                raise Blocked('native filter rejected stop before matching')
            if D(payload['quantity']) > self.btc - locked:
                raise Blocked('coins are locked by protection')
            row = dict(payload, status='NEW', executedQty='0', quote='0')
        else:
            buy = payload['side'] == 'BUY'
            quantity = D(payload['quoteOrderQty']) * self.fraction / self.price if buy else D(payload['quantity']) * self.fraction
            quote = quantity * self.price
            if (buy and quote > self.cash) or (not buy and quantity > self.btc - locked):
                raise Blocked('insufficient account funds')
            self.btc += quantity * (1 - self.fee) if buy else -quantity
            self.cash += -quote if buy else quote * (1 - self.fee)
            row = dict(payload, status='FILLED' if self.fraction == 1 else 'EXPIRED',
                       executedQty=str(quantity * (1 - self.fee) if buy else quantity), quote=str(quote))
        row.update(id=identity, clientOrderId=identity, orderId=len(self.orders) + 1)
        self.orders[identity] = row
        if payload['type'] == 'MARKET':
            self._fill(row, quantity, quote)
        if self.lose_ack:
            self.lose_ack = False
            raise Unknown('acknowledgement lost')
        return row

    def cancel(self, identity):
        row = self.query(identity)
        if row is None:
            raise Unknown('cancel has no confirmed order')
        if row['status'] == 'NEW':
            row['status'] = 'CANCELED'
        return row

    def trigger(self, price):
        self.price = D(price)
        for row in self.orders.values():
            if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW' and self.price <= D(row['stopPrice']):
                quantity = D(row['quantity'])
                quote = quantity * self.price
                self.btc -= quantity
                self.cash += quote * (1 - self.fee)
                row.update(status='FILLED', executedQty=str(quantity), quote=str(quote))
                self._fill(row, quantity, quote)
