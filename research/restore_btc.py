"""Restore frozen official BTC daily inputs and separately refresh forward archives."""
from __future__ import annotations

import argparse
import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def fetch(path, url):
    checksum = Path(str(path) + '.CHECKSUM')
    if not checksum.exists():
        raw = urlopen(Request(url + '.CHECKSUM', headers={'User-Agent': 'spotquant-research'}), timeout=30).read()
    else:
        raw = checksum.read_bytes()
    expected = raw.decode().split()[0]
    if len(expected) != 64 or any(char not in '0123456789abcdef' for char in expected):
        raise ValueError('invalid official archive checksum')
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'existing input is corrupt; refusing to overwrite {path.name}')
    else:
        data = urlopen(Request(url, headers={'User-Agent': 'spotquant-research'}), timeout=30).read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'official checksum mismatch for {path.name}')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.part-' + uuid.uuid4().hex)
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if not checksum.exists():
        checksum.write_bytes(raw)
    return path.name


def restore(market, forward, through=None):
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    through = yesterday if through is None else through
    if not date(2026, 9, 19) <= through <= yesterday:
        raise ValueError('through must be a completed date at or after 2026-09-19')
    jobs = []
    for year in range(2019, 2027):
        for month in range(1, 13):
            if (year, month) > (2026, 8):
                break
            name = f'BTCUSDT-1d-{year}-{month:02d}.zip'
            jobs.append((market / '1d' / name,
                         'https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/' + name))
    day = date(2026, 9, 1)
    while day <= through:
        name = f'BTCUSDT-1d-{day}.zip'
        root = market if day <= date(2026, 9, 19) else forward
        jobs.append((root / '1d/daily' / name,
                     'https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1d/' + name))
        day += timedelta(days=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        names = list(pool.map(lambda job: fetch(*job), jobs))
    return {'verified_archives': len(names), 'frozen_market': str(market), 'forward_market': str(forward),
            'through': str(through)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market', type=Path, default=Path('/tmp/spotquant-market/klines'))
    parser.add_argument('--forward', type=Path, default=Path('/tmp/spotquant-market/forward'))
    parser.add_argument('--through', type=date.fromisoformat)
    args = parser.parse_args(argv)
    print(restore(args.market, args.forward, args.through))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
