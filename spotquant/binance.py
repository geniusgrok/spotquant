"""Read-only Binance spot BTCUSDT adapter. GET requests only.

Live host ``api.binance.com``. Demo host ``demo-api.binance.com``. The API key
is sent only to that configured host. Redirects are refused. ``place_order``
cannot be turned into a request.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal as D

from .follow import normalize_trade
from .types import Blocked, Unknown, number

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


def _default_opener(method: str, url: str, headers: dict):
    request = urllib.request.Request(url, headers=headers, method='GET')
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())
    except Blocked:
        raise
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
        if demo_execution_uid is not None and (environment != 'demo' or capital_limit is None
                or number(capital_limit, positive=True) <= 0
                or not isinstance(demo_execution_uid, str) or not demo_execution_uid.isdigit()):
            raise Blocked('Demo execution needs explicit UID and capital ceiling')
        self.demo_execution_uid = demo_execution_uid
        self.execution_authorized = demo_execution_uid is not None
        self.base = HOSTS[environment]
        self._opener = opener or _default_opener
        self._clock = clock or time.time
        self._offset_ms = None
        self._clock_retried = False
        self._stop = None
        self.ask_multiplier_down = None
        self.ask_multiplier_up = None
        self.trailing_max_bips = None
        self.min_price = None
        self.max_price = None
        self.min_qty = None
        self.max_qty = None
        self.market_min_qty = None
        self.market_max_qty = None
        self.market_step = None
        self.max_notional = None

    def clock(self) -> float:
        return self._clock()

    def place_order(self, *args, **kwargs):
        raise Blocked('spot execution is not qualified; no order request is sent')

    def query(self, identity):
        row = self._get('/api/v3/order', {'symbol': 'BTCUSDT', 'origClientOrderId': identity}, signed=True)
        return self._execution_order(row)

    def submit(self, identity, payload):
        if not self.execution_authorized:
            raise Blocked('Demo execution is not explicitly enabled')
        if self.snapshot(self.demo_execution_uid)['can_trade'] is not True:
            raise Blocked('Demo account cannot trade')
        params = dict(payload, newClientOrderId=identity, newOrderRespType='FULL')
        if set(payload) - {'symbol', 'side', 'type', 'quantity', 'quoteOrderQty', 'stopPrice'}:
            raise Blocked('unsupported Demo order fields')
        if payload.get('symbol') != 'BTCUSDT' or payload.get('type') not in ('MARKET', 'STOP_LOSS'):
            raise Blocked('unsupported Demo market or order type')
        return self._execution_order(self._get('/api/v3/order', params, signed=True, method='POST'))

    def cancel(self, identity):
        return self._execution_order(self._get('/api/v3/order',
            {'symbol': 'BTCUSDT', 'origClientOrderId': identity}, signed=True, method='DELETE'))

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
        btc = D(0)
        usdt_free = D(0)
        usdt_locked = D(0)
        seen = set()
        for row in account['balances']:
            if not isinstance(row, dict) or 'asset' not in row or 'free' not in row or 'locked' not in row:
                raise Unknown('account balance row is incomplete')
            asset = row['asset']
            if asset in seen:
                raise Unknown('account balance row is repeated')
            seen.add(asset)
            free = number(row['free'], asset, nonnegative=True)
            locked = number(row['locked'], asset, nonnegative=True)
            if asset == 'BTC':
                btc = free + locked
            elif asset == 'USDT':
                usdt_free = free
                usdt_locked = locked
        if not isinstance(average, dict) or 'price' not in average:
            raise Unknown('average price response is incomplete')
        return {
            'account_uid': uid,
            'btc': btc,
            'usdt_free': usdt_free,
            'usdt_locked': usdt_locked,
            'open_orders': len(orders),
            'orders': orders,
            'can_trade': account.get('canTrade') is True,
            'environment': self.environment,
            'avg_price': number(average['price'], 'avgPrice', positive=True),
            'ask_multiplier_down': self.ask_multiplier_down,
            'ask_multiplier_up': self.ask_multiplier_up,
            'trailing_max_bips': self.trailing_max_bips,
            'min_price': self.min_price,
            'max_price': self.max_price,
            'min_qty': self.min_qty,
            'max_qty': self.max_qty,
            'market_min_qty': self.market_min_qty,
            'market_max_qty': self.market_max_qty,
            'market_step': self.market_step,
            'max_notional': self.max_notional,
        }

    def trades(self, since_ms: int) -> list[dict]:
        """BTCUSDT fills at or after ``since_ms``. A page that does not advance is unknown."""
        if type(since_ms) is not int:
            raise Blocked('trade cursor must be an integer millisecond timestamp')
        params = {'symbol': 'BTCUSDT', 'startTime': str(since_ms), 'limit': '1000'}
        found = []
        seen = set()
        while True:
            payload = self._get('/api/v3/myTrades', params, signed=True)
            if not isinstance(payload, list):
                raise Unknown('trade response is not a list')
            if not payload:
                break
            ids = []
            for row in payload:
                trade = normalize_trade(row)
                ids.append(trade['id'])
                if trade['time'] >= since_ms and trade['id'] not in seen:
                    seen.add(trade['id'])
                    found.append(trade)
            if len(payload) < 1000:
                break
            nxt = max(ids) + 1
            if params.get('fromId') is not None and nxt <= int(params['fromId']):
                raise Unknown('trade page does not advance')
            params = {'symbol': 'BTCUSDT', 'fromId': str(nxt), 'limit': '1000'}
        found.sort(key=lambda item: (item['time'], item['id']))
        return found

    def completed_daily(self, after_open_ms: int | None) -> list[tuple[int, D, D, D]]:
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
        today = self._today_open()
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
                page.append((open_ms, number(row[2], 'high', positive=True),
                             number(row[3], 'low', positive=True), number(row[4], 'close', positive=True)))
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
        lot = filters.get('LOT_SIZE') or {}
        price = filters.get('PRICE_FILTER') or {}
        notional = filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL') or {}
        market = filters.get('MARKET_LOT_SIZE') or {}
        if (number(lot.get('stepSize', '0'), 'step') != D('0.00001')
                or number(price.get('tickSize', '0'), 'tick') != D('0.01')):
            raise Blocked('BTCUSDT tick or step no longer matches the researched filters')
        if 'minNotional' not in notional:
            raise Blocked('BTCUSDT minimum notional is missing')
        if number(notional['minNotional'], 'minNotional') != D('5'):
            raise Blocked('BTCUSDT minimum notional is no longer 5 USDT')
        self.min_qty = number(lot['minQty'], 'minQty', positive=True) if 'minQty' in lot else None
        self.max_qty = number(lot['maxQty'], 'maxQty', positive=True) if 'maxQty' in lot else None
        self.min_price = number(price['minPrice'], 'minPrice', positive=True) if price.get('minPrice') not in (None, '0', '0.00000000') else None
        self.max_price = number(price['maxPrice'], 'maxPrice', positive=True) if price.get('maxPrice') not in (None, '0', '0.00000000') else None
        if market:
            self.market_step = number(market.get('stepSize', '0.00001'), 'market step', positive=True)
            self.market_min_qty = number(market['minQty'], 'market minQty', positive=True) if 'minQty' in market else None
            self.market_max_qty = number(market['maxQty'], 'market maxQty', positive=True) if 'maxQty' in market else None
        if notional.get('maxNotional') not in (None, ''):
            self.max_notional = number(notional['maxNotional'], 'maxNotional', positive=True)
        types = symbol.get('orderTypes') or []
        if 'STOP_LOSS' not in types or 'MARKET' not in types:
            raise Blocked('BTCUSDT spot cannot rest the researched market and stop orders')
        trailing = filters.get('TRAILING_DELTA') or {}
        try:
            self.trailing_max_bips = int(trailing['maxTrailingBelowDelta'])
        except (KeyError, TypeError, ValueError) as exc:
            raise Blocked('BTCUSDT trailingDelta bound is missing') from exc
        if self.trailing_max_bips <= 0:
            raise Blocked('BTCUSDT trailingDelta bound is missing')
        band = filters.get('PERCENT_PRICE_BY_SIDE') or {}
        try:
            down = number(band['askMultiplierDown'], 'askMultiplierDown', positive=True)
            up = number(band['askMultiplierUp'], 'askMultiplierUp', positive=True)
        except (KeyError, Blocked) as exc:
            raise Blocked('BTCUSDT percent price band is missing') from exc
        if up < 1:
            raise Blocked('BTCUSDT percent price band is invalid')
        self.ask_multiplier_down = down
        self.ask_multiplier_up = up

    def _today_open(self) -> int:
        now = self._timestamp()
        return now - (now % DAY)

    def _timestamp(self) -> int:
        if self._offset_ms is None:
            payload = self._get('/api/v3/time', signed=False)
            server = int(payload['serverTime'])
            self._offset_ms = server - int(self._clock() * 1000)
        return int(self._clock() * 1000) + self._offset_ms

    def _get(self, path: str, params: dict | None = None, *, signed: bool, method='GET'):
        if method != 'GET' and (not self.execution_authorized or self.environment != 'demo'
                or path != '/api/v3/order' or method not in ('POST', 'DELETE') or not signed):
            raise Blocked('only explicitly enabled Demo order writes are supported')
        if not path.startswith('/'):
            raise Blocked('refusing a request outside the configured Binance host')
        pairs = [(key, str(value)) for key, value in (params or {}).items()]
        headers = {'User-Agent': 'spotquant/0.1.0'}
        if signed:
            pairs.append(('timestamp', str(self._timestamp())))
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
        self._check_deadline()
        opened = self._opener(method, url, headers)
        if not isinstance(opened, tuple) or len(opened) not in (2, 3):
            raise Unknown('Binance response is incomplete')
        status, body = opened[0], opened[1]
        response_headers = opened[2] if len(opened) == 3 else {}
        try:
            payload = json.loads(body.decode())
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise Unknown(f'Binance {path} returned status {status} without JSON') from exc
        if status in (418, 429):
            retry = _header(response_headers, 'Retry-After')
            raise Unknown(f'Binance rate limit HTTP {status}; retry after {retry or "unknown"}')
        if status != 200:
            code = payload.get('code') if isinstance(payload, dict) else None
            if method == 'GET' and signed and code == -1021 and not self._clock_retried:
                self._clock_retried = True
                self._offset_ms = None
                try:
                    return self._get(path, params, signed=signed)
                finally:
                    self._clock_retried = False
            raise Unknown(f'Binance {path} failed with HTTP {status} code {code}')
        return payload

    def _check_deadline(self) -> None:
        if self._stop is not None and self._stop():
            raise Unknown('session deadline reached; not starting another request')


def _header(headers, name: str):
    for key, value in dict(headers).items():
        if str(key).lower() == name.lower():
            return value
    return None


def _order(row: dict) -> dict:
    if not isinstance(row, dict):
        raise Unknown('open order row is incomplete')
    try:
        order_id = int(row['orderId'])
        side = row['side']
        order_type = row['type']
        status = row['status']
        original = number(row['origQty'], 'origQty', nonnegative=True)
        executed = number(row['executedQty'], 'executedQty', nonnegative=True)
    except (KeyError, TypeError, ValueError, Blocked) as exc:
        raise Unknown('open order row is incomplete') from exc
    if side not in ('BUY', 'SELL') or not order_type or not status:
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
