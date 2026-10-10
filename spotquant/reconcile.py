"""Compare a Demo state directory with Binance order and trade history.

The report is pass only when every durable order and fill agrees with the
exchange. An unconfirmed local intent stays a mismatch: a missing exchange
row is not proof the order was never sent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .binance import normalize_order
from .demo_guard import install_demo_guard
from .state import State
from .types import Blocked, NotFound, Unknown, number, serial

DAY_MS = 86_400_000
PAD_MS = 60_000
SECRET_KEYS = frozenset({
    'secret', 'apikey', 'api_key', 'signature', 'x-mbx-apikey', 'password',
})
def _num(value):
    if value is None or value == '':
        return None
    return number(value)


def _same(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return _num(left) == _num(right)


def _secret_paths(value, path=''):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f'{path}{key}'
            if str(key).lower() in SECRET_KEYS:
                found.append(here)
            else:
                found.extend(_secret_paths(item, here + '.'))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_secret_paths(item, f'{path}{index}.'))
    return found


def load_latest(directory) -> dict | None:
    path = Path(directory).expanduser() / 'latest.json'
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise Unknown('latest.json cannot be read') from exc
    if not isinstance(data, dict):
        raise Unknown('latest.json is not an object')
    return data


def load_local(state) -> tuple[list[dict], list[dict]]:
    orders = []
    for identity, payload, status, result, updated in state.db.execute(
            "SELECT id,payload,status,result,updated FROM intents WHERE kind='p4' ORDER BY updated,id"):
        body = json.loads(payload)
        native = json.loads(result)
        order_id = native.get('orderId')
        if order_id is not None and (type(order_id) is not int or order_id <= 0):
            order_id = None
        orders.append({
            'id': identity,
            'status': status,
            'order': body.get('order') or {},
            'cancel_id': body.get('cancel_id'),
            'result': native,
            'order_id': order_id,
            'updated_ms': int(float(updated) * 1000),
        })
    fills = []
    for payload, in state.db.execute('SELECT payload FROM fills ORDER BY time_ms,id'):
        fills.append(json.loads(payload))
    return orders, fills


def _local_names(local) -> set:
    names = {local['id']}
    if isinstance(local.get('cancel_id'), str) and local['cancel_id']:
        names.add(local['cancel_id'])
    return names


def _match_exchange(local, exchange_orders):
    names = _local_names(local)
    by_id = None
    by_client = None
    for row in exchange_orders:
        if local.get('order_id') and row['orderId'] == local['order_id']:
            by_id = row
        if row['clientOrderId'] in names or row['origClientOrderId'] in names:
            by_client = row
    if by_id is not None and by_client is not None and by_id['orderId'] != by_client['orderId']:
        return by_id, 'client id and order id point at different exchange orders'
    return by_id or by_client, None


def _window_start(orders, fills, latest) -> int | None:
    times = []
    for row in orders:
        for key in ('time', 'transactTime', 'updateTime'):
            stamp = row['result'].get(key)
            if type(stamp) is int and stamp > 0:
                times.append(stamp)
        if row['updated_ms'] > 0:
            times.append(row['updated_ms'])
    for row in fills:
        if type(row.get('time')) is int:
            times.append(row['time'])
    if isinstance(latest, dict) and type(latest.get('session_started_at_ms')) is int:
        times.append(latest['session_started_at_ms'])
    if not times:
        return None
    return max(0, min(times) - PAD_MS)


def _ignorable(local) -> bool:
    """A local row that never received an exchange id and was not left in flight."""
    if local.get('order_id'):
        return False
    result = local['result']
    if local['status'] == 'prepared' and result.get('not_sent') is True:
        return True
    if local['status'] == 'rejected':
        return True
    if local['status'] == 'settled' and result.get('not_sent') is True:
        return True
    return False


def compare_books(local_orders, local_fills, exchange_orders, exchange_trades, latest, *,
                  account_uid, conflicts=(), window_start_ms=None) -> dict:
    """Pure comparison. ``passed`` is false when any mismatch remains."""
    mismatches = []
    if latest is None:
        mismatches.append({'kind': 'missing_latest', 'id': 'latest.json',
                           'detail': 'state_dir/latest.json is absent'})
    elif not isinstance(latest, dict):
        mismatches.append({'kind': 'latest_unreadable', 'id': 'latest.json',
                           'detail': 'latest.json is not an object'})
    else:
        if latest.get('environment') not in (None, 'demo'):
            mismatches.append({'kind': 'latest_environment', 'id': 'latest.json',
                               'detail': 'latest.json environment is not demo',
                               'local': latest.get('environment')})
        if latest.get('account_uid') not in (None, account_uid):
            mismatches.append({'kind': 'latest_account', 'id': 'latest.json',
                               'detail': 'latest.json account_uid differs from the configuration',
                               'local': latest.get('account_uid')})
        for path in _secret_paths(latest):
            mismatches.append({'kind': 'latest_secret', 'id': path,
                               'detail': 'latest.json contains a credential field'})
        evidence = latest.get('execution_evidence') or {}
        local_trade_ids = {int(row['id']) for row in local_fills if type(row.get('id')) is not bool
                           and isinstance(row.get('id'), int)}
        local_order_ids = {row['order_id'] for row in local_orders if row.get('order_id')}
        for fill in evidence.get('fills') or []:
            trade_id = fill.get('trade_id')
            if type(trade_id) is int and trade_id not in local_trade_ids:
                mismatches.append({'kind': 'latest_fill_missing', 'id': str(trade_id),
                                   'detail': 'latest.json names a fill that is not in sqlite'})
        for row in evidence.get('protection_orders') or []:
            order_id = row.get('order_id')
            if type(order_id) is int and order_id not in local_order_ids:
                mismatches.append({'kind': 'latest_protection_missing', 'id': str(order_id),
                                   'detail': 'latest.json names a protection order that is not in sqlite'})

    claimed = {}
    for local in local_orders:
        if local.get('order_id'):
            claimed.setdefault(local['order_id'], []).append(local['id'])
    for order_id, identities in sorted(claimed.items()):
        if len(identities) > 1:
            mismatches.append({'kind': 'duplicate_local_order', 'id': str(order_id),
                               'detail': 'more than one durable intent claims this exchange order',
                               'local': identities})

    matched_exchange = set()
    for local in local_orders:
        if _ignorable(local):
            continue
        if not local.get('order_id') and not any(
                row['clientOrderId'] in _local_names(local) or row['origClientOrderId'] in _local_names(local)
                for row in exchange_orders):
            mismatches.append({'kind': 'unconfirmed_local_order', 'id': local['id'],
                               'detail': 'durable intent has no exchange order; it was not treated as unsent',
                               'local': local['status']})
            continue
        found, conflict = _match_exchange(local, exchange_orders)
        if conflict:
            mismatches.append({'kind': 'identity_conflict', 'id': local['id'], 'detail': conflict})
            continue
        if found is None:
            mismatches.append({'kind': 'missing_exchange_order', 'id': local['id'],
                               'detail': 'sqlite order is absent from exchange history',
                               'local': local.get('order_id')})
            continue
        matched_exchange.add(found['orderId'])
        order = local['order']
        if found['side'] != order.get('side') or found['type'] != order.get('type'):
            mismatches.append({'kind': 'order_terms', 'id': local['id'],
                               'detail': 'side or type differs',
                               'local': {'side': order.get('side'), 'type': order.get('type')},
                               'exchange': {'side': found['side'], 'type': found['type']}})
        if 'quantity' in order and not _same(order['quantity'], found['origQty']):
            mismatches.append({'kind': 'order_quantity', 'id': local['id'],
                               'detail': 'quantity differs',
                               'local': order.get('quantity'), 'exchange': found['origQty']})
        if ('quoteOrderQty' in order and found['origQuoteOrderQty'] is not None
                and not _same(order['quoteOrderQty'], found['origQuoteOrderQty'])):
            mismatches.append({'kind': 'order_quote', 'id': local['id'],
                               'detail': 'quote quantity differs',
                               'local': order.get('quoteOrderQty'), 'exchange': found['origQuoteOrderQty']})
        if 'stopPrice' in order and not _same(order['stopPrice'], found['stopPrice']):
            mismatches.append({'kind': 'stop_price', 'id': local['id'],
                               'detail': 'stop price differs',
                               'local': order.get('stopPrice'), 'exchange': found['stopPrice']})
        native_status = local['result'].get('status')
        if isinstance(native_status, str) and native_status != found['status']:
            mismatches.append({'kind': 'order_status', 'id': local['id'],
                               'detail': 'status differs',
                               'local': native_status, 'exchange': found['status']})
        elif local['status'] == 'resting' and found['status'] not in ('NEW', 'PARTIALLY_FILLED'):
            mismatches.append({'kind': 'order_status', 'id': local['id'],
                               'detail': 'resting intent is not open on the exchange',
                               'local': local['status'], 'exchange': found['status']})
        if 'executedQty' in local['result'] and not _same(local['result']['executedQty'], found['executedQty']):
            mismatches.append({'kind': 'executed_quantity', 'id': local['id'],
                               'detail': 'executed quantity differs',
                               'local': local['result'].get('executedQty'), 'exchange': found['executedQty']})
        if local.get('order_id') and local['order_id'] != found['orderId']:
            mismatches.append({'kind': 'order_id', 'id': local['id'],
                               'detail': 'exchange order id differs',
                               'local': local['order_id'], 'exchange': found['orderId']})

    for order_id in conflicts:
        mismatches.append({'kind': 'conflicting_exchange_rows', 'id': str(order_id),
                           'detail': 'allOrders and the order query disagree'})

    for row in exchange_orders:
        if row['orderId'] in matched_exchange:
            continue
        mismatches.append({'kind': 'untracked_exchange_order', 'id': str(row['orderId']),
                           'detail': 'exchange order is not in sqlite',
                           'exchange': {'clientOrderId': row['clientOrderId'],
                                        'origClientOrderId': row['origClientOrderId'],
                                        'side': row['side'], 'type': row['type'],
                                        'status': row['status']}})

    trade_index = {}
    for row in exchange_trades:
        trade_index[int(row['id'])] = row
    seen_local = set()
    for row in local_fills:
        trade_id = row.get('id')
        label = str(trade_id)
        if type(trade_id) is not int:
            mismatches.append({'kind': 'local_fill_identity', 'id': label,
                               'detail': 'sqlite fill id is not an integer'})
            continue
        seen_local.add(trade_id)
        found = trade_index.get(trade_id)
        if found is None:
            mismatches.append({'kind': 'missing_exchange_fill', 'id': label,
                               'detail': 'sqlite fill is absent from myTrades'})
            continue
        for key, other in (('order_id', 'order_id'), ('qty', 'qty'), ('quote', 'quote'),
                           ('price', 'price'), ('commission', 'commission')):
            if not _same(row.get(key), found.get(other)):
                mismatches.append({'kind': 'fill_' + key, 'id': label, 'detail': key + ' differs',
                                   'local': row.get(key), 'exchange': str(found.get(other))})
                break
        else:
            if bool(row.get('buyer')) != bool(found.get('buyer')):
                mismatches.append({'kind': 'fill_side', 'id': label, 'detail': 'fill side differs'})
            elif (row.get('commission_asset') or None) != (found.get('commission_asset') or None):
                mismatches.append({'kind': 'fill_commission_asset', 'id': label,
                                   'detail': 'commission asset differs'})
            elif type(row.get('time')) is int and int(found.get('time')) != int(row['time']):
                mismatches.append({'kind': 'fill_time', 'id': label, 'detail': 'fill time differs',
                                   'local': row.get('time'), 'exchange': found.get('time')})

    known_orders = {row['order_id'] for row in local_orders if row.get('order_id')}
    for trade_id, row in sorted(trade_index.items()):
        if trade_id in seen_local:
            continue
        if row.get('order_id') in known_orders:
            mismatches.append({'kind': 'missing_local_fill', 'id': str(trade_id),
                               'detail': 'myTrades fill for a sqlite order is not in sqlite',
                               'exchange': row.get('order_id')})

    mismatches.sort(key=lambda item: (item['kind'], str(item['id'])))
    return {
        'status': 'pass' if not mismatches else 'failed',
        'passed': not mismatches,
        'native_execution_verified': False,
        'environment': 'demo',
        'account_uid': account_uid,
        'window_start_ms': window_start_ms,
        'counts': {
            'local_orders': len(local_orders),
            'local_fills': len(local_fills),
            'exchange_orders': len(exchange_orders),
            'exchange_trades': len(exchange_trades),
            'mismatches': len(mismatches),
        },
        'mismatches': mismatches,
        'reason': ('ledger matches exchange history' if not mismatches
                   else 'ledger differs from exchange history'),
        'note': 'Demo fills are simulated. A passing comparison does not verify mainnet execution.',
    }


def _merge_query(venue, local_orders, history):
    merged = {row['orderId']: row for row in history}
    conflicts = []
    for local in local_orders:
        order_id = local.get('order_id')
        if not order_id:
            continue
        try:
            row = normalize_order(venue.query(order_id))
        except NotFound:
            continue
        current = merged.get(row['orderId'])
        if current is not None and current != row:
            conflicts.append(row['orderId'])
        merged[row['orderId']] = row
    return list(merged.values()), conflicts


def reconcile_account(config, venue, state, *, now_ms=None) -> dict:
    latest = load_latest(state.directory)
    local_orders, local_fills = load_local(state)
    window = _window_start(local_orders, local_fills, latest)
    if window is None:
        if now_ms is None:
            now_ms = venue._timestamp()
        window = max(0, int(now_ms) - DAY_MS)
    history = venue.all_orders(window)
    history, conflicts = _merge_query(venue, local_orders, history)
    trades = venue.trades(window)
    report = compare_books(local_orders, local_fills, history, trades, latest,
                           account_uid=config.account_uid, conflicts=conflicts,
                           window_start_ms=window)
    report['state_dir'] = str(state.directory)
    return report


def render_reconcile_md(report: dict) -> str:
    counts = report.get('counts') or {}
    lines = [
        '# Demo 对账',
        '',
        f"- 结论：{'通过' if report.get('passed') else '未通过'}",
        f"- 环境：{report.get('environment')}",
        f"- 账户：{report.get('account_uid')}",
        f"- 窗口起点（毫秒）：{report.get('window_start_ms')}",
        f"- 本地订单 / 成交：{counts.get('local_orders')} / {counts.get('local_fills')}",
        f"- 交易所订单 / 成交：{counts.get('exchange_orders')} / {counts.get('exchange_trades')}",
        '',
        'Demo 成交由交易所模拟。本报告通过也不表示主网执行已验证。',
        '',
        '## 不一致',
        '',
    ]
    mismatches = report.get('mismatches') or []
    if not mismatches:
        lines.append('无。')
    else:
        lines.append('| 类型 | 身份 | 说明 |')
        lines.append('| --- | --- | --- |')
        for item in mismatches:
            detail = str(item.get('detail', '')).replace('|', '/')
            lines.append(f"| {item.get('kind')} | {item.get('id')} | {detail} |")
    lines.append('')
    return '\n'.join(lines)


def write_reconcile(directory, report: dict) -> dict:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = dict(report, results_dir=str(directory))
    body = serial(report)
    _atomic(directory / 'reconcile.json', json.dumps(body, indent=2, ensure_ascii=False) + '\n')
    _atomic_text(directory / 'reconcile.md', render_reconcile_md(body))
    return report


def _atomic(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def _atomic_text(path: Path, text: str) -> None:
    _atomic(path, text)


def execute_reconcile(config, venue, *, out=None) -> dict:
    install_demo_guard(venue)
    out_dir = Path(out) if out is not None else Path(config.state_dir).expanduser() / 'verification'
    try:
        with State(config.state_dir, config.scope) as state:
            if hasattr(venue, 'bind_state'):
                venue.bind_state(state)
            report = reconcile_account(config, venue, state)
    except (Blocked, Unknown) as exc:
        report = {
            'status': 'unknown' if isinstance(exc, Unknown) else 'blocked',
            'passed': False,
            'native_execution_verified': False,
            'environment': 'demo',
            'account_uid': config.account_uid,
            'reason': str(exc),
            'mismatches': [],
            'counts': {},
        }
    return write_reconcile(out_dir, report)
