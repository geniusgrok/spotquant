"""Normalized, fresh, read-only BTC account export."""
from decimal import Decimal as D

from .types import Unknown, number, serial


def export(config, venue):
    started = int(venue.clock() * 1000)
    snapshot = venue.snapshot(config.account_uid)
    if snapshot.get('other_assets'):
        raise Unknown('other assets prevent a complete BTC/USDT account export')
    ticker = venue._get('/api/v3/ticker/price', {'symbol': 'BTCUSDT'}, signed=False)
    price = number(ticker['price'], positive=True)
    ended = int(venue.clock() * 1000)
    if not 0 <= ended - started <= 5000:
        raise Unknown('account and price collection exceeded five seconds')
    cash = D(snapshot['usdt_free']) + D(snapshot['usdt_locked'])
    qty = D(snapshot['btc'])
    stops = [row for row in snapshot['orders'] if row['side'] == 'SELL' and row['type'] == 'STOP_LOSS'
             and row['status'] in ('NEW', 'PARTIALLY_FILLED') and D(row.get('stop_price', '0')) > 0]
    protected = sum((max(D(0), D(row['orig_qty']) - D(row['executed_qty'])) for row in stops), D(0))
    return serial({'known': True, 'symbol': 'BTCUSDT', 'market': 'spot',
                   'environment': config.environment, 'account_uid': config.account_uid,
                   'observed_at_ms': started, 'collected_until_ms': ended,
                   'equity_usdt': cash + qty * price, 'cash_usdt': cash,
                   'btc_position': qty, 'btc_price_usdt': price,
                   'available_usdt': snapshot['usdt_free'],
                   'native_stop_quantity_btc': protected,
                   'btc_without_native_stop': max(D(0), qty - protected),
                   'protection_observation': 'quantity only; ownership and trigger execution are not verified',
                   'orders': snapshot['orders'], 'write_attempted': False})
