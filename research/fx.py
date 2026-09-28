"""Dated CNY per USD. Ex-post valuation only; never a trading input.

FRED DEXCHUS (H.10 noon New York buying rate) applies from 17:00 UTC on its
observation date. H.10 is published weekly, so a rate was not necessarily
readable at that instant. USDT is valued at par with USD. Conversion cost
is charged by the account meter, not here.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'evidence' / 'rebuild-20260928' / 'inputs' / 'DEXCHUS.csv'
SHA256 = '733c2bbccfd42448d72f8b7a7ee2cd744f1b3263ac1be88e34260b9c7b2ec874'
OBSERVED_UTC_HOUR = 17
BASIS = ('FRED DEXCHUS, ex-post: each observation applies from 17:00 UTC of its date; '
         'USDT at USD par; conversion charged separately')


class DatedFX:
    def __init__(self, path=PATH):
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != SHA256:
            raise ValueError('DEXCHUS input changed')
        self.times: list[int] = []
        self.rates: list[D] = []
        for row in csv.DictReader(raw.decode().splitlines()):
            if row['DEXCHUS'] in ('', '.'):
                continue
            day = datetime.strptime(row['observation_date'], '%Y-%m-%d').replace(
                hour=OBSERVED_UTC_HOUR, tzinfo=timezone.utc)
            self.times.append(int(day.timestamp() * 1000))
            self.rates.append(D(row['DEXCHUS']))

    def __call__(self, now_ms: int) -> D:
        index = bisect.bisect_right(self.times, int(now_ms)) - 1
        if index < 0:
            raise ValueError('no CNY observation before this instant')
        return self.rates[index]
