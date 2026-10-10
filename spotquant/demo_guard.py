"""Hard stop for Demo verification. Orders never go to a mainnet host."""
from __future__ import annotations

import urllib.parse
from decimal import Decimal as D

from .types import Blocked

DEMO_ORIGIN = 'https://demo-api.binance.com'
DEMO_HOST = 'demo-api.binance.com'
MAX_CAPITAL = D('100')
LIVE_HOSTS = frozenset({
    'api.binance.com',
    'api1.binance.com',
    'api2.binance.com',
    'api3.binance.com',
    'api4.binance.com',
    'api-gcp.binance.com',
    'testnet.binance.vision',
    'testnet.binance.com',
})


def assert_demo_config(config, *, capital=False) -> None:
    """Reject a live configuration before credentials or the network are used."""
    if getattr(config, 'environment', None) != 'demo':
        raise Blocked('demo verification only runs when environment is demo')
    if capital and (config.capital_limit is None or config.capital_limit > MAX_CAPITAL):
        raise Blocked('demo verification only runs with a positive capital ceiling of at most 100 USDT')


def refuse_non_demo_url(url: str) -> None:
    """Reject every request whose host is not the Spot Demo REST host."""
    if not isinstance(url, str) or url != url.strip() or any(item in url for item in ('\n', '\r', ' ', '\t')):
        raise Blocked(f'demo verification only sends to {DEMO_ORIGIN}')
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if (parts.scheme != 'https' or host != DEMO_HOST or parts.username or parts.password
            or parts.port not in (None, 443)):
        raise Blocked(f'demo verification only sends to {DEMO_ORIGIN}')
    if host in LIVE_HOSTS:
        raise Blocked(f'demo verification only sends to {DEMO_ORIGIN}')


def install_demo_guard(venue):
    """Wrap the adapter so a later base-URL change cannot reach mainnet."""
    if getattr(venue, 'environment', None) != 'demo' or getattr(venue, 'base', None) != DEMO_ORIGIN:
        raise Blocked(f'demo verification only sends to {DEMO_ORIGIN}')
    opener = venue._opener

    def guarded(method, url, headers):
        refuse_non_demo_url(url)
        return opener(method, url, headers)

    venue._opener = guarded
    venue._demo_guard = True
    return venue
