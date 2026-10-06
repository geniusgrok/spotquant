"""Read-only screen of confirmed-close dust entry vetoes in the original projection."""
from __future__ import annotations

import argparse
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
from statistics import median

DAY = 86_400_000
SOURCE_SHA = "ef32a84a7e89cfd7ea3d76a8c6fa56a1da95546500269a8a0b688047f94a2118"


def screen(source: Path) -> dict:
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA:
        raise ValueError("original projection checksum differs")
    book = json.loads(gzip.decompress(raw))
    allocations = {}
    for _, payload, phase, response in book["allocations"]:
        owner, result = json.loads(payload), json.loads(response) if response else {}
        if "orderId" in result:
            allocations[result["orderId"]] = owner, phase, result
    fills = sorted(book["fills"], key=lambda item: item["time"])
    by_bar = {}
    duplicate_polls = 0
    for event in book["opportunity_ledger"]:
        if event["event"] != "decision" or not event["desired_orders"]:
            continue
        if not any(item.get("blocked_reason") == "held_sleeve_no_topup"
                   and D(str(item.get("risk_scale", "0"))) > 0
                   for item in event.get("diagnostics", [])):
            continue
        bar = event["completed_bar_ms"]
        duplicate_polls += bar in by_bar
        by_bar.setdefault(bar, event)
    first_by_sale = {}
    for bar, event in sorted(by_bar.items()):
        buy = next((order for order in event["desired_orders"]
                    if order["side"] == "BUY"), None)
        if buy is None:
            continue
        sale_ids = set()
        for window in buy["sleeves"]:
            recent = next((fill for fill in reversed(fills)
                           if fill["time"] <= event["decision_ms"]
                           and window in allocations[fill["order_id"]][0]["sleeves"]), None)
            if recent is None or recent["buyer"]:
                raise ValueError("blocked sleeve has no last owned SELL")
            owner, phase, result = allocations[recent["order_id"]]
            executed = D(result["executedQty"])
            if (phase != "settled" or result["status"] != "FILLED"
                    or owner["order"]["side"] != "SELL"
                    or executed != D(owner["order"]["quantity"])
                    or executed != sum((D(fill["qty"]) for fill in fills
                                       if fill["order_id"] == recent["order_id"]), D(0))):
                raise ValueError("SELL is not fully settled and fill matched")
            if D(event["sleeves"][str(window)]["owned_btc"]) <= 0:
                raise ValueError("blocked sleeve has no recorded residual")
            sale_ids.add(recent["order_id"])
        for sale_id in sale_ids:
            first_by_sale.setdefault(sale_id, bar)
    returns = []
    dates = []
    for bar in first_by_sale.values():
        entry = book["daily"].get(str(bar))
        exit_ = book["daily"].get(str(bar + 7 * DAY))
        if entry is None or exit_ is None:
            continue
        # Next UTC open price marks, then an intentionally favorable 20 bp fee floor.
        returns.append(D(exit_["price_usdt"]) / D(entry["price_usdt"]) - 1 - D(".002"))
        dates.append(bar)
    dates.sort()
    return {
        "source_sha256": SOURCE_SHA,
        "blocked_poll_rows": len(by_bar) + duplicate_polls,
        "distinct_completed_bars": len(by_bar),
        "confirmed_last_sale_owners": len(first_by_sale),
        "seven_day_price_marks_available": len(returns),
        "positive_after_fee_floor": sum(value > 0 for value in returns),
        "median_after_fee_floor_pct": str(median(returns) * 100) if returns else None,
        "worst_after_fee_floor_pct": str(min(returns) * 100) if returns else None,
        "overlapping_selected_seven_day_windows": sum(b - a < 7 * DAY for a, b in zip(dates, dates[1:])),
        "position_flags_at_each_decision": "not retained in the projection",
        "account_profit_or_native_execution": "not measured",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("projection", type=Path, nargs="?", default=Path(__file__).resolve().parents[2]
                        / "evidence/btc-flow-risk-20261004/spot-baseline-projection.json.gz")
    args = parser.parse_args()
    print(json.dumps(screen(args.projection), indent=2, ensure_ascii=False))
