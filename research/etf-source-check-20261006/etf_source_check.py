"""Check one bounded ETF page receipt against the existing frozen sign rule."""

import hashlib
import gzip
import json
from decimal import Decimal as D
from html.parser import HTMLParser
from pathlib import Path


class Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside_row = False
        self.inside_cell = False
        self.cells = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.inside_row = True
            self.cells = []
        elif tag == "td" and self.inside_row:
            self.inside_cell = True
            self.cells.append("")

    def handle_data(self, data):
        if self.inside_cell:
            self.cells[-1] += data

    def handle_endtag(self, tag):
        if tag == "td":
            self.inside_cell = False
        elif tag == "tr" and self.inside_row:
            self.rows.append([x.strip() for x in self.cells])
            self.inside_row = False


def value(cell):
    if cell == "-":
        return None
    if cell.startswith("(") and cell.endswith(")"):
        return -D(cell[1:-1].replace(",", ""))
    return D(cell.replace(",", ""))


def main():
    root = Path(__file__).parent
    raw = gzip.decompress((root / "etf-source-check.html.gz").read_bytes())
    receipt = json.loads((root / "etf-source-check-receipt.json").read_text())
    assert receipt["status"] == 200 and receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    rows = Rows()
    rows.feed(raw.decode("utf-8"))
    dates = ["22 Sep 2026", "23 Sep 2026", "24 Sep 2026", "25 Sep 2026", "28 Sep 2026",
             "29 Sep 2026", "30 Sep 2026", "01 Oct 2026", "02 Oct 2026", "05 Oct 2026", "06 Oct 2026"]
    chosen = {}
    for d in dates:
        matches = [r for r in rows.rows if r and r[0] == d]
        assert len(matches) == 1, (d, len(matches))
        cells = matches[0]
        assert len(cells) == 14, (d, len(cells))
        funds = [value(x) for x in cells[1:13]]
        total = value(cells[13])
        complete = all(x is not None for x in funds)
        if complete:
            assert abs(sum(funds, D(0)) - total) <= D(".3"), (d, funds, total)
        chosen[d] = {"complete": complete, "reported_total_usd_million": str(total),
                     "missing_fund_cells": sum(x is None for x in funds)}
    prior = sum((D(chosen[d]["reported_total_usd_million"]) for d in dates[:5]), D(0))
    latest = sum((D(chosen[d]["reported_total_usd_million"]) for d in dates[5:10]), D(0))
    assert all(chosen[d]["complete"] for d in dates[:10])
    assert not chosen[dates[-1]]["complete"]
    result = {"format": "spot-etf-source-check-result-v1", "source_receipt": receipt,
              "rows": chosen, "prior_five_total_usd_million": str(prior),
              "latest_five_total_usd_million": str(latest),
              "frozen_etf_sign_event": latest < 0 or (prior < 0 and latest > 0),
              "status": "NO_EVENT_NO_ECONOMIC_SCREEN" if latest >= 0 and prior >= 0 else "REVIEW_SIGN",
              "limits": "One current receipt; no historical first-availability proof, turnover/FX pair, mature outcome, account or order"}
    (root / "etf-source-check-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("prior_five_total_usd_million", "latest_five_total_usd_million", "frozen_etf_sign_event", "status")}))


if __name__ == "__main__":
    main()
