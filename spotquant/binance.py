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


def _default_opener(method: str, url: str, headers: dict) -> tuple[int, bytes]:
    if method != 'GET':
        raise Blocked('spot adapter issues GET requests only')
    request = urllib.request.Request(url, headers=headers, method='GET')
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Blocked:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Unknown('Binance request failed before a usable response') from exc


class Binance:
    def __init__(self, *, key: str, secret: str, environment: str, capital_limit=None,
                 opener=None, clock=None):
        if environment not in HOSTS:
            raise Blocked('environment must be live or demo')
        if not key or not secret:
            raise Blocked('Binance read credentials are empty')
        self.key = key
        self.secret = secret
        self.environment = environment
        self.capital_limit = capital_limit
        self.base = HOSTS[environment]
        self._opener = opener or _default_opener
        self._clock = clock or time.time
        self._offset_ms = None

    def clock(self) -> float:
        return self._clock()

    def place_order(self, *args, **kwargs):
        raise Blocked('spot execution is not qualified; no order request is sent')

    def snapshot(self, expected_uid: str) -> dict:
        """Balances, open-order count, and a UID check. No order payload is kept."""
        self._filters()
        account = self._get('/api/v3/account', signed=True)
        uid_body = self._get('/sapi/v1/account/uid', signed=True)
        uid = str(uid_body.get('uid', ''))
        if uid != str(expected_uid):
            raise Blocked('exchange UID does not match account_uid')
        orders = self._get('/api/v3/openOrders', {'symbol': 'BTCUSDT'}, signed=True)
        if not isinstance(orders, list):
            raise Unknown('open orders response is not a list')
        btc = D(0)
        usdt_free = D(0)
        usdt_locked = D(0)
        for row in account.get('balances') or []:
            asset = row.get('asset')
            if asset == 'BTC':
                btc += number(row.get('free', '0'), 'btc') + number(row.get('locked', '0'), 'btc')
            elif asset == 'USDT':
                usdt_free += number(row.get('free', '0'), 'usdt')
                usdt_locked += number(row.get('locked', '0'), 'usdt')
        return {
            'account_uid': uid,
            'btc': btc,
            'usdt_free': usdt_free,
            'usdt_locked': usdt_locked,
            'open_orders': len(orders),
            'environment': self.environment,
        }

    def completed_daily(self, after_open_ms: int | None) -> list[tuple[int, D, D, D]]:
        """Completed UTC daily bars strictly after ``after_open_ms`` (or from the origin)."""
        cursor = ORIGIN if after_open_ms is None else int(after_open_ms) + DAY
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
            for row in payload:
                open_ms = _millis(row[0])
                if open_ms < cursor or open_ms >= today:
                    continue
                bars.append((open_ms, number(row[2], 'high', positive=True),
                             number(row[3], 'low', positive=True), number(row[4], 'close', positive=True)))
            last = _millis(payload[-1][0])
            nxt = last + DAY
            if nxt <= cursor:
                break
            cursor = nxt
            if len(payload) < 1000:
                break
        bars.sort()
        return bars

    def _filters(self) -> None:
        info = self._get('/api/v3/exchangeInfo', {'symbol': 'BTCUSDT'}, signed=False)
        symbols = info.get('symbols') or []
        if len(symbols) != 1 or symbols[0].get('symbol') != 'BTCUSDT':
            raise Unknown('BTCUSDT was not the only symbol in the filter response')
        symbol = symbols[0]
        if symbol.get('status') != 'TRADING' or symbol.get('isSpotTradingAllowed') is False:
            raise Blocked('BTCUSDT spot trading is not enabled')
        if symbol.get('baseAsset') != 'BTC' or symbol.get('quoteAsset') != 'USDT':
            raise Blocked('BTCUSDT is no longer BTC quoted in USDT')
        filters = {item.get('filterType'): item for item in symbol.get('filters') or []}
        lot = filters.get('LOT_SIZE') or {}
        price = filters.get('PRICE_FILTER') or {}
        notional = filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL') or {}
        trailing = filters.get('TRAILING_DELTA') or {}
        if (number(lot.get('stepSize', '0'), 'step') != D('0.00001')
                or number(price.get('tickSize', '0'), 'tick') != D('0.01')):
            raise Blocked('BTCUSDT tick or step no longer matches the researched filters')
        minimum = notional.get('minNotional')
        if number(minimum or '0', 'minNotional') != D('5'):
            raise Blocked('BTCUSDT minimum notional is no longer 5 USDT')
        below = trailing.get('maxTrailingBelowDelta')
        if below is None or int(below) < 2000:
            raise Blocked('BTCUSDT cannot rest a 20% trailingDelta')

    def _today_open(self) -> int:
        now = self._timestamp()
        return now - (now % DAY)

    def _timestamp(self) -> int:
        if self._offset_ms is None:
            payload = self._get('/api/v3/time', signed=False)
            server = int(payload['serverTime'])
            self._offset_ms = server - int(self._clock() * 1000)
        return int(self._clock() * 1000) + self._offset_ms

    def _get(self, path: str, params: dict | None = None, *, signed: bool):
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
        status, body = self._opener('GET', url, headers)
        try:
            payload = json.loads(body.decode())
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise Unknown(f'Binance {path} returned status {status} without JSON') from exc
        if status != 200:
            code = payload.get('code') if isinstance(payload, dict) else None
            raise Unknown(f'Binance {path} failed with HTTP {status} code {code}')
        return payload


def _millis(raw) -> int:
    value = int(raw)
    return value // 1000 if value > 10**14 else value
