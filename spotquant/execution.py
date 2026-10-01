"""Durable P4 order allocation. Used by the bounded session and offline replay."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal as D
from time import time

from .model import SLEEVES
from .preview import MIN_NOTIONAL, _protection
from .state import client_id
from .types import Blocked, Unknown, NotSent, number, serial

TERMINAL = {'FILLED', 'EXPIRED', 'CANCELED', 'REJECTED', 'EXPIRED_IN_MATCH'}
FIELDS = ('symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice')


class Lifecycle:
    def __init__(self, state, venue, config):
        if not getattr(venue, 'execution_authorized', False) or config.environment != 'demo':
            raise Blocked('P4 execution requires an explicitly authorized Demo or offline venue')
        if state.identity != config.scope or config.capital_limit is None:
            raise Blocked('P4 execution requires scoped state and a positive capital ceiling')
        self.state, self.venue, self.config = state, venue, config

    def rows(self):
        if getattr(self, '_rows', None) is None:
            self._rows = [(identity, json.loads(payload), status, json.loads(result))
                          for identity, payload, status, result in self.state.db.execute(
                              "SELECT id,payload,status,result FROM intents WHERE kind='p4' ORDER BY updated,id")]
        return self._rows

    def save(self, identity, payload, status, result):
        encoded = (json.dumps(serial(payload)), status, json.dumps(serial(result)))
        prior = self.state.db.execute('SELECT payload,status,result FROM intents WHERE id=?', (identity,)).fetchone()
        if prior == encoded:
            return
        self._rows = None
        with self.state.db:
            self.state.db.execute('INSERT OR REPLACE INTO intents VALUES (?,?,?,?,?,?)',
                                  (identity, 'p4', *encoded, time()))

    def recover(self):
        for identity, payload, status, prior in self.rows():
            if status in ('prepared', 'rejected', 'settled'):
                continue
            row = self.venue.query(identity)
            if row is None:
                raise Unknown('sent order is not confirmed; stable identity is never resubmitted')
            if row.get('clientOrderId') != identity:
                raise Unknown('native order identity differs from its durable intent')
            for key, value in payload['order'].items():
                actual = row.get(key)
                if key in ('quantity', 'quoteOrderQty', 'stopPrice'):
                    if number(actual) != number(value):
                        raise Unknown('order readback differs from durable parameters')
                elif actual != value:
                    raise Unknown('order readback differs from durable parameters')
            if row.get('status') not in TERMINAL | {'NEW', 'PARTIALLY_FILLED'}:
                raise Unknown('unrecognized native order state')
            if type(row.get('orderId')) is not int or row['orderId'] <= 0:
                raise Unknown('native order identity missing')
            self.save(identity, payload, 'settled' if row['status'] in TERMINAL else 'resting', row)

    def owners(self):
        return {str(result['orderId']): payload for _, payload, _, result in self.rows()
                if 'orderId' in result}

    def verify(self, snapshot):
        known = {identity for identity, _, _, _ in self.rows()}
        if any(row.get('client_id') not in known for row in snapshot['orders']):
            raise Unknown('external open order blocks execution')
        if any(status == 'resting' and payload['order']['type'] == 'MARKET'
               for _, payload, status, _ in self.rows()):
            raise Unknown('market remainder must reach a confirmed terminal state')
        anchor = self.state.get('execution_anchor')
        if anchor is None:
            if snapshot['orders'] or D(snapshot['btc']) * D(snapshot['avg_price']) >= MIN_NOTIONAL:
                raise Unknown('fresh execution state cannot adopt holdings or orders')
            anchor = serial({'cash': D(snapshot['usdt_free']) + D(snapshot['usdt_locked']),
                             'btc': D(snapshot['btc']), 'at_ms': int(self.venue.clock() * 1000)})
            self.state.set('execution_anchor', anchor)
        cash, btc = D(anchor['cash']), D(anchor['btc'])
        owners = self.owners()
        for trade in self.state.trades(self.venue, anchor['at_ms']):
            if str(trade['order_id']) not in owners:
                raise Unknown('external trade blocks execution')
            commission, asset = trade['commission'], trade['commission_asset']
            if commission and asset not in ('BTC', 'USDT'):
                raise Unknown('third-asset commission cannot be reconciled')
            sign = 1 if trade['buyer'] else -1
            btc += sign * trade['qty'] - (commission if asset == 'BTC' else D(0))
            cash -= sign * trade['quote'] + (commission if asset == 'USDT' else D(0))
        if abs(btc - D(snapshot['btc'])) > D('0.00000001') or abs(
                cash - D(snapshot['usdt_free']) - D(snapshot['usdt_locked'])) > D('0.00000001'):
            raise Unknown('account balances differ from durable fills; transfers are not inferred')

    def prepare(self, order, bar, positions, follows):
        group = sorted(order['sleeves'])
        if not group or any(window not in SLEEVES for window in group):
            raise Blocked('invalid P4 sleeve allocation')
        raw = {key: order[key] for key in FIELDS if key in order}
        if raw.get('symbol') != 'BTCUSDT' or raw.get('type') not in ('MARKET', 'STOP_LOSS'):
            raise Blocked('invalid P4 order')
        buying = raw.get('side') == 'BUY'
        weights = {str(w): '1' if buying else positions[str(w)]['qty'] for w in group}
        payload = serial({'order': raw, 'sleeves': group, 'weights': weights,
                          'signal_ms': bar,
                          'repair': {str(w): bool((follows.get(str(w)) or {}).get('repair')) for w in group}})
        # One market intent per signal and allocation; stop revisions include their parameters.
        operation = raw['side'] + '-' + raw['type'] + '-' + ','.join(map(str, group))
        if raw['type'] == 'STOP_LOSS':
            operation += '-' + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]
        identity = client_id(self.state.identity, bar, operation)
        prior = next((row for row in self.rows() if row[0] == identity), None)
        if prior is None:
            self.save(identity, payload, 'prepared', {})
        elif prior[1] != payload and prior[2] == 'prepared':
            raise Blocked('prepared identity cannot change parameters')
        return identity

    def send(self, identity):
        saved = next(row for row in self.rows() if row[0] == identity)
        _, payload, status, result = saved
        if status == 'rejected':
            raise Blocked('durable order was rejected; owner review is required')
        if status != 'prepared':
            self.recover()
            if payload['order']['type'] == 'STOP_LOSS' and status == 'settled':
                raise Unknown('desired protection is already terminal; no active coverage inferred')
            return False
        self.save(identity, payload, 'unknown', result)
        try:
            self.venue.submit(identity, payload['order'])
        except NotSent:
            self.save(identity, payload, 'prepared', {'not_sent': True})
            raise
        except Unknown:
            pass
        except Blocked:
            # The adapter uses Blocked only for an explicit preflight/native refusal.
            self.save(identity, payload, 'rejected', {})
            raise
        self.recover()
        return True

    def cancel(self, identity):
        saved = next(row for row in self.rows() if row[0] == identity)
        _, payload, status, result = saved
        if status == 'settled':
            return
        self.save(identity, payload, 'canceling', result)
        try:
            self.venue.cancel(identity)
        except Unknown:
            pass
        self.recover()
        row = next(row for row in self.rows() if row[0] == identity)
        if row[2] != 'settled':
            raise Unknown('protection cancellation is not confirmed')

    def act(self, decision, bar, observed):
        """One action, then the session re-observes fills before sizing any buy."""
        snapshot = self.venue.snapshot(self.config.account_uid)
        self.verify(snapshot)
        def key(row):
            return (number(row['btc']), number(row['usdt_free']), number(row['usdt_locked']),
                    json.dumps(sorted(row['orders'], key=lambda order: order['order_id']), sort_keys=True))
        if key(snapshot) != key(observed):
            raise Unknown('account changed after decision; reconcile before any write')
        positions = self.state.get('positions') or {}
        follows = self.state.get('follows') or {}
        resting = [row for row in self.rows() if row[2] == 'resting']
        for identity, payload, status, _ in self.rows():
            if status != 'prepared' or payload['order']['type'] != 'MARKET':
                continue
            if payload['order']['side'] == 'BUY':
                if payload.get('signal_ms') != bar:
                    self.save(identity, payload, 'settled', {'not_sent': True, 'reason': 'entry signal expired'})
                continue
            held = sum((D((positions.get(str(w)) or {}).get('qty', '0')) for w in payload['sleeves']), D(0))
            if not held:
                self.save(identity, payload, 'settled', {'not_sent': True, 'reason': 'owned fills already closed sleeves'})
                continue
            if number(payload['order']['quantity']) > held:
                raise Unknown('prepared reduction exceeds reconciled holdings; original identity is retained')
            for stop_id, stop, _, _ in resting:
                if set(stop['sleeves']) & set(payload['sleeves']):
                    self.cancel(stop_id)
            return self.send(identity)
        sells = [order for order in decision['orders'] if order['side'] == 'SELL']
        for order in sells:
            identity = self.prepare(order, bar, positions, follows)
            row = next(row for row in self.rows() if row[0] == identity)
            if row[2] in ('settled', 'rejected'):
                continue
            # Persist the sale before releasing any coins; restart resumes this exact intent.
            for stop_id, payload, _, _ in resting:
                if set(payload['sleeves']) & set(order['sleeves']):
                    self.cancel(stop_id)
            return self.send(identity)
        desired = [order for order in decision['protections'] if 'quantity' in order]
        # A terminal partial sale leaves a position. Preserve a stop until a later
        # signal can issue another sale; this signal's stable market ID is consumed.
        from .session import _view
        from .model import Model
        for window in SLEEVES:
            position = positions.get(str(window))
            if position and decision['sleeves'][str(window)]['action'] == 'exit':
                view, qty = _view(Model.restore(self.state.get('models')[str(window)]), position)
                stop = _protection(view, qty, snapshot)
                if D(stop['stopPrice']) >= D(snapshot['avg_price']):
                    raise Unknown('partial exit remainder has a crossed stop; explicit reduction needed')
                desired.append(dict(stop, sleeves=[window]))
        wanted = []
        for order in desired:
            if order.get('placeable') is False:
                raise Blocked('desired protection fails venue filters')
            raw = {key: order[key] for key in FIELDS if key in order}
            matching = [row for row in self.rows() if row[2] in ('resting', 'prepared') and row[1]['order'] == raw
                        and row[1]['sleeves'] == sorted(order['sleeves'])]
            wanted.append(matching[0][0] if matching else self.prepare(order, bar, positions, follows))
        for identity, payload, status, _ in self.rows():
            if status == 'prepared' and payload['order']['type'] == 'STOP_LOSS' and identity not in wanted:
                self.save(identity, payload, 'settled', {'not_sent': True, 'reason': 'protection preparation superseded'})
        # Locked spot coins require confirmed cancellation before replacement. This gap
        # is measured explicitly; native Demo qualification must verify its behavior.
        active = {row[0] for row in resting}
        if active != set(wanted):
            for identity in sorted(active - set(wanted)):
                self.cancel(identity)
            changed = False
            for identity in wanted:
                changed = self.send(identity) or changed
            return changed
        for order in decision['orders']:
            if order['side'] == 'BUY':
                identity = self.prepare(order, bar, positions, follows)
                if self.send(identity):
                    return True
        return False
