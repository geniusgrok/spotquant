"""Durable sleeve order allocation for the bounded Demo session."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal as D
from time import time

from .model import Model, SLEEVES
from .preview import BASE_STEP, MIN_NOTIONAL, PRICE_STEP, _decide, _qty_ok
from .state import client_id
from .types import Blocked, Unknown, NotSent, floor_step, number, serial

TERMINAL = {'FILLED', 'EXPIRED', 'CANCELED', 'REJECTED', 'EXPIRED_IN_MATCH'}
FIELDS = ('symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice')


def allocation_owners(rows):
    """Attach only durable native readback to an immutable sleeve allocation."""
    return {str(result['orderId']): dict(payload, native_status=result.get('status'),
                                       native_executed_qty=result.get('executedQty'),
                                       native_created_ms=result.get('time') or result.get('transactTime'))
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
            row = self.venue.query(prior.get('orderId') or identity)
            if row is None:
                raise Unknown('sent order is not confirmed; stable identity is never resubmitted')
            if row.get('clientOrderId') not in {identity, payload.get('cancel_id')}:
                raise Unknown('native order identity differs from its durable intent')
            if prior.get('orderId') is not None and row.get('orderId') != prior['orderId']:
                raise Unknown('native order ID changed after confirmation')
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
        known = {result.get('orderId'): {identity, payload.get('cancel_id')}
                 for identity, payload, _, result in self.rows() if result.get('orderId') is not None}
        if any(row.get('client_id') not in known.get(row.get('order_id'), set())
               for row in snapshot['orders']):
            raise Unknown('external open order blocks execution')
        if any(status == 'resting' and payload['order']['type'] == 'MARKET'
               for _, payload, status, _ in self.rows()):
            raise Unknown('market remainder must reach a confirmed terminal state')
        anchor = self.state.get('execution_anchor')
        if anchor is None:
            if snapshot['orders'] or D(snapshot['btc']) * D(snapshot['last_price']) >= MIN_NOTIONAL:
                raise Unknown('fresh execution state cannot adopt holdings or orders')
            from .session import _now_ms
            anchor = serial({'cash': D(snapshot['usdt_free']) + D(snapshot['usdt_locked']),
                             'btc': D(snapshot['btc']), 'at_ms': _now_ms(self.venue)})
            self.state.set('execution_anchor', anchor)
        cash, btc = D(anchor['cash']), D(anchor['btc'])
        owners = self.owners()
        unvalued = set(self.state.get('third_asset_fees_unvalued') or [])
        for trade in self.state.trades(self.venue, anchor['at_ms']):
            if str(trade['order_id']) not in owners:
                raise Unknown('external trade blocks execution')
            commission, asset = trade['commission'], trade['commission_asset']
            if commission and not asset:
                raise Unknown('commission asset is missing')
            if commission and asset not in ('BTC', 'USDT'):
                unvalued.add(asset)
            sign = 1 if trade['buyer'] else -1
            btc += sign * trade['qty'] - (commission if asset == 'BTC' else D(0))
            cash -= sign * trade['quote'] + (commission if asset == 'USDT' else D(0))
        if abs(btc - D(snapshot['btc'])) > D('0.00000001') or abs(
                cash - D(snapshot['usdt_free']) - D(snapshot['usdt_locked'])) > D('0.00000001'):
            raise Unknown('account balances differ from durable fills; transfers are not inferred')
        if unvalued != set(self.state.get('third_asset_fees_unvalued') or []):
            self.state.set('third_asset_fees_unvalued', sorted(unvalued))

    def prepare(self, order, bar, positions, follows, *, rearm=None):
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
        payload['rearm'] = {str(w): bool(raw.get('side') == 'SELL' and raw.get('type') == 'MARKET'
                                        and (rearm or {}).get(str(w)) is True) for w in group}
        if raw['type'] == 'STOP_LOSS':
            payload['position_first_ms'] = {str(w): positions[str(w)]['first_ms'] for w in group}
        if getattr(self, '_quote', None) is not None:
            payload['quote_reference'] = self._quote
        # One market intent per signal and allocation; stop revisions include their parameters.
        operation = raw['side'] + '-' + raw['type'] + '-' + ','.join(map(str, group))
        if raw['type'] == 'STOP_LOSS':
            operation += '-' + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]
        identity = client_id(self.state.identity, bar, operation)
        prior = next((row for row in self.rows() if row[0] == identity), None)
        if (raw['side'] == 'SELL' and raw['type'] == 'MARKET' and prior is not None
                and prior[2] in ('settled', 'rejected')
                and number(raw['quantity']) < number(prior[1]['order']['quantity'])):
            payload['reduction_after'] = identity
            operation += '-remainder-' + hashlib.sha256(
                (identity + '|' + format(number(raw['quantity']), 'f')).encode()).hexdigest()[:16]
            identity = client_id(self.state.identity, bar, operation)
            prior = next((row for row in self.rows() if row[0] == identity), None)
        if prior is None:
            self.save(identity, payload, 'prepared', {})
        elif (any(prior[1].get(key) != payload.get(key) for key in
                  ('order', 'sleeves', 'weights', 'signal_ms', 'repair'))
              or prior[1].get('rearm', {str(w): False for w in group}) != payload['rearm']) and prior[2] == 'prepared':
            raise Blocked('prepared identity cannot change parameters')
        elif (prior[2] == 'prepared' and 'position_first_ms' in prior[1]
              and prior[1]['position_first_ms'] != payload.get('position_first_ms')):
            raise Blocked('prepared identity cannot change position ownership')
        return identity

    def send(self, identity):
        _, payload, status, result = next(row for row in self.rows() if row[0] == identity)
        if status == 'rejected':
            raise Blocked('durable order was rejected; owner review is required')
        if status != 'prepared':
            self.recover()
            if payload['order']['type'] == 'STOP_LOSS' and status == 'settled':
                raise Unknown('desired protection is already terminal; no active coverage inferred')
            return False
        self.save(identity, payload, 'unknown', result)
        self._retry_observation = False
        try:
            self.venue.submit(identity, payload['order'],
                              preflight=lambda snapshot: self._preflight(payload, snapshot))
        except NotSent:
            self.save(identity, payload, 'prepared', {'not_sent': True})
            if self._retry_observation:
                return True
            raise
        except Unknown:
            pass
        except Blocked:
            # Mutable preflight failures are NotSent; an explicit refusal requires review.
            self.save(identity, payload, 'rejected', {})
            raise
        self.recover()
        return True

    def cancel(self, identity):
        _, payload, status, result = next(row for row in self.rows() if row[0] == identity)
        if status == 'settled':
            return
        if type(result.get('orderId')) is not int or result['orderId'] <= 0:
            raise Unknown('protection cancellation has no confirmed native order ID')
        payload = dict(payload, cancel_id=payload.get('cancel_id') or
                       'sq-' + hashlib.sha256((identity + '|cancel').encode()).hexdigest()[:30])
        self.save(identity, payload, 'canceling', result)
        try:
            self.venue.cancel(identity, order_id=result['orderId'], cancel_id=payload['cancel_id'])
        except (Unknown, Blocked):
            pass
        self.recover()
        row = next(row for row in self.rows() if row[0] == identity)
        if row[2] != 'settled':
            raise Unknown('protection cancellation is not confirmed')

    @staticmethod
    def account_key(row):
        return (number(row['btc']), number(row['usdt_free']), number(row['usdt_locked']),
                json.dumps(sorted(row['orders'], key=lambda order: order['order_id']), sort_keys=True))

    def _record_quote(self, snapshot):
        from .session import _now_ms, _observe_quotes
        positions = self.state.get('positions') or {}
        observed = _observe_quotes(positions, snapshot, _now_ms(self.venue))
        if observed != positions:
            self.state.set('positions', observed)
        return any(observed[key] and positions.get(key)
                   and (observed[key]['peak'], observed[key]['repair_peak']) !=
                       (positions[key]['peak'], positions[key]['repair_peak']) for key in observed)

    def _preflight(self, payload, snapshot):
        """The adapter calls this after its final observation, before POST."""
        self.verify(snapshot)
        if getattr(self, '_observed', None) is not None and self.account_key(snapshot) != self.account_key(self._observed):
            raise NotSent('account changed before dispatch; reconcile before any write')
        order = payload['order']
        raised_peak = self._record_quote(snapshot)
        positions = self.state.get('positions') or {}
        if raised_peak and order['type'] == 'STOP_LOSS':
            position = positions[str(SLEEVES[0])]
            peak = position['repair_peak'] if position['repair'] and position['repair_peak'] is not None else position['peak']
            model = Model.restore(self.state.get('models')[str(SLEEVES[0])])
            if floor_step(model.stop_price(D(peak)), PRICE_STEP) > D(order['stopPrice']):
                self._retry_observation = True
                raise NotSent('observed position peak raised the stop price; recalculate protection before dispatch')
        if order['side'] == 'BUY':
            if self.state.get('third_asset_fees_unvalued'):
                raise NotSent('third-asset fees are unvalued; new buy needs owner reconciliation')
            if D(snapshot['btc']) * D(snapshot['last_price']) + D(order['quoteOrderQty']) > self.config.capital_limit:
                raise NotSent('buy exceeds whole-account capital ceiling at the latest price')
            for window in payload['sleeves']:
                position = positions.get(str(window))
                owned = D(0) if position is None or position.get('dust') else D(position['qty'])
                fresh = _decide(Model.restore(self.state.get('models')[str(window)]), snapshot,
                                entries_enabled=True, owned_btc=owned, budget=D(order['quoteOrderQty']))
                if fresh['action'] != 'enter' or fresh['repair'] != payload['repair'][str(window)]:
                    raise NotSent('buy signal changed at the latest price; entry is not dispatched')
        else:
            owned = sum((D((positions.get(str(w)) or {}).get('qty', '0')) for w in payload['sleeves']), D(0))
            if D(order['quantity']) > owned or D(order['quantity']) > D(snapshot['btc_free']):
                raise NotSent('sell exceeds the latest free and owned BTC; reconcile before dispatch')
            if order['type'] == 'STOP_LOSS' and D(order['stopPrice']) >= D(snapshot['last_price']):
                self._retry_observation = True
                raise NotSent('desired stop is crossed at the latest price; reconcile before dispatch')

    def act(self, decision, bar, observed):
        """One action, then the session re-observes fills before sizing any buy."""
        snapshot = self.venue.snapshot(self.config.account_uid)
        self._observed = snapshot
        from .session import _now_ms
        self._quote = {'last_price': str(snapshot['last_price']),
                       'observed_at_ms': snapshot.get('quote_observed_ms', _now_ms(self.venue))}
        self.verify(snapshot)
        if self.account_key(snapshot) != self.account_key(observed):
            raise Unknown('account changed after decision; reconcile before any write')
        self._record_quote(snapshot)
        positions = self.state.get('positions') or {}
        follows = self.state.get('follows') or {}
        if any(position and not position.get('dust') for position in positions.values()):
            from .session import _view
            from .preview import decision as decide
            views, owned = {}, {}
            for window in SLEEVES:
                views[window], owned[window] = _view(Model.restore(self.state.get('models')[str(window)]),
                                                    positions.get(str(window)))
            decision = decide(views, owned, snapshot, entries_enabled=False, capital_limit=self.config.capital_limit,
                              positions={w: positions.get(str(w)) for w in SLEEVES}, owners=self.owners())
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
                self.save(identity, payload, 'settled', {'not_sent': True,
                          'reason': 'owned protection fills reduced the prepared sale'})
                continue
            if not _qty_ok(payload['order']['quantity'], snapshot):
                raise Blocked('prepared reduction fails native lot filters')
            for stop_id, stop, _, _ in resting:
                if set(stop['sleeves']) & set(payload['sleeves']):
                    self.cancel(stop_id)
                    return True
            return self.send(identity)
        sells = [order for order in decision['orders'] if order['side'] == 'SELL']
        for order in sells:
            if not _qty_ok(order['quantity'], snapshot):
                raise Blocked('desired reduction fails native lot filters')
            identity = self.prepare(order, bar, positions, follows,
                                    rearm={str(w): decision['sleeves'].get(str(w), {}).get('rearm', False)
                                           for w in order['sleeves']})
            row = next(row for row in self.rows() if row[0] == identity)
            if row[2] == 'rejected':
                raise Blocked('durable reduction was rejected; owner review is required')
            if row[2] == 'settled':
                if row[3].get('executedQty') is not None and number(row[3]['executedQty']) == 0:
                    raise Blocked('terminal reduction made no fill progress; owner review is required')
                continue
            # Persist the sale before releasing any coins; restart resumes this exact intent.
            for stop_id, payload, _, _ in resting:
                if set(payload['sleeves']) & set(order['sleeves']):
                    self.cancel(stop_id)
                    return True
            return self.send(identity)
        if any(item['action'] == 'exit' for item in decision['sleeves'].values()):
            raise Blocked('remaining exit is below the venue minimum; owner takeover required')
        desired = [order for order in decision['protections'] if 'quantity' in order]
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
                return True
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
                    self._observed = fresh
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
                if self.state.get('third_asset_fees_unvalued'):
                    raise Blocked('third-asset fees are unvalued; new buy needs owner reconciliation')
                if snapshot.get('fee_mode') != 'base_quote' or snapshot.get('fee_rate') is None:
                    raise Blocked('buy fee mode is not confirmed as BTC/USDT')
                fee = D(snapshot['fee_rate'])
                if fee >= 1:
                    raise Blocked('buy commission leaves no protectable BTC')
                net = floor_step(D(order['quoteOrderQty']) / D(snapshot['last_price']) * (1 - fee), BASE_STEP)
                minimum = D(snapshot.get('min_notional') or MIN_NOTIONAL)
                if net <= 0 or net * D(snapshot['avg_price']) < minimum or not _qty_ok(net, snapshot):
                    raise Blocked('estimated net buy cannot meet native protection minimum')
                identity = self.prepare(order, bar, positions, follows)
                if self.send(identity):
                    return True
        return False
