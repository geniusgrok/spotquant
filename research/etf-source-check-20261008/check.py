"""Verify the single dated Farside receipt and the already frozen five-day sign rule."""

import argparse
from decimal import Decimal
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path


FUNDS = ('IBIT', 'FBTC', 'BITB', 'ARKB', 'BTCO', 'EZBC', 'BRRR',
         'HODL', 'BTCW', 'MSBT', 'GBTC', 'BTC')
PRIOR = ('23 Sep 2026', '24 Sep 2026', '25 Sep 2026', '28 Sep 2026', '29 Sep 2026')
LATEST = ('30 Sep 2026', '01 Oct 2026', '02 Oct 2026', '05 Oct 2026', '06 Oct 2026')
INCOMPLETE = '07 Oct 2026'


class Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = False

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('th', 'td') and self.row is not None:
            self.cell = True
            self.row.append('')

    def handle_data(self, data):
        if self.cell:
            self.row[-1] += data

    def handle_endtag(self, tag):
        if tag in ('th', 'td'):
            self.cell = False
        elif tag == 'tr' and self.row is not None:
            self.rows.append([item.strip() for item in self.row])
            self.row = None


def value(cell):
    if cell == '-':
        return None
    if cell.startswith('(') and cell.endswith(')'):
        return -Decimal(cell[1:-1].replace(',', ''))
    return Decimal(cell.replace(',', ''))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('raw', type=Path, help='original local-only HTML bytes')
    args = p.parse_args()
    receipt = json.loads(Path(__file__).with_name('receipt.json').read_text())
    raw = args.raw.read_bytes()
    if (receipt['url'] != 'https://farside.co.uk/btc/' or receipt['status'] != 200
            or len(raw) > receipt['max_body_bytes'] or len(raw) != receipt['bytes']
            or hashlib.sha256(raw).hexdigest() != receipt['sha256']):
        raise ValueError('source receipt or raw bytes differ')
    table = Table()
    table.feed(raw.decode('utf-8'))
    if sum(row[1:13] == list(FUNDS) for row in table.rows) != 1:
        raise ValueError('fund header/column order differs')

    def row_for(day):
        matches = [row for row in table.rows if row and row[0] == day]
        if len(matches) != 1 or len(matches[0]) != 14:
            raise ValueError(f'missing, duplicate or malformed day: {day}')
        cells = [value(cell) for cell in matches[0][1:13]]
        reported = value(matches[0][13])
        if reported is None:
            raise ValueError(f'missing published total: {day}')
        if all(cell is not None for cell in cells) and abs(sum(cells) - reported) > Decimal('.3'):
            raise ValueError(f'fund cells disagree with reported total: {day}')
        return cells, reported

    totals = []
    for day in PRIOR + LATEST:
        cells, reported = row_for(day)
        if any(cell is None for cell in cells):
            raise ValueError(f'prior/latest day incomplete: {day}')
        totals.append(reported)
    prior = sum(totals[:5], Decimal(0))
    latest = sum(totals[5:], Decimal(0))
    current, _ = row_for(INCOMPLETE)
    missing = [FUNDS[i] for i, item in enumerate(current) if item is None]
    if not missing:
        raise ValueError('latest row unexpectedly complete; use a new frozen window')
    triggered = latest < 0 or (prior < 0 and latest > 0)
    print(json.dumps(dict(source_sha256=receipt['sha256'], received_utc=receipt['receive_utc'],
                          newest_complete_day=LATEST[-1],
                          prior_five_usd_million=str(prior), latest_five_usd_million=str(latest),
                          current_incomplete_day=INCOMPLETE, missing_funds=missing,
                          frozen_sign_event=triggered,
                          status='TRIGGER_NEEDS_CAUSAL_CONTROLS' if triggered else 'NO_EVENT_NO_ECONOMIC_SCREEN',
                          outcome_or_account_read=False), indent=2))


if __name__ == '__main__':
    main()
