"""Rebuild one archived stop-close state from its original reducer and ordered fills.

This is a source-bound state proof. It makes no venue request, order, wallet,
or historical account replay and does not estimate a candidate return.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal as D
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

SOURCE = "74bd6e035e36531c517029077c1a9e2e5ec44516"
ACCOUNT_SHA = "4c937ecf80f60d957486a752562c8ab8dfee4c06fa2e5b38b135cfb24ec38872"
SEARCH_ZIP_SHA = "c9f9b30ab5f5a551a9a9bf5e9bd99ced3dc934b50d116eee1d9f5d59ff941cc3"
DAILY_SHA = "6a35dadcbe9228d95191a2d2716bc2c2d9f01aa8be08638718325fbefb376009"
DAY = 86_400_000


def checked(path: Path, expected: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"source SHA differs: {path}")
    return raw


def prove(repo: Path, account_path: Path, search_zip: Path, *, order_id: int, sleeve: int,
          trigger_bar: int, candidate_bar: int) -> dict:
    account_blob = checked(account_path, ACCOUNT_SHA)
    with zipfile.ZipFile(io.BytesIO(checked(search_zip, SEARCH_ZIP_SHA))) as archive:
        day_blob = archive.read("daily-composition.json")
    if hashlib.sha256(day_blob).hexdigest() != DAILY_SHA:
        raise ValueError("completed BTCUSDT daily source differs")
    account = json.loads(gzip.decompress(account_blob))
    if account["source"] != dict(git_head=SOURCE, dirty=False,
            python_sources_sha256="f2d6c3e73c2acb7b328a7a0e2678bb95ceb77d8a17a46139abfe8b357befdbf6"):
        raise ValueError("original account producer differs")
    result = account["results"]["crowding-interaction-base"]
    if not (result["complete"] and result["audit"]["passed"]
            and result["audit"]["no_deposits"] and result["execution_unresolved_sessions"] == 0
            and not result["pending_intents"]):
        raise ValueError("original account completion or audit differs")
    fills = sorted(result["fills"], key=lambda row: (row["time"], row["id"]))
    if [row["id"] for row in fills] != list(range(1, len(fills) + 1)):
        raise ValueError("ordered fill IDs are not complete")
    bars = json.loads(day_blob)["bars"]
    decisions = {}
    for event in result["opportunity_ledger"]:
        if event["event"] == "decision":
            decisions.setdefault(event["completed_bar_ms"], event)
    if any(bars[str(day)]["close"] != row["trigger_close"] for day, row in decisions.items()):
        raise ValueError("daily closes differ from original decisions")
    owners = {}
    durable_id = None
    for identity, payload, phase, response_json in result["allocations"]:
        response = json.loads(response_json) if response_json else {}
        if "orderId" not in response:
            continue
        owner = json.loads(payload)
        owner.update(native_status=response["status"], native_executed_qty=response["executedQty"])
        owners[str(response["orderId"])] = owner
        if response["orderId"] == order_id:
            durable_id = identity
            if (phase != "settled" or response["status"] != "FILLED"
                    or D(response["executedQty"]) != D(owner["order"]["quantity"])
                    or owner["order"]["type"] != "STOP_LOSS"):
                raise ValueError("target STOP is not terminal and exact")
    target = [row for row in fills if row["order_id"] == order_id]
    if len(target) != 1 or target[0]["buyer"] or durable_id is None:
        raise ValueError("target STOP lacks its unique allocated fill")
    fill = target[0]
    owner = owners[str(order_id)]
    if D(fill["qty"]) != D(owner["native_executed_qty"]):
        raise ValueError("target STOP fill and native amount differ")
    posts = [event for event in result["client_events"]
             if event["method"] == "POST" and event["client_id"] == durable_id]
    if len(posts) != 1 or posts[0]["order"] != owner["order"] or posts[0]["received_ms"] >= fill["time"]:
        raise ValueError("unique durable pre-close order receipt is absent")
    if any(row["id"] != fill["id"] for row in fills
           if posts[0]["received_ms"] < row["time"] < decisions[candidate_bar]["decision_ms"]):
        raise ValueError("another fill can change the position before the candidate decision")
    if any(event["sent_ms"] < decisions[candidate_bar]["decision_ms"]
           and event["received_ms"] > posts[0]["received_ms"]
           for event in result["client_events"]):
        raise ValueError("unaccounted order write between STOP receipt and decision")

    # Execute the exact original Python package, never the later slim main code.
    source_archive = subprocess.check_output(["git", "archive", SOURCE, "spotquant"], cwd=repo)
    with tempfile.TemporaryDirectory(prefix="spotquant-reducer-") as location:
        with tarfile.open(fileobj=io.BytesIO(source_archive), mode="r:") as bundle:
            if any(not (item.name == "spotquant" or item.name.startswith("spotquant/"))
                   or ".." in Path(item.name).parts
                   for item in bundle.getmembers()):
                raise ValueError("unexpected source archive path")
            bundle.extractall(location, filter="data")
        sys.path.insert(0, location)
        from spotquant.follow import advance, apply_day
        from spotquant.model import Model, ORIGIN, SLEEVES
        from spotquant.preview import BASE_STEP

        models = {window: Model(window) for window in SLEEVES}
        positions = {window: None for window in SLEEVES}
        follows = {window: None for window in SLEEVES}
        accounted = set()
        history = []
        by_day = {}
        for row in fills:
            trade = dict(row)
            for field in ("qty", "quote", "price", "commission"):
                trade[field] = D(trade[field])
            by_day.setdefault(row["time"] // DAY * DAY, []).append(trade)
        stop_day = fill["time"] // DAY * DAY
        pre_stop = post_stop = candidate = None
        initial_bull = None
        reason_view = None
        trend_intact = False
        reason_hits = []
        for day in range(ORIGIN, max(map(int, bars)) + DAY, DAY):
            if day == stop_day:
                pre_stop = deepcopy(positions)
                initial_bull = models[sleeve].bull
                if any(D(pre_stop[window]["qty"]) != D(owner["weights"][str(window)])
                       for window in owner["sleeves"]):
                    raise ValueError("pre-close state differs from durable STOP allocation")
            positions, follows, accounted, closed = apply_day(
                models, positions, follows, accounted, day, by_day.get(day, []),
                lambda: history, owners=owners)
            if day == stop_day:
                if set(closed) != set(owner["sleeves"]):
                    raise ValueError("original reducer did not close all STOP sleeves")
                post_stop = deepcopy(positions)
                reason_view = deepcopy(models[sleeve])
                trend_intact = bool(initial_bull)
            bar = bars[str(day)]
            high, low, close = (D(bar[name]) for name in ("high", "low", "close"))
            for window, model in models.items():
                model.update(day, high, low, close)
                if positions[window] is not None and not positions[window].get("dust"):
                    positions[window] = advance(positions[window], dict(
                        open_ms=day, high=high, low=low, close=close,
                        bull=model.bull, cap_high=model._view_cap_high()), model)
            history.append((day, high, low, close))
            if reason_view is not None and day <= candidate_bar:
                reason_view.update(day, high, low, close)
                if not reason_view.bull:
                    trend_intact = False
                closes = list(reason_view.closes)
                if (trend_intact and day >= stop_day - DAY + 7 * DAY
                        and reason_view.streak >= 2 and reason_view.crash_ok
                        and not reason_view.extended and not reason_view.cap_enter
                        and len(closes) >= 6 and reason_view.close > max(closes[-6:-1])):
                    reason_hits.append(day)
                    reason_view.need_reset = False
                    reason_view.enter = True
            if day == candidate_bar:
                candidate = deepcopy(positions)
                if not (initial_bull and reason_hits == [trigger_bar]
                        and not models[sleeve].enter and reason_view.enter):
                    raise ValueError("frozen reason entry differs from incumbent at candidate bar")
                if any(candidate[window] != post_stop[window] for window in owner["sleeves"]):
                    raise ValueError("a sleeve state changed between STOP and candidate decision")
        if len(accounted) != len(fills):
            raise ValueError("original reducer did not consume all fills")
        if any(positions[window] != result["positions"][str(window)] for window in SLEEVES):
            raise ValueError("reconstructed final positions differ from persisted account")
        if any(not post_stop[window]["dust"] or D(post_stop[window]["qty"]) >= BASE_STEP
               or D(post_stop[window]["sell_applied"][str(order_id)]) != D(fill["qty"])
               for window in owner["sleeves"]):
            raise ValueError("target close did not persist verified dust and applied quantity")

    later_decisions = [event for event in result["opportunity_ledger"]
                       if event["event"] == "decision"
                       and fill["time"] < event["decision_ms"] <= decisions[candidate_bar]["decision_ms"]]
    if not later_decisions or any(any(D(event["sleeves"][str(window)]["owned_btc"])
           != D(post_stop[window]["qty"]) for window in owner["sleeves"])
           for event in later_decisions):
        raise ValueError("post-close logged ownership differs from reconstructed state")
    btc_sum = sum((D(post_stop[window]["qty"]) for window in SLEEVES), D(0))
    daily_marks = [day for day in result["daily"].values()
                   if fill["time"] < day["timestamp_ms"] < decisions[candidate_bar]["decision_ms"]]
    if not daily_marks or any(D(day["btc"]) != btc_sum for day in daily_marks):
        raise ValueError("post-close wallet marks differ from reconstructed owned BTC")
    between_sessions = [session for session in result["sessions"]
                        if fill["time"] < session["start_ms"] <= decisions[candidate_bar]["decision_ms"]]
    if (not between_sessions or between_sessions[0]["status"] != "offline_execution"
            or any(session["execution_unresolved"] for session in between_sessions)
            or any(error["reason"] != "session deadline reached" for session in between_sessions
                   for error in session["errors"])):
        raise ValueError("intervening session status cannot support committed state")
    current = decisions[candidate_bar]
    if not (current["entries_enabled"] and any(order["side"] == "BUY" for order in current["accepted_orders"])):
        raise ValueError("frozen candidate has no original risk-enabled BUY session")
    canonical = json.dumps({str(window): candidate[window] for window in SLEEVES},
                           sort_keys=True, separators=(",", ":"))
    return {
        "original_source_commit": SOURCE,
        "account_sha256": ACCOUNT_SHA,
        "daily_source_sha256": DAILY_SHA,
        "original_decision_closes_checked": len(decisions),
        "ordered_fills_reduced": len(fills),
        "final_persisted_positions_equal": True,
        "pre_stop_durable_weights_equal": True,
        "terminal_stop_and_all_allocated_closes": True,
        "reconstructed_dust_and_sell_applied_at_candidate": True,
        "post_close_ledger_and_daily_wallet_equal": True,
        "post_close_wallet_marks_checked": len(daily_marks),
        "intervening_new_fills": 0,
        "frozen_trigger_day_ms": trigger_bar,
        "candidate_completed_bar_ms": candidate_bar,
        "original_entry_armed": False,
        "reason_entry_armed": True,
        "reconstructed_candidate_state_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "direct_asof_sqlite_snapshot_available": False,
        "development_treatment_support": 1,
        "paired_wallet_or_native_trade_result": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("account", type=Path)
    parser.add_argument("search_zip", type=Path)
    parser.add_argument("--order-id", type=int, required=True)
    parser.add_argument("--sleeve", type=int, required=True)
    parser.add_argument("--trigger-bar", type=int, required=True)
    parser.add_argument("--candidate-bar", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(prove(args.repo, args.account, args.search_zip,
                           order_id=args.order_id, sleeve=args.sleeve,
                           trigger_bar=args.trigger_bar, candidate_bar=args.candidate_bar), indent=2))
