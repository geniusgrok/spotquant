"""Durable sleeve order allocation for the bounded Demo session."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal as D
from time import time

from .model import SLEEVES
from .preview import BASE_STEP, MIN_NOTIONAL, _protection, decision_view
from .state import client_id
from .types import Blocked, Unknown, NotSent, floor_step, number, serial

TERMINAL = {'FILLED', 'EXPIRED', 'CANCELED', 'REJECTED', 'EXPIRED_IN_MATCH'}
FIELDS = ('symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice')


def allocation_owners(rows):
    """Attach only durable native readback to an immutable sleeve allocation."""
    return {str(result['orderId']): dict(payload, native_status=result.get('status'),
                                       native_executed_qty=result.get('executedQty'))
            for payload, result in rows if 'orderId' in result}


class Lifecycle:
    def __init__(self, state, venue, config):
        if not getattr(venue, 'execution_authorized', False) or config.environment != 'demo':
            raise Blocked('execution requires an explicitly authorized Demo venue')
        if state.identity != config.scope or config.capital_limit is None:
            raise Blocked('execution requires scoped state and a positive capital ceiling')
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
        return allocation_owners((payload, result) for _, payload, _, result in self.rows())

    def verify(self, snapshot):
        known = {identity for identity, _, _, _ in self.rows()}
        if any(row.get('client_id') not in known for row in snapshot['orders']):
            raise Unknown('external open order blocks execution')
        if any(status == 'resting' and payload['order']['type'] == 'MARKET'
               for _, payload, status, _ in self.rows()):
            raise Unknown('market remainder must reach a confirmed terminal state')
        anchor = self.state.get('execution_anchor')
        if anchor is None:
            if snapshot['orders'] or D(snapshot['btc']) * D(snapshot['last_price']) >= MIN_NOTIONAL:
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
            raise Blocked('invalid sleeve allocation')
        raw = {key: order[key] for key in FIELDS if key in order}
        if raw.get('symbol') != 'BTCUSDT' or raw.get('type') not in ('MARKET', 'STOP_LOSS'):
            raise Blocked('invalid spot order')
        buying = raw.get('side') == 'BUY'
        weights = {str(w): '1' if buying else positions[str(w)]['qty'] for w in group}
        payload = serial({'order': raw, 'sleeves': group, 'weights': weights,
                          'signal_ms': bar,
                          'repair': {str(w): bool((follows.get(str(w)) or {}).get('repair')) for w in group}})
        if getattr(self, '_quote', None) is not None:
            payload['quote_reference'] = self._quote
        # One market intent per signal and allocation; stop revisions include their parameters.
        operation = raw['side'] + '-' + raw['type'] + '-' + ','.join(map(str, group))
        if raw['type'] == 'STOP_LOSS':
            operation += '-' + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]
        identity = client_id(self.state.identity, bar, operation)
        prior = next((row for row in self.rows() if row[0] == identity), None)
        if prior is None:
            self.save(identity, payload, 'prepared', {})
        elif any(prior[1].get(key) != payload.get(key) for key in
                 ('order', 'sleeves', 'weights', 'signal_ms', 'repair')) and prior[2] == 'prepared':
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
        self._quote = {'last_price': str(snapshot['last_price']), 'observed_at_ms': int(self.venue.clock() * 1000)}
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
                view = decision_view(view, position, getattr(self.state, '_execution_owners', None) or {})
                stop = _protection(view, qty, snapshot)
                if D(stop['stopPrice']) >= D(snapshot['last_price']):
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
        # Locked spot coins require confirmed cancellation before replacement;
        # the position has a protection gap until the replacement is confirmed.
        active = {row[0] for row in resting}
        if active != set(wanted):
            if any(D(order['stopPrice']) >= D(snapshot['last_price']) for order in desired):
                raise Unknown('desired stop is crossed at the last price; protection needs manual reduction')
            for identity in sorted(active - set(wanted)):
                self.cancel(identity)
            if active - set(wanted):
                refreshed = self.venue.snapshot(self.config.account_uid)
                self._quote = {'last_price': str(refreshed['last_price']),
                               'observed_at_ms': int(self.venue.clock() * 1000)}
                self.verify(refreshed)
                if key(refreshed) != key(snapshot):
                    # Expected canceled stops may differ; balances must still agree.
                    if any(refreshed[field] != snapshot[field] for field in ('btc', 'usdt_free', 'usdt_locked')):
                        raise Unknown('account changed while replacing protection')
                if any(D(order['stopPrice']) >= D(refreshed['last_price']) for order in desired):
                    raise Unknown('last price crossed after stop cancellation; unprotected coins need manual reduction')
            changed = False
            for identity in wanted:
                try:
                    changed = self.send(identity) or changed
                except Blocked:
                    saved = next(row for row in self.rows() if row[0] == identity)
                    if saved[2] != 'rejected':
                        raise
                    fresh = self.venue.snapshot(self.config.account_uid)
                    self.verify(fresh)
                    qty = floor_step(min(D(saved[1]['order']['quantity']), D(fresh['btc_free'])), BASE_STEP)
                    if qty <= 0 or qty * D(fresh['avg_price']) < D(fresh.get('min_notional') or MIN_NOTIONAL):
                        raise Unknown('stop rejected; unprotected BTC is too small for confirmed reduction')
                    reduce = dict(symbol='BTCUSDT', side='SELL', type='MARKET',
                                  quantity=str(qty), sleeves=saved[1]['sleeves'])
                    self.send(self.prepare(reduce, bar, positions, follows))
                    return True
            return changed
        for order in decision['orders']:
            if order['side'] == 'BUY':
                if getattr(self.venue, '_risk_stop', lambda: False)():
                    return False
                if snapshot.get('fee_mode') != 'base_quote' or snapshot.get('fee_rate') is None:
                    raise Blocked('buy fee mode is not confirmed as BTC/USDT')
                fee = D(snapshot['fee_rate'])
                if fee >= 1:
                    raise Blocked('buy commission leaves no protectable BTC')
                net = floor_step(D(order['quoteOrderQty']) / D(snapshot['last_price']) * (1 - fee), BASE_STEP)
                minimum = D(snapshot.get('min_notional') or MIN_NOTIONAL)
                if (net <= 0 or net * D(snapshot['avg_price']) < minimum
                        or snapshot.get('min_qty') is not None and net < D(snapshot['min_qty'])):
                    raise Blocked('estimated net buy cannot meet native protection minimum')
                identity = self.prepare(order, bar, positions, follows)
                if self.send(identity):
                    return True
        return False
