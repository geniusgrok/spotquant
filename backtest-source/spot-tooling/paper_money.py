"""Private archived fill/account primitives; no archived strategy or Lifecycle."""
from decimal import Decimal as D
from spotquant.preview import MIN_NOTIONAL
from spotquant.types import Blocked,Unknown,number,serial

class OfflineVenue:
    def __init__(self, cash='1000', price='100'):
        self.cash, self.btc = number(cash, nonnegative=True), D(0)
        self.price = number(price, positive=True)
        self.orders, self.sent, self.events = {}, [], []
        self.fraction = D(1)
        self.lose_ack = self.unknown_query = self.reject_stop = False
        self.fee = D('.001')

    def balances(self):
        if self.unknown_query:
            raise Unknown('offline balances unavailable')
        return {'cash': self.cash, 'btc': self.btc}

    def query(self, identity):
        if self.unknown_query:
            raise Unknown('offline order query unavailable')
        return self.orders.get(identity)

    def submit(self, identity, payload):
        if identity in self.orders:
            raise Blocked('duplicate submission in offline venue')
        self.sent.append(identity)
        side, kind = payload['side'], payload['type']
        if kind == 'STOP_LOSS':
            if self.reject_stop:
                raise Unknown('offline stop rejected without a confirmed response')
            row = dict(payload, id=identity, status='NEW', executedQty='0', quote='0')
        else:
            # Partial market fills are terminal here; no resting entry remainder.
            if side == 'BUY':
                spent = D(payload['quoteOrderQty']) * self.fraction
                if spent > self.cash:
                    raise Blocked('offline cash is insufficient')
                qty = spent * (1 - self.fee) / self.price
                self.cash -= spent
                self.btc += qty
            else:
                qty = D(payload['quantity']) * self.fraction
                if qty > self.btc:
                    raise Blocked('offline BTC is insufficient')
                spent = qty * self.price * (1 - self.fee)
                self.btc -= qty
                self.cash += spent
            row = dict(payload, id=identity, status='FILLED' if self.fraction == 1 else 'EXPIRED',
                       executedQty=str(qty), quote=str(spent))
        self.orders[identity] = row
        self.events.append({'event': 'accepted', 'order': dict(row), 'balances': serial(self.balances())})
        if self.lose_ack:
            self.lose_ack = False
            raise Unknown('offline acknowledgement lost')
        return row

    def trigger(self, price):
        """Venue protection runs without the lifecycle process; IOC remainder is absent."""
        self.price = number(price, positive=True)
        for row in self.orders.values():
            if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW' and self.price <= D(row['stopPrice']):
                qty = D(row['quantity'])
                if qty > self.btc:
                    raise Unknown('offline stop no longer matches holdings')
                quote = qty * self.price * (1 - self.fee)
                self.btc -= qty
                self.cash += quote
                row.update(status='FILLED', executedQty=str(qty), quote=str(quote))
                self.events.append({'event': 'stop_triggered', 'order': dict(row), 'balances': serial(self.balances())})

    def cancel(self, identity):
        row = self.query(identity)
        if row is None:
            raise Unknown('offline cancel has no confirmed original order')
        if row['status'] == 'NEW':
            row['status'] = 'CANCELED'
            self.events.append({'event': 'canceled', 'order': dict(row)})
        if self.lose_ack:
            self.lose_ack = False
            raise Unknown('offline cancel acknowledgement lost')
        return row

class P4Venue(OfflineVenue):
    """Binance-shaped offline adapter for the actual session and P4 lifecycle."""
    offline = execution_authorized = True
    environment = 'demo'

    def __init__(self, bars, *, cash='1000', capital_limit='1000', uid='1'):
        super().__init__(cash=cash, price=str(bars[-1][3]))
        self.bars = list(bars)
        self.capital_limit, self.uid = D(capital_limit), uid
        self.now_ms = bars[-1][0] + 86_400_000
        self.fills = []

    def clock(self):
        return self.now_ms / 1000

    def monotonic(self):
        return self.clock()

    def wait(self, seconds):
        self.now_ms += int(seconds * 1000)

    def completed_daily(self, after):
        return [bar for bar in self.bars if after is None or bar[0] > after]

    def trades(self, since):
        return [trade for trade in self.fills if trade['time'] >= since]

    def snapshot(self, expected_uid):
        from spotquant.binance import _order
        if expected_uid != self.uid:
            raise Blocked('offline UID mismatch')
        self.balances()
        active = [row for row in self.orders.values() if row['status'] == 'NEW']
        locked = sum((D(row['quantity']) for row in active), D(0))
        return {'account_uid': self.uid, 'environment': self.environment, 'btc': self.btc,
                'btc_free': self.btc - locked, 'btc_locked': locked, 'usdt_free': self.cash,
                'usdt_locked': D(0), 'avg_price': self.price, 'can_trade': True,
                'open_orders': len(active), 'orders': [_order(self.native(row)) for row in active]}

    def native(self, row):
        return dict(row, clientOrderId=row['id'], orderId=row['orderId'],
                    origQty=row.get('quantity', '0'))

    def query(self, identity):
        row = super().query(identity)
        return row

    def _fill(self, row, before):
        from spotquant.follow import normalize_trade
        delta = self.btc - before
        if not delta:
            return
        buy = row['side'] == 'BUY'
        gross = D(row['quote']) / self.price if buy else -delta
        self.fills.append(normalize_trade({'id': len(self.fills) + 1, 'orderId': row['orderId'],
                                          'time': self.now_ms, 'qty': str(gross),
                                          'quoteQty': str(gross * self.price), 'price': str(self.price),
                                          'commission': str(gross - delta if buy else gross * self.price * self.fee),
                                          'commissionAsset': 'BTC' if buy else 'USDT', 'isBuyer': buy}))

    def submit(self, identity, payload):
        before = self.btc
        locked = sum((D(row['quantity']) for row in self.orders.values()
                      if row['status'] == 'NEW'), D(0))
        if payload['side'] == 'SELL' and D(payload['quantity']) > self.btc - locked:
            raise Blocked('offline spot coins are locked by protection')
        lose_ack, self.lose_ack = self.lose_ack, False
        row = super().submit(identity, payload)
        row['orderId'] = len(self.orders)
        row['clientOrderId'] = identity
        self._fill(row, before)
        if lose_ack:
            raise Unknown('offline acknowledgement lost')
        return row

    def trigger(self, price):
        self.price = number(price, positive=True)
        for row in self.orders.values():
            if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW' and self.price <= D(row['stopPrice']):
                before = self.btc
                qty = D(row['quantity'])
                self.btc -= qty
                self.cash += qty * self.price * (1 - self.fee)
                row.update(status='FILLED', executedQty=str(qty), quote=str(qty * self.price * (1 - self.fee)))
                self._fill(row, before)
