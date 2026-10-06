"""Count original STOP close and frozen 7/2/5 support; never replay an account."""
from __future__ import annotations

import argparse
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
import zipfile

from spotquant.model import DAY, ORIGIN, Model, SLEEVES
from spotquant.preview import BASE_STEP, MIN_NOTIONAL

PROJECTION_SHA = "ef32a84a7e89cfd7ea3d76a8c6fa56a1da95546500269a8a0b688047f94a2118"
PROGRESS_ZIP_SHA = "86259ad2886e697cd316e42ca6f754a3d5869fcbc59f0db0471469f4851beb69"
DAILY_SHA = "26c9509c42ebcd88beca5bd1dc818a866d72b1285822f22e37fbb75091755d2a"


def checked(path: Path, expected: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"checksum mismatch: {path}")
    return raw


def support(projection: Path, progress_zip: Path) -> dict:
    source = json.loads(gzip.decompress(checked(projection, PROJECTION_SHA)))
    with zipfile.ZipFile(progress_zip, "r") as bundle:
        checked(progress_zip, PROGRESS_ZIP_SHA)
        daily_raw = bundle.read("inputs/days.json")
    if hashlib.sha256(daily_raw).hexdigest() != DAILY_SHA:
        raise ValueError("original daily packet checksum mismatch")
    bars = json.loads(daily_raw)["bars"]
    original_decisions = {}
    for event in source["opportunity_ledger"]:
        if event["event"] == "decision":
            original_decisions.setdefault(event["completed_bar_ms"], event)
    for day, event in original_decisions.items():
        if bars[str(day)]["close"] != event["trigger_close"]:
            raise ValueError("daily packet is not the original decision price")

    models = {window: Model(window) for window in SLEEVES}
    states = {}
    for day in sorted(int(key) for key in bars if int(key) >= ORIGIN):
        bar = bars[str(day)]
        for window, model in models.items():
            model.update(day, D(bar["high"]), D(bar["low"]), D(bar["close"]))
            closes = list(model.closes)
            states[window, day] = dict(
                bull=model.bull, streak=model.streak, crash_ok=model.crash_ok,
                extended=model.extended, cap_enter=model.cap_enter,
                close=model.close,
                prior_five_high=max(closes[-6:-1]) if len(closes) >= 6 else None,
            )
    for day, event in original_decisions.items():
        for window in SLEEVES:
            if states[window, day]["bull"] != event["bullish_votes"][str(window)]:
                raise ValueError("replayed daily trend differs from original decision")

    owners = {}
    for _, payload, phase, result_json in source["allocations"]:
        result = json.loads(result_json) if result_json else {}
        if "orderId" in result:
            owners[result["orderId"]] = json.loads(payload), phase, result
    fills = sorted(source["fills"], key=lambda row: (row["time"], row["id"]))
    holdings = {window: D(0) for window in SLEEVES}
    dust = {window: False for window in SLEEVES}
    roles = []
    for fill in fills:
        owner, phase, result = owners[fill["order_id"]]
        windows = owner["sleeves"]
        weights = {int(window): D(value) for window, value in owner["weights"].items()}
        total = sum(weights.values(), D(0))
        commission = D(fill["commission"]) if fill["commission_asset"] == "BTC" else D(0)
        delta = D(fill["qty"]) - commission if fill["buyer"] else D(fill["qty"]) + commission
        if delta <= 0 or total <= 0 or set(weights) != set(windows):
            raise ValueError("invalid fill ownership")
        given = D(0)
        for index, window in enumerate(windows):
            portion = delta - given if index == len(windows) - 1 else delta * weights[window] / total
            given += portion
            if fill["buyer"]:
                holdings[window] += portion
                dust[window] = False
                continue
            if portion > holdings[window] + BASE_STEP:
                raise ValueError("sale exceeds attributed holding")
            remaining = max(D(0), holdings[window] - portion)
            intended = D(owner["order"]["quantity"])
            executed = D(result.get("executedQty", "0"))
            rounded = sum((value // BASE_STEP * BASE_STEP for value in weights.values()), D(0))
            grouped_dust = (phase == "settled" and result.get("status") == "FILLED"
                            and intended == rounded and executed == intended
                            and BASE_STEP <= remaining < BASE_STEP * len(windows)
                            and remaining * D(fill["price"]) < MIN_NOTIONAL)
            closed = remaining < BASE_STEP or grouped_dust
            holdings[window] = remaining
            dust[window] = closed
            if owner["order"]["type"] == "STOP_LOSS":
                full_receipt = (phase == "settled" and result.get("status") == "FILLED"
                                and executed == intended == rounded
                                and executed == sum((D(row["qty"]) for row in fills
                                                    if row["order_id"] == fill["order_id"]), D(0)))
                applied_final = (source["positions"].get(str(window)) or {}).get("sell_applied", {}).get(
                    str(fill["order_id"]))
                roles.append(dict(order_id=fill["order_id"], window=window,
                                  fill_ms=fill["time"], closed_by_allocation=closed,
                                  full_terminal_receipt=full_receipt,
                                  final_applied_matches=applied_final is not None
                                  and D(applied_final) == executed))
    for window in SLEEVES:
        saved = source["positions"][str(window)]
        if holdings[window] != D(saved["qty"]) or dust[window] != saved["dust"]:
            raise ValueError("attribution fails to reproduce final persisted position")

    pattern_roles = []
    aligned = []
    for role in roles:
        fill_ms, window = role["fill_ms"], role["window"]
        first_day = fill_ms // DAY * DAY
        completed_day = first_day - DAY  # frozen policy uses model.last at the fill
        initial_bull = states[window, completed_day]["bull"]
        next_buy = min((row["time"] for row in fills if row["buyer"] and row["time"] > fill_ms
                        and window in owners[row["order_id"]][0]["sleeves"]), default=10**30)
        intact = initial_bull
        first_pattern = None
        for day in range(first_day, min(next_buy // DAY * DAY + DAY, max(int(k) for k in bars) + DAY), DAY):
            row = states.get((window, day))
            if row is None:
                break
            if not row["bull"]:
                intact = False
            if (intact and day >= completed_day + 7 * DAY
                    and row["streak"] >= 2 and row["crash_ok"]
                    and not row["extended"] and not row["cap_enter"]
                    and row["prior_five_high"] is not None
                    and row["close"] > row["prior_five_high"] and day < next_buy):
                first_pattern = day
                break
        if first_pattern is None:
            continue
        pattern_roles.append((role["order_id"], window, first_pattern))
        for bar, event in sorted(original_decisions.items()):
            if not (first_pattern <= bar < next_buy // DAY * DAY and event["decision_ms"] < next_buy):
                continue
            if (not event["entries_enabled"] or not all(
                    states[window, d]["bull"] and states[window, d]["crash_ok"]
                    and states[window, d]["streak"] >= 2
                    for d in range(first_pattern, bar + DAY, DAY))):
                continue
            if any(order["side"] == "BUY" for order in event["accepted_orders"]):
                aligned.append((role["order_id"], window, bar))
                break

    asof_snapshots = sum("positions" in event for event in source["opportunity_ledger"])
    if asof_snapshots:
        raise ValueError("new per-event positions need separate durable-field validation")
    by_stop = []
    for order_id in sorted({row["order_id"] for row in roles}):
        group = [row for row in roles if row["order_id"] == order_id]
        by_stop.append({
            "order_id": order_id,
            "allocated_sleeves": [row["window"] for row in group],
            "terminal_exact_fill_all": all(row["full_terminal_receipt"] for row in group),
            "allocated_close_all": all(row["closed_by_allocation"] for row in group),
            "final_sell_applied_all": all(row["final_applied_matches"] for row in group),
            "per_decision_durable_state_present": False,
        })
    return {
        "source_sha256": PROJECTION_SHA,
        "daily_sha256": DAILY_SHA,
        "matching_original_decision_bars": len(original_decisions),
        "native_stop_orders": len({row["order_id"] for row in roles}),
        "allocated_stop_sleeve_roles": len(roles),
        "terminal_and_attributed_sleeve_roles": sum(row["full_terminal_receipt"]
                                                    and row["closed_by_allocation"]
                                                    and row["final_applied_matches"] for row in roles),
        "frozen_price_pattern_roles": len(pattern_roles),
        "frozen_price_pattern_orders": len({order for order, _, _ in pattern_roles}),
        "pattern_with_later_baseline_buy_session": len(aligned),
        "per_decision_dust_sell_applied_snapshots": asof_snapshots,
        "strict_persisted_state_qualified_opportunities": 0,
        "per_stop_proof_private": by_stop,
        "pattern_details_private": pattern_roles,
        "aligned_details_private": aligned,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("projection", type=Path)
    parser.add_argument("progress_zip", type=Path)
    args = parser.parse_args()
    print(json.dumps(support(args.projection, args.progress_zip), indent=2))
