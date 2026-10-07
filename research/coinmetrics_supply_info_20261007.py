"""Pinned, conditional Coin Metrics USDT supply information screen; no account replay."""

import argparse
from datetime import datetime, time, timezone
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
import zipfile

from research.coinmetrics_usdt_source_overlap_20261007 import coinmetrics
from research.continuous_routes import solve


DAY = 86_400_000
LAG = 2 * DAY + 600_000  # Frozen 48 hours + 10 minutes; not proved historical PIT.
ACCOUNT_SHA = '4c937ecf80f60d957486a752562c8ab8dfee4c06fa2e5b38b135cfb24ec38872'
SEARCH_SHA = 'c9f9b30ab5f5a551a9a9bf5e9bd99ced3dc934b50d116eee1d9f5d59ff941cc3'
DAILY_SHA = '6a35dadcbe9228d95191a2d2716bc2c2d9f01aa8be08638718325fbefb376009'


def checked(path, digest):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('pinned input changed')
    return raw


def inputs(account_path, search_path, cm_path, receipt_path):
    account = json.loads(gzip.decompress(checked(account_path, ACCOUNT_SHA)))
    if account['source']['git_head'] != '74bd6e035e36531c517029077c1a9e2e5ec44516' or account['source']['dirty']:
        raise ValueError('original account identity differs')
    result = account['results']['crowding-interaction-base']
    if not result['complete'] or not result['audit']['passed'] or result['execution_unresolved_sessions']:
        raise ValueError('original account not complete')
    with zipfile.ZipFile(search_path) as archive:
        if hashlib.sha256(Path(search_path).read_bytes()).hexdigest() != SEARCH_SHA:
            raise ValueError('BTC search ZIP changed')
        daily = archive.read('daily-composition.json')
    if hashlib.sha256(daily).hexdigest() != DAILY_SHA:
        raise ValueError('BTC daily bars changed')
    bars = json.loads(daily)['bars']
    cm, receipt = coinmetrics(cm_path, receipt_path)
    supply = {}
    for day, (cap, price, units) in cm.items():
        stamp = int(datetime.combine(day, time.min, timezone.utc).timestamp() * 1000)
        supply[stamp] = (cap, price, units)
    return result, bars, supply, receipt


def support(result, bars, supply, receipt):
    allocations = {}
    for record in result['allocations']:
        intent = json.loads(record[1])
        if intent.get('order', {}).get('side') != 'BUY' or record[2] != 'settled':
            continue
        signal = intent['signal_ms']
        if signal in allocations:
            raise ValueError('duplicate settled external BUY allocation')
        response = json.loads(record[3])
        if response.get('side') != 'BUY' or response.get('clientOrderId') != record[0]:
            raise ValueError('BUY allocation/order identity differs')
        allocations[signal] = (intent['order'], response['orderId'])
    original = []
    for row in result['opportunity_ledger']:
        if row.get('event') != 'decision':
            continue
        buys = [o for o in row['accepted_orders'] if o.get('side') == 'BUY' and o.get('type') == 'MARKET']
        if buys:
            if len(buys) != 1:
                raise ValueError('multiple external BUY orders in manual decision')
            original.append(row)
    original.sort(key=lambda row: row['decision_ms'])
    unique = {}
    for row in original:
        day = row['completed_bar_ms']
        if day in unique:
            before = [o for o in unique[day]['accepted_orders'] if o.get('side') == 'BUY']
            after = [o for o in row['accepted_orders'] if o.get('side') == 'BUY']
            if before != after:
                raise ValueError('conflicting accepted BUY for one signal')
            continue
        unique[day] = row
    original = list(unique.values())
    rejected = {key: 0 for key in ('outside_source_calendar', 'missing_three_source_days',
                                  'missing_btc_controls', 'missing_seven_day_endpoint', 'missing_fill')}
    aligned = []
    for row in original:
        decision, btc_day = row['decision_ms'], row['completed_bar_ms']
        if decision >= receipt['received_ms']:
            raise ValueError('archived decision after actual source receipt')
        source_day = (decision - LAG) // DAY * DAY
        if source_day < min(supply) or source_day > max(supply):
            rejected['outside_source_calendar'] += 1
            continue
        if not all(source_day - i * DAY in supply for i in (0, 1, 2)):
            rejected['missing_three_source_days'] += 1
            continue
        if not all(str(btc_day - i * DAY) in bars for i in range(21)):
            rejected['missing_btc_controls'] += 1
            continue
        if any(D(bars[str(btc_day - i * DAY)]['close']) <= 0 for i in range(21)):
            raise ValueError('BTC control close invalid')
        if str(btc_day + 7 * DAY) not in bars or btc_day + 8 * DAY + 60_000 > receipt['received_ms']:
            rejected['missing_seven_day_endpoint'] += 1
            continue
        owned = allocations.get(btc_day)
        accepted = [o for o in row['accepted_orders'] if o.get('side') == 'BUY']
        if (owned is None or len(accepted) != 1
                or any(owned[0].get(key) != accepted[0].get(key)
                       for key in ('symbol', 'side', 'type', 'quoteOrderQty'))
                or not any(fill['buyer'] and fill['order_id'] == owned[1]
                           and decision < fill['time'] < decision + 60_000 for fill in result['fills'])):
            rejected['missing_fill'] += 1
            continue
        aligned.append((row, source_day))
    independent = []
    for row, source_day in aligned:
        if not independent or row['decision_ms'] >= independent[-1][0]['decision_ms'] + 7 * DAY:
            independent.append((row, source_day))
    halves = (len(independent) // 2, len(independent) - len(independent) // 2)
    report = dict(source_id=receipt['source_id'], source_sha256=receipt['body_sha256'],
                  historical_pit=False, strict_pit_eligible_buy=0,
                  availability_assumption='event+48h+10m; no historical receipt proof',
                  original_accepted_buy_count=len(original), calendar_aligned=len(aligned),
                  independent_seven_day_count=len(independent), chronological_half_counts=halves,
                  rejected=rejected, minimum_support_met=len(independent) >= 10 and min(halves) >= 3,
                  slope_signs_or_outcomes_inspected=False, wallet_run=False)
    return report, independent


def information(independent, bars, supply):
    if len(independent) < 10 or min(len(independent) // 2, len(independent) - len(independent) // 2) < 3:
        raise ValueError('frozen support gate not met')
    events = []
    cost = D('.0037')
    for row, source_day in independent:
        btc_day = row['completed_bar_ms']
        s0, s2 = supply[source_day - 2 * DAY][2], supply[source_day][2]
        p0, p2 = supply[source_day - 2 * DAY][1], supply[source_day][1]
        negative = (s2.ln() - s0.ln()) / 2 < 0
        price_slope = (p2.ln() - p0.ln()) / 2
        closes = [D(bars[str(btc_day - i * DAY)]['close']) for i in range(20, -1, -1)]
        returns = [b / a - 1 for a, b in zip(closes, closes[1:])]
        gross = D(bars[str(btc_day + 7 * DAY)]['close']) / closes[-1] - 1
        events.append(dict(negative=negative, price_slope=float(price_slope),
                           momentum=float(closes[-1] / closes[0] - 1),
                           rms=float((sum((v * v for v in returns), D(0)) / 20).sqrt()),
                           gross=float(gross)))

    def fit(rows):
        neg = [e for e in rows if e['negative']]
        ref = [e for e in rows if not e['negative']]
        result = dict(count=len(rows), negative_count=len(neg), reference_count=len(ref),
                      negative_fraction=len(neg) / len(rows),
                      negative_mean_net7=sum(e['gross'] - float(cost) for e in neg) / len(neg) if neg else None,
                      reference_mean_net7=sum(e['gross'] - float(cost) for e in ref) / len(ref) if ref else None,
                      reference_mean_net7_double_cost=sum(e['gross'] - float(2 * cost) for e in ref) / len(ref) if ref else None,
                      coefficient=None, identifiable=False)
        if len(neg) < 3 or len(ref) < 3 or len(rows) <= 5:
            return result
        xs = [[1., float(e['negative']), e['momentum'], e['rms'], e['price_slope']] for e in rows]
        ys = [e['gross'] - float(cost) for e in rows]
        try:
            coeff = solve([[sum(x[i] * x[j] for x in xs) for j in range(5)] for i in range(5)],
                          [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(5)])
        except ValueError:
            return result
        result.update(coefficient=coeff[1], identifiable=True)
        return result

    midpoint = len(events) // 2
    overall, first, second = fit(events), fit(events[:midpoint]), fit(events[midpoint:])
    passed = (all(x['identifiable'] for x in (overall, first, second))
              and overall['coefficient'] <= -float(cost)
              and all(x['coefficient'] < 0 and x['negative_mean_net7'] < 0
                      and x['reference_mean_net7_double_cost'] > 0 for x in (first, second)))
    return dict(status='INFORMATION_ENTRANT_ACCOUNT_NOT_RUN' if passed else 'CLOSE_SOURCE_EXPRESSION',
                source='conditional_history_not_PIT', label='seven_day_BTCUSDT_close_net_37bp_proxy',
                controls=['prior_BTC_momentum20', 'prior_BTC_RMS20', 'CoinMetrics_PriceUSD_slope3'],
                overall=overall, first_half=first, second_half=second,
                fixed_information_gate_passed=passed, account_replay=False,
                strict_pit=False, prospective=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=('support', 'information'))
    p.add_argument('account', type=Path)
    p.add_argument('search_zip', type=Path)
    p.add_argument('cm_raw', type=Path)
    p.add_argument('cm_receipt', type=Path)
    args = p.parse_args()
    result, bars, supply, receipt = inputs(args.account, args.search_zip, args.cm_raw, args.cm_receipt)
    report, independent = support(result, bars, supply, receipt)
    if args.stage == 'support':
        print(json.dumps(report, indent=2))
    elif report['minimum_support_met']:
        print(json.dumps(information(independent, bars, supply), indent=2))
    else:
        raise ValueError('support gate failed; slope signs and outcomes not inspected')


if __name__ == '__main__':
    main()
