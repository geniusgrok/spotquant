"""Unprotected, independently funded offline spot buy-and-hold/cash references.

These benchmarks use the existing HistoricalVenue/P4Venue prices, fills and
costs. One snapshot and an optional direct market order replace the strategy
session. That distinct decision clock is descriptive, never a shared-session
alpha comparison, a production candidate or protection/native qualification.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import time

from research.complete_spot import PriorFX
from research.rebuild import source_identity
from research.session_account import HistoricalVenue, audit
from spotquant.model import DAY
from spotquant.preview import MIN_NOTIONAL, QUOTE_STEP
from spotquant.state import client_id
from spotquant.types import floor_step, number, serial

POLICIES = {'hold25': D('.25'), 'cash0': D(0)}
CLOCK = dict(execution_path='offline_single_snapshot_direct_submit_v1',
             snapshot_reads=1, read_latency_ms=200, write_latency_ms=1000)


def stamp(day):
    return int(datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() * 1000)


def window_bars(packet, begin, end):
    """Require the prior completed day and every input day before the endpoint.

    The bar opening at end is deliberately excluded: the terminal mark is the
    last covered day's close, rather than a quote from outside the window.
    """
    found = {}
    for key, row in packet['bars'].items():
        t = int(key)
        if not begin - DAY <= t < end:
            continue
        if t in found or t % DAY:
            raise ValueError('duplicate or non-midnight benchmark input')
        o, h, l, c = (number(row[k], 'daily price', positive=True)
                      for k in ('open', 'high', 'low', 'close'))
        if not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError('benchmark OHLC prices are inconsistent')
        found[t] = (t, o, h, l, c, D(0))
    if sorted(found) != list(range(begin - DAY, end, DAY)):
        raise ValueError('benchmark input lacks contiguous end-exclusive coverage')
    return [found[t] for t in sorted(found)]


def _daily(venue):
    if venue.now_ms % DAY:
        raise ValueError('benchmark daily mark is not an actual UTC midnight')
    equity = venue.cash + venue.btc * venue.price
    return dict(timestamp_ms=venue.now_ms, equity_usdt=equity,
                equity_cny=equity * venue.fx(venue.now_ms) * D('.999'),
                cash_usdt=venue.cash, btc=venue.btc, price_usdt=venue.price,
                gross_notional_usdt=venue.btc * venue.price,
                net_notional_usdt=venue.btc * venue.price)


def _owned_audit(venue, account_id, entry_id, spend, receipt_ms):
    """Own every actual venue fill once, including the BTC entry commission."""
    money = audit(venue)
    owners, seen, btc = [], set(), D(0)
    expected_count = 1 if spend else 0
    valid = len(venue.fills) == len(venue.orders) == expected_count
    for trade in venue.fills:
        key = (account_id, trade['id'])
        if key in seen:
            raise ValueError('duplicate benchmark fill ownership')
        seen.add(key)
        order = venue.orders.get(entry_id)
        valid = valid and bool(order and order['status'] == 'FILLED'
            and trade['order_id'] == order['orderId'] and trade['buyer']
            and trade['commission_asset'] == 'BTC' and trade['time'] == receipt_ms
            and abs(trade['quote'] - spend) <= D('1e-8') and order['type'] == 'MARKET'
            and order['side'] == 'BUY')
        net = trade['qty'] - trade['commission']
        btc += net
        owners.append(dict(account_id=account_id, fill_id=trade['id'],
            order_id=trade['order_id'], client_id=entry_id, filled_ms=trade['time'],
            gross_btc=trade['qty'], commission_btc=trade['commission'], owned_btc=net))
    valid = (valid and abs(venue.btc - btc) <= D('1e-8')
             and venue.cash == venue.initial_cash - spend)
    money.update(fill_ownership_passed=bool(valid), independently_funded=True,
                 no_rebalance=True, no_stop_orders=not any(
                     o['type'] == 'STOP_LOSS' for o in venue.orders.values()))
    money['passed'] = money['passed'] and bool(valid) and money['no_stop_orders']
    return money, owners


def measure(policy, bars, starts, fx, *, begin, end, registration, identity):
    """Create one fresh wallet; no runtime state, account adapters or curve scaling."""
    if policy not in POLICIES or policy not in registration['policies']:
        raise ValueError('unregistered benchmark policy')
    if begin % DAY or end % DAY or begin >= end:
        raise ValueError('benchmark window needs increasing UTC midnight boundaries')
    if (not starts or any(type(s) is not int for s in starts)
            or starts != sorted(set(starts)) or any(not begin <= s < end for s in starts)):
        raise ValueError('benchmark requires the original ordered window starts')
    clock = registration['clock']
    day = datetime.fromtimestamp(begin / 1000, timezone.utc).date().isoformat()
    if (any(clock.get(k) != v for k, v in CLOCK.items())
            or clock['first_registered_start_ms'].get(day) != starts[0]):
        raise ValueError('benchmark decision clock differs from its preregistration')
    entry_start = starts[0]
    receipt_ms = entry_start + CLOCK['read_latency_ms'] + (CLOCK['write_latency_ms'] if POLICIES[policy] else 0)
    if receipt_ms >= end:
        raise ValueError('benchmark entry cannot finish inside its end-exclusive window')
    if entry_start // DAY != receipt_ms // DAY:
        raise ValueError('registered direct entry spans a daily reporting boundary')
    # Minimal history is enough here: the benchmark has no daily signal model.
    selected = [r for r in bars if begin - DAY <= r[0] < end]
    if [r[0] for r in selected] != list(range(begin - DAY, end, DAY)):
        raise ValueError('benchmark price coverage is incomplete or unordered')
    initial_cny = D(10000)
    initial_usdt = initial_cny / number(fx(begin), 'prior FX', positive=True) * D('.999')
    account_id = day + '-' + policy + '-unprotected-benchmark'
    venue = HistoricalVenue(selected, begin, initial_usdt, fx)
    daily = [_daily(venue)]
    entry_id = client_id(account_id + ':' + identity['specification_sha256'], entry_start, 'one-entry')
    spend = floor_step(initial_usdt * POLICIES[policy], QUOTE_STEP)
    if POLICIES[policy] and spend < MIN_NOTIONAL:
        raise ValueError('registered benchmark entry is below venue minimum notional')
    # Capture each boundary before entry if the first registered start is later.
    next_day = begin + DAY
    while next_day <= entry_start:
        venue.advance(next_day)
        daily.append(_daily(venue))
        next_day += DAY
    venue.advance(entry_start)
    snapshot = venue.snapshot('1')  # Exactly one original HistoricalVenue read (200ms).
    if (snapshot['btc'] != 0 or snapshot['open_orders'] or snapshot['usdt_free'] != initial_usdt):
        raise ValueError('benchmark cold wallet changed before its only entry')
    decision_ms = venue.now_ms
    if spend:
        venue.submit(entry_id, dict(symbol='BTCUSDT', side='BUY', type='MARKET',
                                    quoteOrderQty=str(spend)))
    held, cash = venue.btc, venue.cash
    while next_day <= end:
        venue.advance(next_day)
        if venue.btc != held or venue.cash != cash:
            raise ValueError('benchmark hold quantities changed without an authorized entry')
        daily.append(_daily(venue))
        next_day += DAY
    money, ownership = _owned_audit(venue, account_id, entry_id, spend, receipt_ms)
    final = daily[-1]
    money['daily_financial_passed'] = all(
        r['equity_usdt'] == r['cash_usdt'] + r['btc'] * r['price_usdt']
        and r['equity_cny'] == r['equity_usdt'] * fx(r['timestamp_ms']) * D('.999')
        and (r['btc'] == 0 if r['timestamp_ms'] <= entry_start else r['btc'] == held)
        for r in daily)
    money['passed'] = money['passed'] and money['daily_financial_passed']
    identity = dict(identity, policy=policy, matcher='OHLC', **CLOCK)
    return serial(dict(case=account_id, policy=policy, identity=identity,
        window=[day, datetime.fromtimestamp(end / 1000, timezone.utc).date().isoformat()],
        initial_cny=initial_cny, initial_usdt=initial_usdt, additional_capital='0',
        initial_state=dict(kind='cold_cash', cash_usdt=initial_usdt, btc='0', orders=[]),
        final_usdt=final['equity_usdt'], final_cny=final['equity_cny'],
        return_cny=final['equity_cny'] / initial_cny - 1,
        cash_usdt=venue.cash, btc=venue.btc, mdd=venue.mdd, audit=money,
        entry_spend_usdt=spend, entry_fraction=POLICIES[policy],
        fills=venue.fills, ownership=ownership, daily=daily,
        client_events=venue.client_events, pending_intents=[], failure=None,
        complete_finite=money['passed'] and venue.now_ms == end,
        sessions=[], session_count=0, registered_session_count=0,
        selected_registered_starts_ms=starts,
        decision_clock=dict(registered=clock, snapshot_sent_ms=entry_start,
            snapshot_received_ms=decision_ms, decision_ms=decision_ms,
            entry_received_ms=receipt_ms if spend else None,
            shared_session_equivalent=False),
        cost_model=dict(fee_rate=venue.fee, market_slippage=venue.slip,
            ingress_conversion_factor='.999', reporting_conversion_factor='.999',
            fx='PriorFX prior-date rates; original fallback'),
        execution_path=CLOCK['execution_path'], protection_policy='none: explicit offline benchmark',
        intended_use='descriptive unprotected buy-and-hold/cash reference only',
        strategy_policy_differs=True, venue_cost_and_price_model_unchanged=True,
        shared_runtime_verified=False, protection_acceptance=False,
        production_candidate=False, qualification='NOT_QUALIFIED', native_verified=False,
        known_path=True, price_model='high-before-low OHLC proxy',
        input_coverage='prior completed day and every window day; end exclusive'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--policy', choices=tuple(POLICIES), required=True)
    parser.add_argument('--begin', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        raise ValueError('preserve the existing benchmark receipt')
    source = source_identity()
    if source['dirty']:
        raise ValueError('freeze benchmark producer source before execution')
    raw_spec = args.spec.read_bytes()
    spec = json.loads(raw_spec)
    windows = dict(spec['windows'])
    if args.begin not in windows or D(spec['capital_cny']) != D(10000):
        raise ValueError('unregistered benchmark window or initial capital')
    begin, end = stamp(args.begin), stamp(windows[args.begin])
    raw_daily = Path(spec['daily_packet']).read_bytes()
    daily_sha = hashlib.sha256(raw_daily).hexdigest()
    if daily_sha != spec['daily_sha256']:
        raise ValueError('qualified daily benchmark input changed')
    raw_schedule = Path(spec['schedule']).read_bytes()
    raw_fx = Path(spec['fx']).read_bytes()
    starts = [s for s in json.loads(raw_schedule)['primary']['starts_ms'] if begin <= s < end]
    identity = dict(source=source, source_head=source['git_head'],
        specification_sha256=hashlib.sha256(raw_spec).hexdigest(), price_sha256=daily_sha,
        schedule_sha256=hashlib.sha256(raw_schedule).hexdigest(),
        fx_sha256=hashlib.sha256(raw_fx).hexdigest())
    fx = PriorFX(spec['fx'])
    started = time.monotonic()
    row = measure(args.policy, window_bars(json.loads(raw_daily), begin, end), starts, fx,
                  begin=begin, end=end, registration=spec['spot_hold_control'], identity=identity)
    row['elapsed_seconds'] = time.monotonic() - started
    if source_identity() != source:
        raise ValueError('benchmark producer changed; preserve output without relabeling')
    if Path(spec['fx']).read_bytes() != raw_fx:
        raise ValueError('prior-date FX changed during benchmark execution')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        stream.write(json.dumps(row, indent=2) + '\n')
    print(json.dumps({k: row[k] for k in ('case', 'return_cny', 'mdd', 'complete_finite',
                                        'elapsed_seconds')}), flush=True)


if __name__ == '__main__':
    main()
