"""Load official Binance spot daily klines. The files are not committed.

Monthly vision zips live under ``{root}/1d/{SYMBOL}-1d-YYYY-MM.zip``.
Days after the last monthly file live under ``{root}/1d/daily/``.
Open times published in microseconds are scaled back to milliseconds.
"""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
import zipfile

from spotquant.model import DAY, ORIGIN

# Binance vision CHECKSUM companion is ``<name>.zip.CHECKSUM`` with ``sha256  name``.


def _millis(raw: str) -> int:
    value = int(raw)
    return value // 1000 if value > 10**14 else value


def _read_zip(path: Path, into: dict):
    with zipfile.ZipFile(path) as archive:
        text = archive.read(archive.namelist()[0]).decode()
    for line in text.splitlines():
        if not line or not line[0].isdigit():
            continue
        parts = line.split(',')
        open_time = _millis(parts[0])
        if open_time % DAY:
            continue
        into[open_time] = (D(parts[1]), D(parts[2]), D(parts[3]), D(parts[4]), D(parts[7]))


def _zips(root: Path, symbol: str) -> list[Path]:
    base = Path(root) / '1d'
    return sorted(base.glob(f'{symbol}-1d-*.zip')) + sorted((base / 'daily').glob(f'{symbol}-1d-*.zip'))


def load_daily(root: Path, end_ms: int, symbol: str = 'BTCUSDT',
               extra=()) -> list[tuple[int, D, D, D, D, D]]:
    """Return (open_ms, open, high, low, close, quote_volume) from ORIGIN up to end_ms.

    ``extra`` lists more roots whose files add later days to the same series.
    """
    found: dict[int, tuple[D, D, D, D, D]] = {}
    zips = _zips(root, symbol)
    for other in extra:
        zips += _zips(other, symbol)
    if not zips:
        raise FileNotFoundError(f'no {symbol} 1d zips under {Path(root) / "1d"}')
    for path in zips:
        _read_zip(path, found)
    times = [item for item in sorted(found) if ORIGIN <= item < end_ms]
    if not times or times[0] != ORIGIN:
        raise ValueError('daily history does not include the 2019-01-01 origin')
    for previous, current in zip(times, times[1:]):
        if current - previous != DAY:
            raise ValueError(f'missing daily bar after {previous}')
    return [(item, *found[item]) for item in times]


def file_digest(root: Path, symbol: str = 'BTCUSDT', extra=()) -> str:
    import hashlib
    digest = hashlib.sha256()
    paths = _zips(root, symbol)
    for other in extra:
        paths += _zips(other, symbol)
    for path in paths:
        digest.update(path.name.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
