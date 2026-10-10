"""Default read-only Binance spot BTCUSDT adapter; explicitly capped execution.

Live host ``api.binance.com``. Demo host ``demo-api.binance.com``. The API key
is sent only to that configured host. Redirects are refused. Writes in either
environment require its verified UID and a positive capital ceiling.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal as D

from .follow import normalize_trade
from .types import Blocked, Unknown, NotFound, NotSent, number

HOSTS = {
    'live': 'https://api.binance.com',
    'demo': 'https://demo-api.binance.com',
}
DAY = 86_400_000
ORIGIN = 1546300800000
RECV_WINDOW = '5000'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Blocked('refusing an HTTP redirect')


def _default_opener(method: str, url: str, headers: dict, timeout=10):
    request = urllib.request.Request(url, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())
    except Blocked as exc:
        # Redirect rejection happens after transport started. It cannot prove
        # that a POST was refused by the venue.
        raise Unknown('redirect refused after request dispatch; outcome unknown') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Unknown('Binance request failed before a usable response') from exc


class Binance:
    def __init__(self, *, key: str, secret: str, environment: str, capital_limit=None,
                 opener=None, clock=None, demo_execution_uid=None):
        if environment not in HOSTS:
            raise Blocked('environment must be live or demo')
        if not key or not secret:
            raise Blocked('Binance read credentials are empty')
        self.key = key
        self.secret = secret
        self.environment = environment
        self.capital_limit = capital_limit
        self.write_attempted = False
        if demo_execution_uid is not None and (environment not in ('demo', 'live') or capital_limit is None
                or number(capital_limit, positive=True) <= 0
                or not isinstance(demo_execution_uid, str) or not demo_execution_uid.isascii()
                or not demo_execution_uid.isdigit() or int(demo_execution_uid) <= 0):
            raise Blocked('execution needs explicit UID and capital ceiling')
        self.demo_execution_uid = demo_execution_uid
        self.execution_authorized = demo_execution_uid is not None
        self.base = HOSTS[environment]
        self._opener = opener or (lambda method, url, headers: _default_opener(
            method, url, headers, self._remaining(10)))
        self.clock = clock or time.time
        self._offset_ms = None
        self._clock_retried = False
        self._stop = None
        self._retry_after_at = 0
        self._rate_limits = {}
        self._state = None
        self.min_price = None
        self.max_price = None
        self.min_qty = None
        self.max_qty = None
        self.market_min_qty = None
        self.market_max_qty = None
        self.market_step = None
        self.max_notional = None
        self.min_notional = None
        self.percent_price_by_side = None
        self.percent_price = None

    def bind_state(self, state):
        """Keep venue backoff in the locked account directory across invocations."""
        saved = state.get('rate_limits')
        hosts = {urllib.parse.urlsplit(url).netloc for url in HOSTS.values()} | {'fapi.binance.com'}
        if saved is not None and (not isinstance(saved, dict) or any(
                host not in hosts or type(until) is not int or until < 0
                for host, until in saved.items())):
            raise Blocked('persisted rate limit state is invalid')
        self._state = state
        for host, until in (saved or {}).items():
            self._rate_limits[host] = max(self._rate_limits.get(host, 0), until)
        host = urllib.parse.urlsplit(self.base).netloc
        delay = max(0, (self._rate_limits.get(host, 0) - int(self.clock() * 1000)) / 1000)
        self._retry_after_at = max(self._retry_after_at,
                                  getattr(self, '_monotonic', time.monotonic)() + delay)

    def _remember_backoff(self, host, delay):
        until = int(self.clock() * 1000) + math.ceil(delay * 1000)
        self._rate_limits[host] = max(self._rate_limits.get(host, 0), until)
        if host == urllib.parse.urlsplit(self.base).netloc:
            self._retry_after_at = max(self._retry_after_at,
                                      getattr(self, '_monotonic', time.monotonic)() + delay)
        if self._state is not None:
            self._state.set('rate_limits', self._rate_limits)

    def crowding_features(self):
        from .crowding import PublicFeatures
        self._check_deadline()
        if not hasattr(self, '_crowding_source'):
            self._crowding_source = PublicFeatures(clock=self._timestamp,
                                                   on_rate_limit=self._remember_backoff)
        for host, until in self._rate_limits.items():
            delay = max(0, until - int(self.clock() * 1000))
            if delay:
                self._crowding_source.retry_after[host] = max(
                    self._crowding_source.retry_after.get(host, 0), self._timestamp() + delay)
        self._crowding_source.refresh(self._timestamp(),
                                      getattr(self, '_risk_stop', self._stop),
                                      lambda: self._remaining(5, risk=True))
        # Public spot data and signed spot requests share the same IP limit.
        retry_ms = self._crowding_source.retry_after.get(urllib.parse.urlsplit(self.base).netloc, 0)
        delay = max(0, (retry_ms - self._timestamp()) / 1000)
        if delay:
            self._retry_after_at = max(self._retry_after_at,
                                      getattr(self, '_monotonic', time.monotonic)() + delay)
        return self._crowding_source

    def query(self, identity):
        field = 'orderId' if type(identity) is int else 'origClientOrderId'
        row = self._get('/api/v3/order', {'symbol': 'BTCUSDT', field: identity}, signed=True)
        return self._execution_order(row)

    def submit(self, identity, payload, *, preflight=None):
        if not self.execution_authorized:
            raise Blocked('execution is not explicitly enabled')
        if not isinstance(identity, str) or not identity.startswith('sq-'):
            raise Blocked('order requires a stable Spotquant identity')
        try:
            observed = self.snapshot(self.demo_execution_uid)
        except (Unknown, Blocked) as exc:
            raise NotSent(str(exc)) from exc
        if observed['can_trade'] is not True:
            raise NotSent('account cannot trade')
        params = dict(payload, newClientOrderId=identity, newOrderRespType='FULL')
        if set(payload) - {'symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice'}:
            raise Blocked('unsupported spot order fields')
        if payload.get('symbol') != 'BTCUSDT' or payload.get('type') not in ('MARKET', 'STOP_LOSS'):
            raise Blocked('unsupported spot market or order type')
        if payload.get('side') not in ('BUY', 'SELL'):
            raise Blocked('invalid spot order side')
        buying = payload['side'] == 'BUY'
        sizing = 'quoteOrderQty' if buying else 'quantity'
        if number(payload.get(sizing), positive=True) <= 0 or ('quantity' if buying else 'quoteOrderQty') in payload:
            raise Blocked('invalid spot order sizing')
        if buying and (payload['type'] != 'MARKET' or number(payload[sizing]) > self.capital_limit):
            raise Blocked('buy exceeds its configured cash ceiling')
        from .preview import BASE_STEP, _qty_ok
        if buying:
            from .types import floor_step
            if observed['fee_mode'] != 'base_quote' or observed['fee_rate'] is None:
                raise NotSent('buy fee mode is not confirmed as BTC/USDT')
            rate = observed['fee_rate']
            if rate >= 1:
                raise NotSent('buy commission leaves no protectable BTC')
            net = floor_step(number(payload[sizing]) / observed['last_price'] * (1 - rate), BASE_STEP)
            if net * observed['avg_price'] < observed['min_notional'] or not _qty_ok(net, observed):
                raise NotSent('estimated net buy cannot meet native protection minimum')
        elif not _qty_ok(payload[sizing], observed):
            raise NotSent('sell quantity fails native lot filters')
        if payload['type'] == 'STOP_LOSS' and (buying or number(payload.get('stopPrice'), positive=True) <= 0):
            raise Blocked('only sell-side spot stop protection is supported')
        if payload['type'] == 'STOP_LOSS' and getattr(self, 'stop_price_percent_band', False):
            from .preview import stop_band_violation
            # Only after the owner confirms the filter includes stopPrice.
            violation = stop_band_violation(observed, payload.get('stopPrice'))
            if violation:
                raise NotSent(violation)
        if buying and getattr(self, '_risk_stop', lambda: False)():
            raise NotSent('entry deadline reached before order dispatch')
        if preflight is not None:
            try:
                preflight(observed)
            except (Unknown, Blocked) as exc:
                raise NotSent(str(exc)) from exc
        if buying and getattr(self, '_risk_stop', lambda: False)():
            raise NotSent('entry deadline reached before order dispatch')
        return self._execution_order(self._get('/api/v3/order', params, signed=True, method='POST'))

    def cancel(self, identity, *, order_id, cancel_id):
        if not isinstance(identity, str) or not identity.startswith('sq-'):
            raise Blocked('cancellation requires its original Spotquant identity')
        if (type(order_id) is not int or order_id <= 0
                or not isinstance(cancel_id, str) or not cancel_id.startswith('sq-')):
            raise Blocked('cancellation requires its native order and durable cancel identity')
        return self._execution_order(self._get('/api/v3/order',
            {'symbol': 'BTCUSDT', 'orderId': order_id, 'newClientOrderId': cancel_id},
            signed=True, method='DELETE'))

    @staticmethod
    def _execution_order(row):
        if not isinstance(row, dict):
            raise Unknown('native order response incomplete')
        # origQuoteOrderQty is required for quote-sized market buys; no inference from fills.
        out = dict(row)
        if 'origQty' in row:
            out['quantity'] = row['origQty']
        if 'origQuoteOrderQty' in row:
            out['quoteOrderQty'] = row['origQuoteOrderQty']
        return out

    def snapshot(self, expected_uid: str) -> dict:
        """Balances, open orders, and the UID from the account response. No order is sent."""
        self._filters()
        average = self._get('/api/v3/avgPrice', {'symbol': 'BTCUSDT'}, signed=False)
        account = self._get('/api/v3/account', signed=True)
        if not isinstance(account, dict) or 'uid' not in account or 'balances' not in account:
            raise Unknown('account response is missing uid or balances')
        if not isinstance(account['balances'], list):
            raise Unknown('account balances are not a list')
        uid = str(account['uid'])
        if not uid or uid != str(expected_uid):
            raise Blocked('exchange UID does not match account_uid')
        raw_orders = self._get('/api/v3/openOrders', {'symbol': 'BTCUSDT'}, signed=True)
        if not isinstance(raw_orders, list):
            raise Unknown('open orders response is not a list')
        orders = [_order(row) for row in raw_orders]
        if (len({row['order_id'] for row in orders}) != len(orders)
                or len({row['client_id'] for row in orders}) != len(orders)):
            raise Unknown('open orders contain repeated native identities')
        after = self._get('/api/v3/account', signed=True)
        if (not isinstance(after, dict) or str(after.get('uid')) != uid
                or after.get('balances') != account['balances']
                or after.get('canTrade') != account.get('canTrade')):
            raise Unknown('account changed during bounded spot observation')
        btc = D(0)
        btc_free = D(0)
        btc_locked = D(0)
        usdt_free = D(0)
        usdt_locked = D(0)
        bnb = D(0)
        seen = set()
        other_assets = []
        for row in account['balances']:
            if not isinstance(row, dict) or 'asset' not in row or 'free' not in row or 'locked' not in row:
                raise Unknown('account balance row is incomplete')
            asset = row['asset']
            if asset in seen:
                raise Unknown('account balance row is repeated')
            seen.add(asset)
            free = number(row['free'], asset, nonnegative=True)
            locked = number(row['locked'], asset, nonnegative=True)
            if asset == 'BNB':
                bnb = free + locked
            if asset == 'BTC':
                btc = free + locked
                btc_free, btc_locked = free, locked
            elif asset == 'USDT':
                usdt_free = free
                usdt_locked = locked
            elif free + locked > 0:
                other_assets.append(asset)
        if not {'BTC', 'USDT'} <= seen:
            raise Unknown('account response omits BTC or USDT balances')
        if not isinstance(average, dict) or 'price' not in average:
            raise Unknown('average price response is incomplete')
        fee_rate = None
        fee_mode = None
        fee_status = 'not_queried'
        if self.execution_authorized:
            try:
                commission = self._get('/api/v3/account/commission', {'symbol': 'BTCUSDT'}, signed=True)
                if not isinstance(commission, dict) or commission.get('symbol') != 'BTCUSDT':
                    raise Unknown('commission response is incomplete')
                discount = commission.get('discount')
                if (not isinstance(discount, dict) or type(discount.get('enabledForAccount')) is not bool
                        or type(discount.get('enabledForSymbol')) is not bool):
                    raise Unknown('commission discount mode is incomplete')
                fee_mode = 'third_asset' if (discount['enabledForAccount'] and discount['enabledForSymbol']) else 'base_quote'
                fee_rate = sum((number(commission[group][key], f'{group} {key}', nonnegative=True)
                                for group in ('standardCommission', 'specialCommission', 'taxCommission')
                                for key in ('taker', 'buyer')), D(0))
                fee_status = 'confirmed'
            except (Unknown, KeyError, TypeError, Blocked):
                fee_mode, fee_rate, fee_status = 'unknown', None, 'unavailable'
        # Demo keeps the BNB discount on and its UI cannot disable it. When that
        # account holds no BNB, the fill is charged in BTC or USDT. The standard
        # rate above is unchanged; the 0.75 BNB discount is not applied. Mainnet
        # and any positive BNB balance keep the refusal.
        allowance = (fee_status == 'confirmed' and fee_mode == 'third_asset' and fee_rate is not None
                     and allow_demo_bnb_discount(self.environment, self.base, bnb))
        if allowance:
            fee_mode = 'base_quote'
        self.demo_bnb_discount_allowance = allowance
        last = self._get('/api/v3/ticker/price', {'symbol': 'BTCUSDT'}, signed=False)
        if not isinstance(last, dict) or last.get('symbol') != 'BTCUSDT':
            raise Unknown('last price response is incomplete')
        return {
            'account_uid': uid,
            'btc': btc,
            'btc_free': btc_free,
            'btc_locked': btc_locked,
            'usdt_free': usdt_free,
            'usdt_locked': usdt_locked,
            'open_orders': len(orders),
            'orders': orders,
            'other_assets': other_assets,
            'can_trade': account.get('canTrade') is True,
            'environment': self.environment,
            'avg_price': number(average['price'], 'avgPrice', positive=True),
            'last_price': number(last['price'], 'last price', positive=True),
            'quote_observed_ms': self._timestamp(),
            'fee_rate': fee_rate,
            'fee_mode': fee_mode,
            'fee_status': fee_status,
            'bnb': bnb,
            'demo_bnb_discount_allowance': allowance,
            'percent_price_by_side': self.percent_price_by_side,
            'percent_price': self.percent_price,
            'min_notional': self.min_notional,
            'min_price': self.min_price,
            'max_price': self.max_price,
            'min_qty': self.min_qty,
            'max_qty': self.max_qty,
            'market_min_qty': self.market_min_qty,
            'market_max_qty': self.market_max_qty,
            'market_step': self.market_step,
            'max_notional': self.max_notional,
        }

    def trades(self, since_ms: int, from_id: int | None = None) -> list[dict]:
        """BTCUSDT fills at or after ``since_ms``. A page that does not advance is unknown.

        ``from_id`` pages with the trade id alone. Binance rejects combining it
        with ``startTime``.
        """
        if type(since_ms) is not int:
            raise Blocked('trade cursor must be an integer millisecond timestamp')
        if from_id is not None and (type(from_id) is not int or from_id <= 0):
            raise Blocked('trade id cursor must be a positive integer')
        params = ({'symbol': 'BTCUSDT', 'fromId': str(from_id), 'limit': '1000'} if from_id is not None
                  else {'symbol': 'BTCUSDT', 'startTime': str(since_ms), 'limit': '1000'})
        found = []
        seen = {}
        while True:
            payload = self._get('/api/v3/myTrades', params, signed=True)
            if not isinstance(payload, list):
                raise Unknown('trade response is not a list')
            if not payload:
                break
            ids = []
            for row in payload:
                trade = normalize_trade(row)
                if params.get('fromId') is not None and trade['id'] < int(params['fromId']):
                    raise Unknown('trade page precedes the requested id')
                ids.append(trade['id'])
                if trade['id'] in seen and trade != seen[trade['id']]:
                    raise Unknown('duplicate trade id has conflicting contents')
                if (from_id is not None or trade['time'] >= since_ms) and trade['id'] not in seen:
                    seen[trade['id']] = trade
                    found.append(trade)
            if len(payload) < 1000:
                break
            nxt = max(ids) + 1
            if params.get('fromId') is not None and nxt <= int(params['fromId']):
                raise Unknown('trade page does not advance')
            params = {'symbol': 'BTCUSDT', 'fromId': str(nxt), 'limit': '1000'}
        found.sort(key=lambda item: (item['time'], item['id']))
        return found

    def all_orders(self, since_ms: int) -> list[dict]:
        """BTCUSDT order history at or after ``since_ms``.

        A page that starts before the cursor or does not advance is unknown.
        """
        if type(since_ms) is not int:
            raise Blocked('order cursor must be an integer millisecond timestamp')
        params = {'symbol': 'BTCUSDT', 'startTime': str(since_ms), 'limit': '1000'}
        found = []
        seen = {}
        while True:
            payload = self._get('/api/v3/allOrders', params, signed=True)
            if not isinstance(payload, list):
                raise Unknown('order history response is not a list')
            if not payload:
                break
            page = []
            for row in payload:
                order = normalize_order(row)
                if params.get('orderId') is not None and order['orderId'] < int(params['orderId']):
                    raise Unknown('order page precedes the requested id')
                page.append(order)
            ids = [order['orderId'] for order in page]
            for order in page:
                prior = seen.get(order['orderId'])
                if prior is not None and prior != order:
                    raise Unknown('duplicate order id has conflicting contents')
                if order['time'] >= since_ms and order['orderId'] not in seen:
                    seen[order['orderId']] = order
                    found.append(order)
                elif order['orderId'] not in seen:
                    seen[order['orderId']] = order
            if len(payload) < 1000:
                break
            nxt = max(ids) + 1
            if params.get('orderId') is not None and nxt <= int(params['orderId']):
                raise Unknown('order page does not advance')
            params = {'symbol': 'BTCUSDT', 'orderId': str(nxt), 'limit': '1000'}
        found.sort(key=lambda item: (item['time'], item['orderId']))
        return found

    def completed_daily(self, after_open_ms: int | None) -> list[tuple[int, D, D, D, D]]:
        """Completed UTC daily bars strictly after ``after_open_ms`` (or from the origin).

        Each page must begin on the requested open and step one UTC day at a
        time. A startTime is set, so the public route returns the oldest page,
        but a page that starts later is rejected instead of being stored. A
        short or empty page that ends before the current UTC day is unknown.
        """
        if after_open_ms is None:
            cursor = ORIGIN
        elif type(after_open_ms) is not int:
            raise Blocked('daily cursor must be an integer millisecond timestamp')
        else:
            cursor = after_open_ms + DAY
        if cursor < ORIGIN or (cursor - ORIGIN) % DAY:
            raise Blocked('daily cursor is not on the UTC day grid')
        now = self._timestamp()
        today = now - (now % DAY)
        bars = []
        while cursor < today:
            payload = self._get('/api/v3/klines', {
                'symbol': 'BTCUSDT',
                'interval': '1d',
                'startTime': str(cursor),
                'endTime': str(today - 1),
                'limit': '1000',
            }, signed=False)
            if not isinstance(payload, list):
                raise Unknown('kline response is not a list')
            if not payload:
                break
            page = []
            for row in payload:
                if not isinstance(row, list) or len(row) < 5:
                    raise Unknown('kline row is incomplete')
                open_ms = _millis(row[0])
                if open_ms < cursor or open_ms >= today:
                    continue
                price, high, low, close = (number(row[index], name, positive=True)
                                           for index, name in ((1, 'open'), (2, 'high'), (3, 'low'), (4, 'close')))
                if not low <= price <= high or not low <= close <= high:
                    raise Unknown('invalid completed daily OHLC')
                page.append((open_ms, price, high, low, close))
            if not page:
                break
            if page[0][0] != cursor:
                raise Unknown('daily kline page does not start at the requested open')
            for previous, nxt in zip(page, page[1:]):
                if nxt[0] - previous[0] != DAY:
                    raise Unknown('daily kline page is not contiguous')
            bars.extend(page)
            cursor = page[-1][0] + DAY
            if len(payload) < 1000:
                break
        if cursor < today:
            raise Unknown('completed daily history stops before the current UTC day')
        return bars

    def daily_open(self, open_ms: int) -> tuple[int, D]:
        """Observe the requested arrived UTC open; unfinished HLC is unused."""
        if type(open_ms) is not int or open_ms < ORIGIN or (open_ms - ORIGIN) % DAY:
            raise Blocked('daily open is not on the UTC day grid')
        if open_ms > self._timestamp():
            raise Unknown('daily open has not arrived')
        payload = self._get('/api/v3/klines', {
            'symbol': 'BTCUSDT', 'interval': '1d', 'startTime': str(open_ms),
            'endTime': str(open_ms + DAY - 1), 'limit': '1',
        }, signed=False)
        if (not isinstance(payload, list) or len(payload) != 1
                or not isinstance(payload[0], list) or len(payload[0]) < 2
                or _millis(payload[0][0]) != open_ms):
            raise Unknown('daily open response does not match the requested day')
        return open_ms, number(payload[0][1], 'daily open', positive=True)

    def _filters(self) -> None:
        info = self._get('/api/v3/exchangeInfo', {'symbol': 'BTCUSDT'}, signed=False)
        symbols = info.get('symbols') or []
        if len(symbols) != 1 or symbols[0].get('symbol') != 'BTCUSDT':
            raise Unknown('BTCUSDT was not the only symbol in the filter response')
        symbol = symbols[0]
        if symbol.get('status') != 'TRADING' or symbol.get('isSpotTradingAllowed') is not True:
            raise Blocked('BTCUSDT spot trading is not enabled')
        if symbol.get('baseAsset') != 'BTC' or symbol.get('quoteAsset') != 'USDT':
            raise Blocked('BTCUSDT is no longer BTC quoted in USDT')
        filters = {item.get('filterType'): item for item in symbol.get('filters') or []}
        self.percent_price_by_side = _percent_filter(filters.get('PERCENT_PRICE_BY_SIDE'), by_side=True)
        self.percent_price = _percent_filter(filters.get('PERCENT_PRICE'), by_side=False)
        lot = filters.get('LOT_SIZE') or {}
        price = filters.get('PRICE_FILTER') or {}
        notional = filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL') or {}
        market = filters.get('MARKET_LOT_SIZE') or {}
        if (number(lot.get('stepSize', '0'), 'step') != D('0.00001')
                or number(price.get('tickSize', '0'), 'tick') != D('0.01')):
            raise Blocked('BTCUSDT tick or step differs from supported order sizing')
        if 'minNotional' not in notional:
            raise Blocked('BTCUSDT minimum notional is missing')
        self.min_notional = number(notional['minNotional'], 'minNotional', positive=True)
        self.min_qty = number(lot['minQty'], 'minQty', positive=True) if 'minQty' in lot else None
        self.max_qty = number(lot['maxQty'], 'maxQty', positive=True) if 'maxQty' in lot else None
        self.min_price = number(price['minPrice'], 'minPrice', positive=True) if price.get('minPrice') not in (None, '0', '0.00000000') else None
        self.max_price = number(price['maxPrice'], 'maxPrice', positive=True) if price.get('maxPrice') not in (None, '0', '0.00000000') else None
        self.market_step = number(market.get('stepSize', '0'), 'market step', nonnegative=True) or None
        self.market_min_qty = number(market.get('minQty', '0'), 'market minQty', nonnegative=True) or None
        self.market_max_qty = (number(market['maxQty'], 'market maxQty', nonnegative=True)
                               if 'maxQty' in market else None)
        self.max_notional = (number(notional['maxNotional'], 'maxNotional', positive=True)
                             if notional.get('maxNotional') not in (None, '') else None)
        types = symbol.get('orderTypes') or []
        if 'STOP_LOSS' not in types or 'MARKET' not in types:
            raise Blocked('BTCUSDT must support market orders and fixed stop protection')

    def _timestamp(self) -> int:
        if self._offset_ms is None:
            payload = self._get('/api/v3/time', signed=False)
            server = int(payload['serverTime'])
            self._offset_ms = server - int(self.clock() * 1000)
        return int(self.clock() * 1000) + self._offset_ms

    def _get(self, path: str, params: dict | None = None, *, signed: bool, method='GET'):
        if method != 'GET' and (not self.execution_authorized
                or path != '/api/v3/order' or method not in ('POST', 'DELETE') or not signed):
            raise Blocked('only explicitly enabled spot order writes are supported')
        if not path.startswith('/'):
            raise Blocked('refusing a request outside the configured Binance host')
        try:
            self._check_deadline()
        except Unknown as exc:
            if method != 'GET':
                raise NotSent(str(exc)) from exc
            raise
        pairs = [(key, str(value)) for key, value in (params or {}).items()]
        headers = {'User-Agent': 'spotquant/0.1.0'}
        if signed:
            try:
                pairs.append(('timestamp', str(self._timestamp())))
            except Unknown as exc:
                if method != 'GET':
                    raise NotSent(str(exc)) from exc
                raise
            pairs.append(('recvWindow', RECV_WINDOW))
            query = urllib.parse.urlencode(pairs)
            signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query = query + '&signature=' + signature
            headers['X-MBX-APIKEY'] = self.key
        else:
            query = urllib.parse.urlencode(pairs)
        url = self.base + path + ('?' + query if query else '')
        if not url.startswith(self.base + '/'):
            raise Blocked('refusing a request outside the configured Binance host')
        try:
            self._check_deadline()
        except Unknown as exc:
            if method != 'GET':
                raise NotSent(str(exc)) from exc
            raise
        if (method == 'POST' and (params or {}).get('side') == 'BUY'
                and getattr(self, '_risk_stop', lambda: False)()):
            raise NotSent('entry deadline reached before order dispatch')
        if method != 'GET':
            self.write_attempted = True
        opened = self._opener(method, url, headers)
        if not isinstance(opened, tuple) or len(opened) not in (2, 3):
            raise Unknown('Binance response is incomplete')
        status, body = opened[0], opened[1]
        response_headers = opened[2] if len(opened) == 3 else {}
        if status in (418, 429):
            retry = _header(response_headers, 'Retry-After')
            try:
                delay = max(1, float(number(retry, 'Retry-After', positive=True)))
                if not math.isfinite(delay * 1000):
                    raise Blocked('invalid Retry-After')
            except (Blocked, OverflowError):
                delay = 60
            self._remember_backoff(urllib.parse.urlsplit(self.base).netloc, delay)
            raise Unknown(f'Binance rate limit HTTP {status}; retry after {delay:g} seconds')
        try:
            payload = json.loads(body.decode())
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise Unknown(f'Binance {path} returned status {status} without JSON') from exc
        if status != 200:
            code = payload.get('code') if isinstance(payload, dict) else None
            if method == 'GET' and path == '/api/v3/order' and status == 400 and code == -2013:
                raise NotFound(f'Binance {path} has no order HTTP {status} code {code}')
            if method == 'GET' and signed and code == -1021 and not self._clock_retried:
                self._clock_retried = True
                self._offset_ms = None
                try:
                    return self._get(path, params, signed=signed)
                finally:
                    self._clock_retried = False
            rejected = type(code) is int and (
                code in (-1002, -1013, -1014, -1015, -1016, -1020, -1021, -1022,
                         -2010, -2011, -2014, -2015)
                or -1225 <= code <= -1100)
            if code == -2010 and 'duplicate' in str(payload.get('msg', '')).lower():
                # This attempt was refused, but the original identity may be live.
                rejected = False
            self.last_exchange_error = _exchange_error(status, payload)
            detail = _venue_message(payload)
            if method != 'GET' and 400 <= status < 500 and status not in (403, 409) and rejected:
                raise Blocked(f'Binance {path} rejected HTTP {status} code {code}{detail}')
            raise Unknown(f'Binance {path} failed with HTTP {status} code {code}{detail}')
        return payload

    def _check_deadline(self) -> None:
        if getattr(self, '_monotonic', time.monotonic)() < self._retry_after_at:
            raise Unknown('Binance rate limit backoff is still active; no request sent')
        if self._stop is not None and self._stop():
            raise Unknown('session deadline reached; not starting another request')

    def _remaining(self, ceiling, *, risk=False):
        cutoff = getattr(self, '_risk_deadline_at' if risk else '_deadline_at', None)
        clock = getattr(self, '_monotonic', None)
        if cutoff is None or clock is None:
            return ceiling
        return max(.001, min(ceiling, cutoff - clock()))


def _percent_filter(item, *, by_side: bool):
    """PERCENT_PRICE_BY_SIDE or PERCENT_PRICE, if the exchange sent one."""
    if not item:
        return None
    try:
        mins = item.get('avgPriceMins')
        if type(mins) is not int or mins < 0:
            raise Blocked('percent price filter is invalid')

        def mult(name):
            raw = item.get(name)
            if raw in (None, ''):
                return None
            return number(raw, name, positive=True)

        if by_side:
            spec = {
                'filter': 'PERCENT_PRICE_BY_SIDE',
                'ask_multiplier_down': mult('askMultiplierDown'),
                'ask_multiplier_up': mult('askMultiplierUp'),
                'bid_multiplier_down': mult('bidMultiplierDown'),
                'bid_multiplier_up': mult('bidMultiplierUp'),
                'avg_price_mins': mins,
            }
        else:
            spec = {
                'filter': 'PERCENT_PRICE',
                'ask_multiplier_down': mult('multiplierDown'),
                'ask_multiplier_up': mult('multiplierUp'),
                'bid_multiplier_down': None,
                'bid_multiplier_up': None,
                'avg_price_mins': mins,
            }
    except (Blocked, TypeError, ValueError) as exc:
        raise Unknown('percent price filter is invalid') from exc
    if spec['ask_multiplier_down'] is None or spec['ask_multiplier_up'] is None:
        raise Unknown('percent price filter is invalid')
    return spec


def _exchange_error(status, payload) -> dict:
    """HTTP status, Binance code, and the msg field as the exchange sent it."""
    msg = payload.get('msg') if isinstance(payload, dict) else None
    code = payload.get('code') if isinstance(payload, dict) else None
    return {
        'http_status': status,
        'code': code if type(code) is int else None,
        'msg': msg if isinstance(msg, str) else None,
    }


def _venue_message(payload) -> str:
    """Binance msg text, without credentials or a multi-line body."""
    if not isinstance(payload, dict) or not isinstance(payload.get('msg'), str):
        return ''
    text = ' '.join(payload['msg'].split())
    if not text or len(text) > 500:
        return ''
    lowered = text.lower()
    if 'signature' in lowered or 'api-key' in lowered or 'secret' in lowered:
        return ''
    return ' ' + text


def allow_demo_bnb_discount(environment, base, bnb) -> bool:
    """True only for the Spot Demo host when the BNB balance is exactly zero."""
    if environment != 'demo' or base != HOSTS['demo'] or bnb != 0:
        return False
    parts = urllib.parse.urlsplit(base)
    return (parts.scheme == 'https' and parts.hostname == 'demo-api.binance.com'
            and parts.port in (None, 443) and not parts.username)


def _header(headers, name: str):
    for key, value in dict(headers).items():
        if str(key).lower() == name.lower():
            return value
    return None


def normalize_order(row: dict) -> dict:
    """One allOrders or order-query row, with numeric fields checked."""
    if not isinstance(row, dict):
        raise Unknown('order history row is incomplete')
    if row.get('symbol', 'BTCUSDT') != 'BTCUSDT':
        raise Unknown('order history symbol differs from BTCUSDT')
    try:
        order_id = row['orderId']
        if type(order_id) is not int or order_id <= 0:
            raise Unknown('order history identity is invalid')
        side = row['side']
        order_type = row['type']
        status = row['status']
        client = row['clientOrderId']
        original = number(row['origQty'], 'origQty', nonnegative=True)
        executed = number(row['executedQty'], 'executedQty', nonnegative=True)
        stamp = row['time']
        if type(stamp) is not int or stamp < 0:
            raise Unknown('order history time is invalid')
    except (KeyError, TypeError, ValueError, Blocked) as exc:
        raise Unknown('order history row is incomplete') from exc
    if (side not in ('BUY', 'SELL') or not isinstance(order_type, str) or not order_type
            or not isinstance(status, str) or not status or not isinstance(client, str) or not client):
        raise Unknown('order history row is incomplete')
    if original > 0 and executed > original:
        raise Unknown('order history executed quantity exceeds the original')
    original_client = row.get('origClientOrderId')
    if not isinstance(original_client, str) or not original_client:
        original_client = client
    parsed = {
        'orderId': order_id,
        'clientOrderId': client,
        'origClientOrderId': original_client,
        'symbol': 'BTCUSDT',
        'side': side,
        'type': order_type,
        'status': status,
        'origQty': format(original, 'f'),
        'executedQty': format(executed, 'f'),
        'time': stamp,
        'updateTime': row['updateTime'] if type(row.get('updateTime')) is int else None,
        'cummulativeQuoteQty': None,
        'origQuoteOrderQty': None,
        'stopPrice': None,
    }
    if row.get('cummulativeQuoteQty') not in (None, ''):
        parsed['cummulativeQuoteQty'] = format(
            number(row['cummulativeQuoteQty'], 'cummulativeQuoteQty', nonnegative=True), 'f')
    if row.get('origQuoteOrderQty') not in (None, ''):
        parsed['origQuoteOrderQty'] = format(
            number(row['origQuoteOrderQty'], 'origQuoteOrderQty', nonnegative=True), 'f')
    if row.get('stopPrice') not in (None, ''):
        stop = number(row['stopPrice'], 'stopPrice', nonnegative=True)
        parsed['stopPrice'] = None if stop == 0 else format(stop, 'f')
    return parsed


def _order(row: dict) -> dict:
    if not isinstance(row, dict):
        raise Unknown('open order row is incomplete')
    if row.get('symbol', 'BTCUSDT') != 'BTCUSDT':
        raise Unknown('open order symbol differs from BTCUSDT')
    try:
        order_id = row['orderId']
        if type(order_id) is not int or order_id <= 0:
            raise Unknown('open order native identity is invalid')
        side = row['side']
        order_type = row['type']
        status = row['status']
        original = number(row['origQty'], 'origQty', nonnegative=True)
        executed = number(row['executedQty'], 'executedQty', nonnegative=True)
    except (KeyError, TypeError, ValueError, Blocked) as exc:
        raise Unknown('open order row is incomplete') from exc
    if (side not in ('BUY', 'SELL') or not order_type or not status
            or not isinstance(row.get('clientOrderId'), str) or not row['clientOrderId']
            or executed > original):
        raise Unknown('open order row is incomplete')
    parsed = {
        'order_id': order_id,
        'client_id': row.get('clientOrderId'),
        'side': side,
        'type': order_type,
        'status': status,
        'orig_qty': format(original, 'f'),
        'executed_qty': format(executed, 'f'),
    }
    if row.get('stopPrice') not in (None, ''):
        parsed['stop_price'] = format(number(row['stopPrice'], 'stopPrice', nonnegative=True), 'f')
    return parsed


def _millis(raw) -> int:
    value = int(raw)
    return value // 1000 if value > 10**14 else value
