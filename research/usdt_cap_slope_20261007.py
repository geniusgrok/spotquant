"""One-shot, append-only public USDT market-cap receipt and causal 3-day slope.

Research-only. No account or scheduled collection. Python standard library only.
"""
from __future__ import annotations

import argparse
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

DAY = 86_400_000
URL = ('https://api.coingecko.com/api/v3/coins/tether/market_chart'
       '?vs_currency=usd&days=7&interval=daily&precision=full')
VERSION = 'coingecko-usdt-market-chart-keyless-v1'
MAX_BYTES = 128_000


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def daily_pairs(raw: bytes) -> tuple[dict[int, tuple[D, D]], int]:
    """Match market cap and USD price by exact UTC-midnight event timestamp."""
    obj = json.loads(raw, parse_float=D)
    if not isinstance(obj, dict):
        raise ValueError('market-chart response must be an object')
    maps = {}
    ignored = 0
    for name in ('market_caps', 'prices'):
        rows = obj.get(name)
        if not isinstance(rows, list):
            raise ValueError(f'{name} missing')
        found = {}
        prior = -1
        for row in rows:
            if (not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int
                    or row[0] <= prior):
                raise ValueError(f'{name} event timestamps are invalid or out of order')
            stamp = row[0]
            prior = stamp
            if stamp % DAY:
                ignored += 1
                continue
            value = D(str(row[1]))
            if not value.is_finite() or value <= 0:
                raise ValueError(f'{name} has a nonpositive daily value')
            found[stamp] = value
        maps[name] = found
    caps, prices = maps['market_caps'], maps['prices']
    if set(caps) != set(prices):
        raise ValueError('cap and USD price daily event times differ')
    return {stamp: (caps[stamp], prices[stamp]) for stamp in caps}, ignored


def receipts(directory: Path) -> list[tuple[dict, bytes, bytes]]:
    result = []
    for path in directory.glob('*.receipt.json'):
        raw_meta = path.read_bytes()
        meta = json.loads(raw_meta)
        if (meta.get('format') != 1 or meta.get('source_version') != VERSION
                or meta.get('url') != URL or type(meta.get('received_ms')) is not int
                or not isinstance(meta.get('body_file'), str)
                or Path(meta['body_file']).name != meta['body_file']):
            raise ValueError(f'foreign or invalid receipt: {path.name}')
        body = (directory / meta['body_file']).read_bytes()
        if digest(body) != meta.get('body_sha256'):
            raise ValueError(f'raw response hash mismatch: {path.name}')
        result.append((meta, raw_meta, body))
    result.sort(key=lambda row: (row[0]['received_ms'], digest(row[1])))
    previous = None
    for meta, raw_meta, _ in result:
        if meta.get('previous_receipt_sha256') != previous:
            raise ValueError('receipt revision chain is broken')
        previous = digest(raw_meta)
    return result


def capture(directory: Path) -> dict:
    """Make one public GET; save the raw response, including a non-200 response."""
    directory.mkdir(parents=True, exist_ok=True)
    prior = receipts(directory)
    request_ms = time.time_ns() // 1_000_000
    req = urllib.request.Request(URL, headers={'User-Agent': 'spotquant-research/1.0'})
    try:
        response = urllib.request.urlopen(req, timeout=12)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status = response.status
        final_url = response.url
        headers = {key: response.headers.get(key) for key in
                   ('Date', 'Content-Type', 'ETag', 'Last-Modified', 'Cache-Control')}
        body = response.read(MAX_BYTES + 1)
    received_ms = time.time_ns() // 1_000_000
    if len(body) > MAX_BYTES:
        raise ValueError('response exceeds the fixed one-request byte bound')
    if final_url != URL:
        raise ValueError('source redirected; no receipt accepted')
    older = prior[-1] if prior else None
    revisions = []
    points = {}
    ignored = 0
    if status == 200:
        points, ignored = daily_pairs(body)
        before = daily_pairs(older[2])[0] if older and older[0]['http_status'] == 200 else {}
        for stamp in sorted(set(points) & set(before)):
            if points[stamp] != before[stamp]:
                revisions.append({'event_ms': stamp,
                                  'previous_value_sha256': digest(f'{stamp}|{before[stamp]}'.encode()),
                                  'new_value_sha256': digest(f'{stamp}|{points[stamp]}'.encode())})
    body_name = f'{received_ms}-{digest(body)[:12]}.raw.json'
    meta_name = f'{received_ms}-{digest(body)[:12]}.receipt.json'
    meta = {
        'format': 1, 'source_version': VERSION, 'url': URL,
        'request_started_ms': request_ms, 'received_ms': received_ms,
        'http_status': status, 'response_headers': headers,
        'body_file': body_name, 'body_sha256': digest(body), 'bytes': len(body),
        'previous_receipt_sha256': digest(older[1]) if older else None,
        'daily_points': len(points), 'ignored_nonmidnight_values': ignored,
        'revisions': revisions, 'data_era': 'first observed on receipt; no historical PIT claim',
    }
    with (directory / body_name).open('xb') as stream:
        stream.write(body)
    with (directory / meta_name).open('xb') as stream:
        stream.write((json.dumps(meta, sort_keys=True, separators=(',', ':')) + '\n').encode())
    return {'receipt': meta_name, 'http_status': status, 'body_sha256': meta['body_sha256'],
            'received_ms': received_ms, 'daily_points': len(points),
            'ignored_nonmidnight_values': ignored, 'revisions': len(revisions)}


def signal(directory: Path, decision_ms: int) -> dict:
    """Use only a receipt actually received before a supplied manual decision."""
    eligible = [item for item in receipts(directory)
                if item[0]['http_status'] == 200 and item[0]['received_ms'] <= decision_ms]
    if not eligible:
        return {'status': 'MISSING_ASOF_RECEIPT'}
    meta, _, body = eligible[-1]
    points, ignored = daily_pairs(body)
    available = [stamp for stamp in points
                 if stamp + 600_000 <= meta['received_ms'] <= decision_ms]
    if not available:
        return {'status': 'MISSING_COMPLETED_DAILY_POINT'}
    latest = max(available)
    if not decision_ms < latest + DAY + 600_000:
        return {'status': 'STALE_DAILY_POINT', 'latest_event_ms': latest}
    stamps = (latest - 2 * DAY, latest - DAY, latest)
    if any(stamp not in points for stamp in stamps):
        return {'status': 'MISSING_CONSECUTIVE_DAILY_POINTS', 'latest_event_ms': latest}
    # Each selected value is the one preserved in the as-of raw response, never
    # a subsequently finalized value from a later download.
    caps = [points[stamp][0] for stamp in stamps]
    prices = [points[stamp][1] for stamp in stamps]
    supply = [cap / price for cap, price in zip(caps, prices)]
    slope = (supply[2].ln() - supply[0].ln()) / 2
    cap_slope = (caps[2].ln() - caps[0].ln()) / 2
    price_slope = (prices[2].ln() - prices[0].ln()) / 2
    return {'status': 'SOURCE_QUALIFIED_ONLY', 'latest_event_ms': latest,
            'receipt_received_ms': meta['received_ms'],
            'receipt_body_sha256': meta['body_sha256'],
            'event_ms': stamps, 'supply_log_slope_per_day': str(slope),
            'market_cap_log_slope_per_day': str(cap_slope),
            'usdt_usd_log_slope_per_day': str(price_slope),
            'negative_supply_slope': slope < 0,
            'ignored_nonmidnight_values': ignored,
            'manual_new_buy_verified': False, 'future_outcome_mature': False}


def selfcheck() -> None:
    import tempfile
    day = 1_800_000_000_000 // DAY * DAY
    def body(caps, prices, omit=()):
        return json.dumps({'market_caps': [[day + i*DAY, caps[i]] for i in range(3) if i not in omit],
                           'prices': [[day + i*DAY, prices[i]] for i in range(3) if i not in omit],
                           'total_volumes': []}, separators=(',', ':')).encode()
    def saved(path, raw, received, previous=None):
        name=f'{received}.raw.json'
        (path/name).write_bytes(raw)
        meta={'format': 1, 'source_version': VERSION, 'url': URL,
              'request_started_ms': received-100, 'received_ms': received,
              'http_status': 200, 'body_file': name, 'body_sha256': digest(raw),
              'previous_receipt_sha256': previous}
        m=(json.dumps(meta,sort_keys=True)+'\n').encode()
        (path/f'{received}.receipt.json').write_bytes(m)
        return digest(m)
    with tempfile.TemporaryDirectory() as location:
        path=Path(location)
        raw=body([100,95,90],[1,1,1])
        first=saved(path,raw,day+2*DAY+700_000)
        assert signal(path,day+2*DAY+699_999)['status']=='MISSING_ASOF_RECEIPT'
        item=signal(path,day+2*DAY+800_000)
        assert item['status']=='SOURCE_QUALIFIED_ONLY' and item['negative_supply_slope']
        assert signal(path,day+3*DAY+600_000)['status']=='STALE_DAILY_POINT'
        revision=body([100,95,100],[1,1,1])
        saved(path,revision,day+2*DAY+900_000,first)
        assert signal(path,day+2*DAY+800_000)['negative_supply_slope']
        assert not signal(path,day+2*DAY+950_000)['negative_supply_slope']
        assert daily_pairs(body([100,95,90],[1,1,1],omit=(1,)))[0].get(day+DAY) is None
        try:
            daily_pairs(b'{"prices":[[0,1],[0,1]],"market_caps":[]}')
            raise AssertionError('duplicate timestamps accepted')
        except ValueError:
            pass
    print('selfcheck PASS: as-of, revision, freshness, pairing, duplicate')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('capture')
    c.add_argument('directory', type=Path)
    s = sub.add_parser('signal')
    s.add_argument('directory', type=Path)
    s.add_argument('--decision-ms', type=int, required=True)
    sub.add_parser('selfcheck')
    args = parser.parse_args()
    if args.command == 'capture':
        result = capture(args.directory)
    elif args.command == 'signal':
        result = signal(args.directory, args.decision_ms)
    else:
        selfcheck()
        raise SystemExit(0)
    print(json.dumps(result, sort_keys=True))
