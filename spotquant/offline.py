"""Durable spot order experiment. Only the concrete in-memory venue is accepted.

This is an offline lifecycle, not a Binance executor. Simulated stop amendments
and liquidity do not establish native semantics. The public execute gate is unchanged.
"""
from __future__ import annotations

import json
from decimal import Decimal as D
from time import time

from .state import State, client_id
from .preview import MIN_NOTIONAL
from .types import Blocked, Unknown, number, serial


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


class Lifecycle:
    def __init__(self, state: State, venue: OfflineVenue):
        if type(venue) is not OfflineVenue or not state.identity.startswith('offline:BTCUSDT:spot:'):
            raise Blocked('offline lifecycle requires its in-memory venue and separate offline state')
        self.state, self.venue = state, venue
        self.recover()
        saved = state.get('offline_balances')
        actual = serial(venue.balances())
        if saved is None:
            if venue.btc or venue.orders:
                raise Unknown('new offline state cannot adopt an existing account')
            state.set('offline_balances', actual)
        elif saved != actual:
            raise Unknown('offline balances differ from reconciled fills')

    def _save(self, identity, payload, status, result):
        with self.state.db:
            self.state.db.execute('INSERT OR REPLACE INTO intents VALUES (?,?,?,?,?,?)',
                                  (identity, payload['type'], json.dumps(serial(payload)), status,
                                   json.dumps(serial(result)), time()))

    def _resolve(self, identity, payload, prior):
        row = self.venue.query(identity)
        if row is None:
            raise Unknown('saved offline send has no confirmed order; never resend it')
        if any(row.get(key) != value for key, value in payload.items()):
            raise Unknown('offline order readback differs from durable intent')
        before = prior['before']
        qty, quote = number(row['executedQty'], nonnegative=True), number(row['quote'], nonnegative=True)
        expected = {'cash': D(before['cash']), 'btc': D(before['btc'])}
        if row['type'] == 'MARKET' or row['status'] == 'FILLED':
            sign = 1 if row['side'] == 'BUY' else -1
            expected['btc'] += sign * qty
            expected['cash'] -= sign * quote
        if serial(expected) != serial(self.venue.balances()):
            raise Unknown('offline fills cannot explain balances')
        if row['status'] not in ('NEW', 'FILLED', 'EXPIRED', 'CANCELED') or (row['type'] == 'MARKET' and row['status'] == 'NEW'):
            raise Unknown('offline entry remainder is unresolved')
        status = 'resting' if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW' else 'settled'
        with self.state.db:
            self.state.db.execute('UPDATE intents SET status=?,result=?,updated=? WHERE id=?',
                                  (status, json.dumps(serial({'before': before, 'order': row})), time(), identity))
            self.state.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                                  ('offline_balances', json.dumps(serial(expected))))
        return row

    def recover(self):
        rows = list(self.state.db.execute(
            "SELECT id,payload,status,result FROM intents WHERE status IN ('unknown','resting') ORDER BY updated"))
        for identity, payload, _status, result in rows:
            self._resolve(identity, json.loads(payload), json.loads(result))

    def cancel_stop(self, identity):
        saved = self.state.db.execute('SELECT payload,result FROM intents WHERE id=?', (identity,)).fetchone()
        if saved is None:
            raise Unknown('offline cancel cannot adopt an external protection')
        payload, prior = json.loads(saved[0]), json.loads(saved[1])
        if payload['type'] != 'STOP_LOSS':
            raise Blocked('only confirmed offline protection can be canceled')
        self._save(identity, payload, 'unknown', prior)
        try:
            self.venue.cancel(identity)
        except Unknown:
            pass
        return self._resolve(identity, payload, prior)

    def submit(self, payload, bar):
        if type(bar) is not int or bar < 0:
            raise Blocked('invalid offline signal time')
        payload = {key: value for key, value in payload.items() if key in (
            'symbol', 'side', 'type', 'quoteOrderQty', 'quantity', 'stopPrice')}
        if payload.get('symbol') != 'BTCUSDT' or payload.get('side') not in ('BUY', 'SELL'):
            raise Blocked('invalid offline spot order')
        kind, side = payload.get('type'), payload['side']
        if kind not in ('MARKET', 'STOP_LOSS') or (kind == 'STOP_LOSS' and side != 'SELL'):
            raise Blocked('invalid offline spot order kind')
        key = 'quoteOrderQty' if side == 'BUY' else 'quantity'
        expected_keys = {'symbol', 'side', 'type', key} | ({'stopPrice'} if kind == 'STOP_LOSS' else set())
        if set(payload) != expected_keys:
            raise Blocked('conflicting offline order fields')
        number(payload.get(key), positive=True)
        if kind == 'STOP_LOSS':
            number(payload.get('stopPrice'), positive=True)
            if D(payload['quantity']) > self.venue.btc or D(payload['stopPrice']) >= self.venue.price:
                raise Blocked('offline protection is not below the current price or exceeds holdings')
        identity = client_id(self.state.identity, bar, f'{side}-{kind}')
        saved = self.state.db.execute('SELECT payload,status,result FROM intents WHERE id=?', (identity,)).fetchone()
        if saved:
            if json.loads(saved[0]) != serial(payload):
                raise Blocked('same durable identity cannot change its order')
            if saved[1] == 'settled':
                return json.loads(saved[2])['order']
            return self._resolve(identity, payload, json.loads(saved[2]))
        self.recover()
        if serial(self.venue.balances()) != self.state.get('offline_balances'):
            raise Unknown('unknown account change blocks new order')
        if ((side == 'BUY' and D(payload['quoteOrderQty']) > self.venue.cash)
                or (side == 'SELL' and D(payload['quantity']) > self.venue.btc)):
            raise Blocked('order exceeds reconciled available funds')
        protections = [row['id'] for row in self.venue.orders.values()
                       if row['type'] == 'STOP_LOSS' and row['status'] == 'NEW']
        if protections and side == 'SELL' and kind == 'MARKET':
            # Spot sales need released coins. Cancellation and sale are not atomic.
            for stop_id in protections:
                self.cancel_stop(stop_id)
        elif protections:
            raise Blocked('resting protection blocks unsupported replacement or additional order')
        if side == 'BUY' and self.venue.btc * self.venue.price >= MIN_NOTIONAL:
            raise Blocked('unprotected holdings block additional offline exposure')
        if ((side == 'BUY' and D(payload['quoteOrderQty']) > self.venue.cash)
                or (side == 'SELL' and D(payload['quantity']) > self.venue.btc)):
            raise Blocked('order exceeds reconciled available funds')
        result = self._send(identity, payload)
        if protections and kind == 'MARKET' and side == 'SELL' and self.venue.btc * self.venue.price >= MIN_NOTIONAL:
            old = self.venue.orders[protections[0]]
            if D(old['stopPrice']) >= self.venue.price:
                raise Unknown('remaining offline position has a crossed stop; further reduction is unresolved')
            remaining = {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS',
                         'quantity': str(self.venue.btc), 'stopPrice': old['stopPrice']}
            remaining_id = client_id(self.state.identity, bar, 'remaining-' + protections[0])
            self._send(remaining_id, remaining)
        return result

    def _send(self, identity, payload):
        before = serial(self.venue.balances())
        self._save(identity, payload, 'unknown', {'before': before})
        try:
            self.venue.submit(identity, payload)
        except Unknown:
            pass
        return self._resolve(identity, payload, {'before': before})
