"""Normalized, fresh, read-only BTC account export."""
from decimal import Decimal as D
import time

from .types import Unknown, number, serial


def export(config, venue):
    started = int(venue.clock() * 1000)
    snapshot = venue.snapshot(config.account_uid)
    ticker = venue._get('/api/v3/ticker/price', {'symbol': 'BTCUSDT'}, signed=False)
    price = number(ticker['price'], positive=True)
    ended = int(venue.clock() * 1000)
    if not 0 <= ended - started <= 5000:
        raise Unknown('account and price collection exceeded five seconds')
    cash = D(snapshot['usdt_free']) + D(snapshot['usdt_locked'])
    qty = D(snapshot['btc'])
    return serial({'known': True, 'symbol': 'BTCUSDT', 'market': 'spot',
                   'environment': config.environment, 'account_uid': config.account_uid,
                   'observed_at_ms': started, 'collected_until_ms': ended,
                   'equity_usdt': cash + qty * price, 'cash_usdt': cash,
                   'btc_position': qty, 'btc_price_usdt': price,
                   'orders': snapshot['orders'], 'write_attempted': False,
                   'native_execution_verified': False})
