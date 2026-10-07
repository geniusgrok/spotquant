"""Local-only Coin Metrics source/overlap check. No trading outcome or raw upload.

Coin Metrics Community material is marked CC BY-NC 4.0; this script does not
license trading use or make its historical values point-in-time observations.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import statistics

SOURCE_ID = 'coinmetrics_community_usdt_capest_price_v1'
CM_SHA = '18d511062eb636677960843006325a7f448f176faca0fbcddb8a9459ae4f99da'
CG_SHA = '5585da24ce972871b71173484235366207f4f9186d29539da747516993249894'
CM_URL = ('https://community-api.coinmetrics.io/v4/timeseries/asset-metrics'
          '?assets=usdt&metrics=CapMrktEstUSD,PriceUSD&frequency=1d'
          '&start_time=2020-01-01&end_time=2026-09-30&page_size=10000')
DAY_MS = 86_400_000


def checked(path, expected):
    body = Path(path).read_bytes()
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError('source bytes differ from fixed receipt')
    return json.loads(body)


def coinmetrics(raw, receipt):
    meta = json.loads(Path(receipt).read_bytes())
    if (meta['source_id'] != SOURCE_ID or meta['url'] != CM_URL
            or meta['body_sha256'] != CM_SHA or meta['http_status'] != 200
            or meta['historical_first_receipt'] is not None or meta['pit'] is not False):
        raise ValueError('Coin Metrics source identity or receipt differs')
    doc = checked(raw, CM_SHA)
    if set(doc) != {'data'} or not isinstance(doc['data'], list):
        raise ValueError('missing data or unexpected pagination')
    start, end = date(2020, 1, 1), date(2026, 9, 30)
    expected = (end - start).days + 1
    if len(doc['data']) != expected:
        raise ValueError('missing or excess Coin Metrics days')
    out = {}
    for index, row in enumerate(doc['data']):
        day = start + timedelta(days=index)
        if (set(row) != {'asset', 'time', 'CapMrktEstUSD', 'PriceUSD'}
                or row['asset'] != 'usdt'
                or row['time'] != day.isoformat() + 'T00:00:00.000000000Z'):
            raise ValueError('row asset, ordering, field or UTC label differs')
        cap, price = D(row['CapMrktEstUSD']), D(row['PriceUSD'])
        if not cap.is_finite() or not price.is_finite() or cap <= 0 or price <= 0:
            raise ValueError('null or invalid daily metric')
        out[day] = (cap, price, cap / price)
    return out, meta


def coingecko(raw, receipt):
    meta = json.loads(Path(receipt).read_bytes())
    if meta['body_sha256'] != CG_SHA or meta['http_status'] != 200:
        raise ValueError('CoinGecko fixed calendar receipt differs')
    doc = checked(raw, CG_SHA)
    maps = []
    for key in ('market_caps', 'prices'):
        rows = doc[key]
        values = {}
        prior = -1
        for stamp, value in rows:
            if type(stamp) is not int or stamp <= prior:
                raise ValueError('CoinGecko daily order differs')
            prior = stamp
            if stamp % DAY_MS:
                continue
            day = datetime.fromtimestamp(stamp / 1000, timezone.utc).date()
            value = D(str(value))
            if not value.is_finite() or value <= 0 or day in values:
                raise ValueError('CoinGecko daily value differs')
            values[day] = value
        maps.append(values)
    caps, prices = maps
    if set(caps) != set(prices):
        raise ValueError('CoinGecko cap/price UTC date mismatch')
    return {day: (caps[day], prices[day], caps[day] / prices[day]) for day in caps}


def summary(cm, cg):
    overlap = sorted(set(cm) & set(cg))
    if not overlap:
        raise ValueError('no exact UTC-day overlap')
    def pct_differences(field, shift=0):
        diffs = []
        for day in overlap:
            other = cg.get(day + timedelta(days=shift))
            if other is not None:
                diffs.append(abs(float(cm[day][field] / other[field] - 1)) * 100)
        return diffs
    def stats(values):
        values = sorted(values)
        return dict(count=len(values), median_abs_pct=statistics.median(values),
                    p95_abs_pct=values[int(.95 * (len(values)-1))],
                    max_abs_pct=values[-1])
    return dict(source_id=SOURCE_ID, cm_days=len(cm), cg_days=len(cg),
                exact_utc_day_overlap=len(overlap),first_overlap=overlap[0].isoformat(),
                last_overlap=overlap[-1].isoformat(),
                same_day_supply_within_1e_minus_12_fraction=sum(
                    abs(cm[day][2] / cg[day][2] - 1) <= D('1e-12')
                    for day in overlap) / len(overlap),
                same_day_cap=stats(pct_differences(0)),
                same_day_price=stats(pct_differences(1)),
                same_day_implied_supply=stats(pct_differences(2)),
                cm_vs_cg_prior_day_supply=stats(pct_differences(2, -1)),
                cm_vs_cg_next_day_supply=stats(pct_differences(2, 1)),
                independent_source_validation=False, historical_pit=False,
                trading_use_license_confirmed=False)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('cm_raw', 'cm_receipt', 'cg_raw', 'cg_receipt'):
        p.add_argument(key, type=Path)
    args = p.parse_args()
    cm, _ = coinmetrics(args.cm_raw, args.cm_receipt)
    cg = coingecko(args.cg_raw, args.cg_receipt)
    print(json.dumps(summary(cm, cg), indent=2))
