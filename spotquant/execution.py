"""Durable order allocation for explicitly authorized, bounded spot sessions."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal as D
from time import time

from .model import DAY, Model, SLEEVES
from .preview import BASE_STEP, MIN_NOTIONAL, _annotate_venue, _decide, _protection, _qty_ok, decision_view
from .state import client_id
from .types import Blocked, Unknown, NotFound, NotSent, floor_step, number, serial

TERMINAL = {'FILLED', 'EXPIRED', 'CANCELED', 'REJECTED', 'EXPIRED_IN_MATCH'}
FIELDS = ('symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice')


def _no_fill_failure(row):
    """A native refusal or terminal zero-fill order is safe to repair separately."""
    _, _, status, result = row
    return (status == 'rejected' or status == 'settled'
            and result.get('status') in TERMINAL - {'FILLED'}
            and result.get('executedQty') is not None and number(result['executedQty']) == 0)


def allocation_owners(rows):
    """Attach only durable native readback to an immutable sleeve allocation."""
    return {str(result['orderId']): dict(payload, native_status=result.get('status'),
                                       native_executed_qty=result.get('executedQty'),
                                       native_quote_qty=result.get('cummulativeQuoteQty'),
                                       native_created_ms=result.get('time') or result.get('transactTime'))
            for payload, result in rows if 'orderId' in result}


class Lifecycle:
    def __init__(self, state, venue, config):
        if not getattr(venue, 'execution_authorized', False) or config.environment not in ('demo', 'live'):
            raise Blocked('execution requires an explicitly authorized venue and capital ceiling')
        if state.identity != config.scope or config.capital_limit is None:
            raise Blocked('execution requires scoped state and a positive capital ceiling')
        self.state, self.venue, self.config = state, venue, config
        self._entry_inputs = {}

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
            try:
                row = self.venue.query(prior.get('orderId') or identity)
            except NotFound:
                row = None
            if row is None:
                raise Unknown('sent order is not confirmed; stable identity is never resubmitted')
            allowed = {identity} | ({payload['cancel_id']} if payload.get('cancel_id') else set())
            if row.get('clientOrderId') not in allowed:
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
            if status == 'unknown' and payload['order']['type'] == 'STOP_LOSS' and row['status'] not in TERMINAL:
                self._confirmed_stop = identity

    def owners(self):
        return allocation_owners((payload, result) for _, payload, _, result in self.rows())

    def _protect_unsold(self, bar, positions, follows, snapshot):
        """Keep a stop on a position whose market sell cannot be sent."""
        from .session import _view
        window = SLEEVES[0]
        position = positions.get(str(window))
        if not position or position.get('dust'):
            return False
        view, quantity = _view(Model.restore(self.state.get('models')[str(window)]), position)
        order = _protection(decision_view(view, position, self.owners()), quantity, snapshot)
        if order.get('placeable') is False or 'quantity' not in order:
            return False
        if D(order['stopPrice']) >= D(snapshot['last_price']):
            return False
        order = dict(order, sleeves=[window])
        identity = self.prepare(order, bar, positions, follows)
        row = next(item for item in self.rows() if item[0] == identity)
        if row[2] in ('rejected', 'settled'):
            return False
        for stop_id, payload, status, _ in self.rows():
            if status == 'resting' and payload['order']['type'] == 'STOP_LOSS' and stop_id != identity:
                self.cancel(stop_id)
                return True
        self._allow_exit_protection = True
        try:
            return self.send(identity)
        finally:
            self._allow_exit_protection = False

    def verify(self, snapshot):
        known = {result.get('orderId'): {identity} | ({payload['cancel_id']} if payload.get('cancel_id') else set())
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
        totals = {}
        for trade in self.state.trades(self.venue, anchor['at_ms']):
            order_id = str(trade['order_id'])
            owner = owners.get(order_id)
            if owner is None:
                raise Unknown('external trade blocks execution')
            if trade['buyer'] is not (owner['order']['side'] == 'BUY'):
                raise Unknown('fill side differs from its durable order')
            gross, quote = totals.get(order_id, (D(0), D(0)))
            totals[order_id] = gross + trade['qty'], quote + trade['quote']
            commission, asset = trade['commission'], trade['commission_asset']
            if commission and not asset:
                raise Unknown('commission asset is missing')
            if commission and asset not in ('BTC', 'USDT'):
                unvalued.add(asset)
            sign = 1 if trade['buyer'] else -1
            btc += sign * trade['qty'] - (commission if asset == 'BTC' else D(0))
            cash -= sign * trade['quote'] + (commission if asset == 'USDT' else D(0))
        for order_id, owner in owners.items():
            gross, quote = totals.get(order_id, (D(0), D(0)))
            executed = number(owner['native_executed_qty'], 'native executed quantity', nonnegative=True)
            if abs(gross - executed) > D('0.00000001'):
                raise Unknown('durable fill quantity differs from native executed quantity')
            native_quote = owner.get('native_quote_qty')
            # Binance may report a negative cumulative quote for unavailable
            # historical data; native executed quantity still must reconcile.
            if native_quote is not None:
                native_quote = number(native_quote, 'native cumulative quote')
                if native_quote >= 0 and abs(quote - native_quote) > D('0.00000001'):
                    raise Unknown('durable fill quote differs from native cumulative quote')
        if abs(btc - D(snapshot['btc'])) > D('0.00000001') or abs(
                cash - D(snapshot['usdt_free']) - D(snapshot['usdt_locked'])) > D('0.00000001'):
            raise Unknown('account balances differ from durable fills; transfers are not inferred')
        if unvalued != set(self.state.get('third_asset_fees_unvalued') or []):
            self.state.set('third_asset_fees_unvalued', sorted(unvalued))

    def prepare(self, order, bar, positions, follows, *, rearm=None, retry_rejected=False):
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
        while (raw['type'] == 'STOP_LOSS' and prior is not None and prior[2] == 'settled'
               and prior[3].get('status') == 'CANCELED'):
            payload['replaced_stop'] = identity
            operation += '-again-' + str(prior[3].get('orderId'))
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
        if (retry_rejected and raw['side'] == 'SELL' and raw['type'] == 'MARKET' and prior is not None
                and _no_fill_failure(prior)
                and number(raw['quantity']) == number(prior[1]['order']['quantity'])
                and not prior[1].get('retry_after_reject')):
            payload['retry_after_reject'] = identity
            operation += '-after-reject'
            identity = client_id(self.state.identity, bar, operation)
            prior = next((row for row in self.rows() if row[0] == identity), None)
        if prior is None:
            self.save(identity, payload, 'prepared', {})
        elif (raw['type'] == 'STOP_LOSS' and prior[2] == 'settled'
              and prior[3].get('not_sent') is True and 'orderId' not in prior[3]):
            if any(prior[1].get(key) != payload.get(key) for key in (
                    'order', 'sleeves', 'weights', 'signal_ms', 'repair', 'rearm', 'position_first_ms')):
                raise Blocked('unsent protection identity belongs to a different allocation')
            self.save(identity, prior[1], 'prepared', {})
        elif (any(prior[1].get(key) != payload.get(key) for key in
                  ('order', 'sleeves', 'weights', 'signal_ms', 'repair'))
              or prior[1].get('rearm', {str(w): False for w in group}) != payload['rearm']) and prior[2] == 'prepared':
            raise Blocked('prepared identity cannot change parameters')
        elif (prior[2] == 'prepared' and raw['type'] == 'STOP_LOSS'
              and prior[1].get('position_first_ms') != payload.get('position_first_ms')):
            raise Blocked('prepared identity cannot change position ownership')
        return identity

    def send(self, identity):
        _, payload, status, _ = next(row for row in self.rows() if row[0] == identity)
        if status == 'rejected':
            raise Blocked('durable order was rejected; owner review is required')
        if status != 'prepared':
            self.recover()
            if payload['order']['type'] == 'STOP_LOSS' and status == 'settled':
                raise Unknown('desired protection is already terminal; no active coverage inferred')
            return False
        # Only the adapter's NotSent proof permits another dispatch. A later
        # -2013/open-order absence cannot undo a request sent to an async venue.
        self.save(identity, payload, 'unknown', {})
        self._retry_observation = False
        try:
            accepted = self.venue.submit(identity, payload['order'],
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
        else:
            if (not isinstance(accepted, dict) or type(accepted.get('orderId')) is not int
                    or accepted['orderId'] <= 0 or accepted.get('clientOrderId') != identity
                    or accepted.get('symbol') != payload['order']['symbol']):
                raise Unknown('order acknowledgement has no matching native identity')
            # Preserve the matching-engine identity before any further network
            # request. Readback still verifies the immutable order parameters.
            self.save(identity, payload, 'unknown', accepted)
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

    def _preflight(self, payload, snapshot):
        """The adapter calls this after its final observation, before POST."""
        self.verify(snapshot)
        if getattr(self, '_observed', None) is not None and self.account_key(snapshot) != self.account_key(self._observed):
            raise NotSent('account changed before dispatch; reconcile before any write')
        order = payload['order']
        self._record_quote(snapshot)
        positions = self.state.get('positions') or {}
        if order['side'] == 'BUY':
            from .session import _now_ms
            from .crowding import value_at
            now = _now_ms(self.venue)
            models = self.state.get('models')
            if any(Model.restore(models[str(window)]).last + 2 * DAY <= now
                   for window in payload['sleeves']):
                self._retry_observation = True
                raise NotSent('UTC day changed before entry; refresh completed daily inputs')
            if any(value_at(name, self._entry_inputs.get(name, {}), now)[0] is None
                   for name in ('funding', 'basis')):
                self._retry_observation = True
                raise NotSent('crowding inputs expired before entry; refresh public inputs')
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
            if order['type'] == 'STOP_LOSS':
                from .session import _view
                position = positions[str(SLEEVES[0])]
                if payload.get('position_first_ms', {}).get(str(SLEEVES[0])) != position['first_ms']:
                    raise NotSent('prepared protection has no matching position identity')
                view, quantity = _view(Model.restore(self.state.get('models')[str(SLEEVES[0])]), position)
                fresh = _decide(decision_view(view, position, self.owners()), snapshot,
                                entries_enabled=False, owned_btc=quantity, budget=D(0))
                if fresh['action'] == 'exit' and not getattr(self, '_allow_exit_protection', False):
                    self._retry_observation = True
                    raise NotSent('latest position decision requires an exit before protection dispatch')
            if order['type'] == 'STOP_LOSS' and D(order['stopPrice']) >= D(snapshot['last_price']):
                self._retry_observation = True
                raise NotSent('desired stop is crossed at the latest price; reconcile before dispatch')

    def act(self, decision, bar, observed):
        """One action, then the session re-observes fills before sizing any buy."""
        self._entry_inputs = {item['name']: item for diagnostic in decision.get('crowding', [])
                              for item in diagnostic.get('inputs', [])}
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
        crossed = any(item.get('action') == 'exit' and 'already crossed' in (item.get('reason') or '')
                      for item in decision['sleeves'].values())

        def dispatch_sell(identity, order):
            try:
                return self.send(identity)
            except Blocked:
                saved = next(item for item in self.rows() if item[0] == identity)
                if not _no_fill_failure(saved):
                    raise
                if not crossed:
                    return self._protect_unsold(bar, positions, follows, snapshot)
                successor = self.prepare(
                    order, bar, positions, follows,
                    rearm={str(w): decision['sleeves'].get(str(w), {}).get('rearm', False)
                           for w in order['sleeves']},
                    retry_rejected=True)
                return self.send(successor)

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
                return self._protect_unsold(bar, positions, follows, snapshot)
            for stop_id, stop, _, _ in resting:
                if set(stop['sleeves']) & set(payload['sleeves']):
                    self.cancel(stop_id)
                    return True
            return dispatch_sell(identity, dict(payload['order'], sleeves=payload['sleeves']))
        sells = [order for order in decision['orders'] if order['side'] == 'SELL']
        for order in sells:
            if not _qty_ok(order['quantity'], snapshot):
                return self._protect_unsold(bar, positions, follows, snapshot)
            identity = self.prepare(order, bar, positions, follows,
                                    rearm={str(w): decision['sleeves'].get(str(w), {}).get('rearm', False)
                                           for w in order['sleeves']},
                                    retry_rejected=crossed)
            row = next(row for row in self.rows() if row[0] == identity)
            if _no_fill_failure(row):
                if not crossed:
                    continue
                raise Blocked('durable reduction made no fill progress; owner review is required')
            if row[2] == 'settled':
                continue
            # Persist the sale before releasing any coins; restart resumes this exact intent.
            for stop_id, payload, _, _ in resting:
                if set(payload['sleeves']) & set(order['sleeves']):
                    self.cancel(stop_id)
                    return True
            return dispatch_sell(identity, order)
        if any(item['action'] == 'exit' for item in decision['sleeves'].values()):
            return self._protect_unsold(bar, positions, follows, snapshot)
        desired = [order for order in decision['protections'] if 'quantity' in order]
        # Confirm one fixed target before chasing new quotes. After confirmation,
        # observe its coverage and defer price-only replacement to the next poll.
        risk_stopped = getattr(self.venue, '_risk_stop', lambda: False)()
        fixed = [row for row in self.rows() if row[1]['order']['type'] == 'STOP_LOSS'
                 and (row[2] == 'prepared' or row[2] == 'resting' and (
                     row[0] == getattr(self, '_confirmed_stop', None) or risk_stopped))]
        for identity, payload, status, result in sorted(fixed, key=lambda row: row[2] != ('resting' if risk_stopped else 'prepared')):
            position = positions.get(str(SLEEVES[0]))
            created = result.get('time') or result.get('transactTime')
            same_position = position and (payload.get('position_first_ms', {}).get(str(SLEEVES[0])) == position['first_ms']
                or status == 'resting' and 'position_first_ms' not in payload
                and type(created) is int and created >= position['first_ms'])
            if (not position or position.get('dust')
                    or D(payload['order']['quantity']) != floor_step(D(position['qty']), BASE_STEP)
                    or not same_position):
                continue
            view = decision_view(views[SLEEVES[0]], position, self.owners())
            if D(payload['order']['stopPrice']) < view._stop_floor:
                continue
            order = dict(payload['order'], sleeves=payload['sleeves'])
            _annotate_venue(order, view, snapshot)
            desired = [order]
            break
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
