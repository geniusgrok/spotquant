"""Fetch only absent 2017 warmup/2018 public days; preserve existing caches."""
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile

from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener

DAY = 86400000
ROOT = Path('/workspace/btc-history-2018-2019-20261005')
OLD = Path('/workspace/btc-search-next-20261005/daily-composition.json')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def fetch(item):
    name, url = item
    receipt = dict(file=name, url=url, request_utc=now(), TLS_verification=True,
                   inherited_proxy=True, retry=False, private_request=False)
    try:
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            response = build_opener(NoRedirect).open(url, timeout=30)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(3000001)
        receipt.update(status=response.code, receipt_utc=now(), bytes=len(raw),
                       sha256=sha(raw))
        (ROOT/'raw'/name).write_bytes(raw)
    except (URLError, OSError) as exc:
        receipt.update(receipt_utc=now(), error_type=type(exc).__name__, reason=str(exc))
    return receipt


def main():
    started = time.monotonic()
    spec_raw = (ROOT/'spec.json').read_bytes()
    if (ROOT/'data-receipt.json').exists():
        raise ValueError('preserve original capture; do not repeat')
    (ROOT/'raw').mkdir()
    months = [f'2017-{m:02d}' for m in range(9, 13)] + [f'2018-{m:02d}' for m in range(1, 13)]
    jobs = []
    for month in months:
        name = f'BTCUSDT-1d-{month}.zip'
        url = 'https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/'+name
        jobs += [(name, url), (name+'.CHECKSUM', url+'.CHECKSUM')]
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(fetch, jobs))
    b, e = 1567296000000, 1577836800000
    future_urls = [
        ('futures-2019-4h.json', f'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=4h&startTime={b}&endTime={e-1}&limit=1000'),
        ('futures-2019-funding.json', f'https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&startTime={b}&endTime={e-1}&limit=1000')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts += list(pool.map(fetch, future_urls))
    (ROOT/'http-receipts.json').write_text(json.dumps(receipts, indent=2)+'\n')
    if sum(r.get('bytes', 0) for r in receipts) > 3000000:
        raise ValueError('registered bytes budget exceeded; raw retained')
    old_raw = OLD.read_bytes()
    old = json.loads(old_raw)
    bars = old['bars'].copy()
    sources = []
    for month in months:
        name = f'BTCUSDT-1d-{month}.zip'
        rs = [r for r in receipts if r['file'] in (name, name+'.CHECKSUM')]
        if len(rs) != 2 or any(r.get('status') != 200 for r in rs):
            raise ValueError('fixed spot window unavailable: '+month)
        raw = (ROOT/'raw'/name).read_bytes()
        checksum = (ROOT/'raw'/(name+'.CHECKSUM')).read_text().split()[0]
        if checksum != sha(raw):
            raise ValueError('official checksum mismatch '+name)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if not n.endswith('/')]
            if len(names) != 1:
                raise ValueError('ambiguous original zip')
            for row in csv.reader(io.StringIO(archive.read(names[0]).decode())):
                if not row[0].isdigit():
                    continue
                t, end = int(row[0]), int(row[6])
                if t > 10**14:
                    t, end = t//1000, end//1000
                o, h, l, c, base, quote, taker = map(D, [row[1], row[2], row[3], row[4], row[5], row[7], row[10]])
                count = int(row[8])
                if not (t % DAY == 0 and end == t+DAY-1 and 0 < l <= min(o, c) <= max(o, c) <= h
                        and base > 0 and quote > 0 and count > 0 and 0 <= taker <= quote):
                    raise ValueError('invalid official daily row '+name)
                record = dict(open=o, high=h, low=l, close=c, base_volume=base,
                              quote_volume=quote, trade_count=count, taker_buy_quote=taker,
                              avg_quote_per_trade=quote/count)
                record = {k: str(v) if isinstance(v, D) else v for k, v in record.items()}
                if str(t) in bars and bars[str(t)] != record:
                    raise ValueError('conflicting prior daily cache')
                bars[str(t)] = record
        sources.append(dict(file=name, sha256=checksum, bytes=len(raw)))
    missing = {}
    for year in (2018, 2019):
        start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()*1000)
        end = int(datetime(year+1, 1, 1, tzinfo=timezone.utc).timestamp()*1000)
        absent = [t for t in range(start, end, DAY) if str(t) not in bars]
        missing[str(year)] = absent
        if absent:
            raise ValueError('daily signal gap; no interpolation')
    packet = dict(format='btc-joined-history-days-v1', bars=bars,
                  old_cache_path=str(OLD), old_cache_sha256=sha(old_raw), new_sources=sources,
                  availability='completed UTC daily end+60000ms modeled development clock, not historical receipt vintage')
    joined_raw = (json.dumps(packet, indent=2)+'\n').encode()
    (ROOT/'days.json').write_bytes(joined_raw)
    futures = {}
    for name, _ in future_urls:
        r = next(r for r in receipts if r['file'] == name)
        values = None
        if r.get('status') == 200:
            values = json.loads((ROOT/'raw'/name).read_text())
        futures[name] = dict(status=r.get('status'), raw_sha256=r.get('sha256'),
                             rows=len(values) if isinstance(values, list) else None,
                             first_actual_ms=(int(values[0][0]) if name.endswith('4h.json') else int(values[0]['fundingTime']))
                             if isinstance(values, list) and values else None,
                             account_qualified=False,
                             limit='Trade/funding existence only; official minute mark and actual print execution coverage not acquired.')
    receipt = dict(spec_sha256=sha(spec_raw), producer_sha256=sha(Path(__file__).read_bytes()),
                   parsed_sha256=sha(joined_raw), parsed_bytes=len(joined_raw), old_cache_sha256=sha(old_raw),
                   spot_status='QUALIFIED_DAILY_DIRECTION_INPUT', new_year_rows={'2018':365, '2019':365},
                   missing=missing, total_daily_rows=len(bars), futures=futures,
                   public_GET=len(receipts), raw_bytes=sum(r.get('bytes', 0) for r in receipts),
                   elapsed_seconds=time.monotonic()-started, large_vault_scans=0, account_runs=0,
                   limitations='New2018 added;2019 was already cached development history. No revised-history OOS or prelisting futures wallet proof.')
    (ROOT/'data-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
