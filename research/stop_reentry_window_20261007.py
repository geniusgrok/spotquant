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
CASE_COMMITMENT = "e3bf2304e1ea0590db53302ab71dbee0975d7629e65fe10b9a9367d6dded1b06"
DAY = 86_400_000


def checked(path: Path, expected: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"source SHA differs: {path}")
    return raw


def prove(repo: Path, account_path: Path, search_zip: Path, *, order_id: int, sleeve: int,
          trigger_bar: int, candidate_bar: int) -> dict:
    identity = f"{order_id}:{sleeve}:{trigger_bar}:{candidate_bar}".encode()
    if hashlib.sha256(identity).hexdigest() != CASE_COMMITMENT:
        raise ValueError("case differs from the previously counted fixed opportunity")
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
        pre_stop = post_stop = candidate = candidate_models = candidate_history = None
        next_models = next_positions = None
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
                candidate_models = deepcopy(models)
                candidate_history = deepcopy(history)
                if not (initial_bull and reason_hits == [trigger_bar]
                        and not models[sleeve].enter and reason_view.enter):
                    raise ValueError("frozen reason entry differs from incumbent at candidate bar")
                if any(candidate[window] != post_stop[window] for window in owner["sleeves"]):
                    raise ValueError("a sleeve state changed between STOP and candidate decision")
            if day == candidate_bar + 2*DAY:
                next_models = deepcopy(models)
                next_positions = deepcopy(positions)
        # Probe the exact original preview at the first supported candidate.
        from spotquant.session import _view
        from spotquant.preview import decision
        ref = decisions[candidate_bar]
        mark = next(row for row in result["daily"].values()
                    if row["timestamp_ms"] == candidate_bar + DAY)
        if any(f["time"] >= mark["timestamp_ms"] and f["time"] < ref["decision_ms"] for f in fills):
            raise ValueError("wallet changed before candidate")
        bar = bars[str(candidate_bar + DAY)]
        offset = D(ref["decision_ms"] - (candidate_bar + DAY))
        nodes = [D(bar[k]) for k in ("open", "high", "low", "close")]
        section = min(2, int(offset // (DAY // 3)))
        avg_price = nodes[section] + (nodes[section+1] - nodes[section]) * (
            offset - section * (DAY // 3)) / (DAY // 3)
        snapshot = dict(btc=D(mark["btc"]), usdt_free=D(mark["cash_usdt"]),
                        avg_price=avg_price, open_orders=0)
        views, owned = {}, {}
        for window in SLEEVES:
            views[window], owned[window] = _view(candidate_models[window], candidate[window])
        historical_owners = {oid: payload for oid,payload in owners.items()
                             if payload["signal_ms"] <= candidate_bar}
        control = decision(views, owned, snapshot, positions=candidate,
                           owners=historical_owners, entries_enabled=True,
                           capital_limit=D("5000000"), allocation_scale=D(1))
        views[sleeve], owned[sleeve] = _view(reason_view, candidate[sleeve])
        treatment = decision(views, owned, snapshot, positions=candidate,
                             owners=historical_owners, entries_enabled=True,
                             capital_limit=D("5000000"), allocation_scale=D(1))
        print("ORDER_PROBE", json.dumps({
            "control": control["orders"], "treatment": treatment["orders"],
            "avg_price": str(avg_price), "source_control": ref["accepted_orders"],
            "owned": {str(k): str(v) for k,v in owned.items()},
            "actions": {str(k): treatment["sleeves"][str(k)]["action"] for k in SLEEVES},
            "dust": {str(k): bool(getattr(views[k], "_owned_dust",False)) for k in SLEEVES},
        }))
        from spotquant.follow import advance as advance_position
        from spotquant.preview import decision_view
        from spotquant.types import floor_step
        from spotquant.session import _view
        original_buy = next(row for row in fills
                            if row["buyer"] and row["time"] > ref["decision_ms"]
                            and row["time"] < ref["decision_ms"] + 60_000)
        if D(original_buy["quote"]) != D(control["orders"][0]["quoteOrderQty"]):
            raise ValueError("original order and fill not aligned")
        next_ref = next(row for row in result["opportunity_ledger"]
                       if row["event"] == "decision" and row["completed_bar_ms"] == candidate_bar + 2*DAY
                       and any(o["side"] == "SELL" for o in row["accepted_orders"]))
        local_orders = {}
        for label, chosen, modelset in (("control", control, candidate_models),
                                        ("treatment", treatment, {**candidate_models, sleeve: reason_view})):
            copy_models = deepcopy(modelset)
            copy_positions = deepcopy(candidate)
            q = D(chosen["orders"][0]["quoteOrderQty"])
            price=D(original_buy["price"])
            gross=q/price
            local_fill = dict(original_buy, id=10_000, order_id=10_000, quote=q,
                        price=price,qty=gross, commission=gross*D(".001"))
            if label=="control":
                local_fill.update(qty=D(original_buy["qty"]),commission=D(original_buy["commission"]))
            synthetic_owner = {"order": chosen["orders"][0], "sleeves": chosen["orders"][0]["sleeves"],
                               "weights": {str(w): "1" for w in chosen["orders"][0]["sleeves"]},
                               "repair": {str(w): False for w in chosen["orders"][0]["sleeves"]},
                               "signal_ms": candidate_bar, "native_status": "FILLED",
                               "native_executed_qty": str(local_fill["qty"])}
            copy_positions, _, _, _ = apply_day(copy_models, copy_positions,
                {w: None for w in SLEEVES}, set(), candidate_bar + DAY,
                [local_fill], lambda: candidate_history, owners={"10000": synthetic_owner})
            initial_views,initial_owned={},{}
            for w in SLEEVES:
                initial_views[w],initial_owned[w]=_view(copy_models[w],copy_positions[w])
            initial_snapshot=dict(btc=sum((D(p["qty"]) for p in copy_positions.values() if p),D(0)),
                                  usdt_free=snapshot["usdt_free"]-q,avg_price=D(original_buy["price"]),
                                  open_orders=0)
            initial_guard=decision(initial_views,initial_owned,initial_snapshot,
                                   positions=copy_positions,owners=historical_owners,
                                   entries_enabled=True,capital_limit=D("5000000"),
                                   allocation_scale=D(1))
            for open_ms in (candidate_bar + DAY, candidate_bar + 2*DAY):
                row = bars[str(open_ms)]
                high, low, close = (D(row[k]) for k in ("high","low","close"))
                for w, m in copy_models.items():
                    m.update(open_ms,high,low,close)
                    if copy_positions[w] is not None and not copy_positions[w].get("dust"):
                        copy_positions[w] = advance_position(copy_positions[w],
                            dict(open_ms=open_ms,high=high,low=low,close=close,
                                 bull=m.bull,cap_high=m._view_cap_high()),m)
            if label=="control" and any(copy_positions[w] != next_positions[w] for w in SLEEVES):
                raise ValueError("control copied positions differ from recorded source reducer")
            next_views,next_owned={},{}
            for w in SLEEVES:
                next_views[w],next_owned[w]=_view(copy_models[w],copy_positions[w])
            next_mark=next(row for row in result["daily"].values()
                           if row["timestamp_ms"] == candidate_bar + 3*DAY)
            next_bar = bars[str(candidate_bar + 3*DAY)]
            offset=D(next_ref["decision_ms"]-(candidate_bar+3*DAY))
            nodes=[D(next_bar[k]) for k in ("open","high","low","close")]
            section=min(2,int(offset//(DAY//3)))
            next_price=nodes[section]+(nodes[section+1]-nodes[section])*(
                offset-section*(DAY//3))/(DAY//3)
            local_snapshot=dict(btc=sum((D(p["qty"]) for p in copy_positions.values() if p),D(0)),
                                usdt_free=snapshot["usdt_free"]-q,avg_price=next_price,
                                open_orders=1)
            original_stop=next(p for p in owners.values()
                               if p["order"]["type"]=="STOP_LOSS" and
                               p["signal_ms"]==candidate_bar and
                               p.get("native_status")=="CANCELED")
            # The protection is in force at this decision before SELL-first cancellation.
            local_owner=dict(original_stop)
            local_owner["native_status"]="NEW"
            local_owner["sleeves"]=chosen["orders"][0]["sleeves"]
            local_owner["weights"]={str(w):copy_positions[w]["qty"]
                                    for w in local_owner["sleeves"]}
            local_owner["order"]=dict(original_stop["order"],
                quantity=str(floor_step(sum((D(copy_positions[w]["qty"]) for w in local_owner["sleeves"]),D(0)),BASE_STEP)))
            out=decision(next_views,next_owned,local_snapshot,positions=copy_positions,
                         owners={"10001":local_owner},entries_enabled=True,
                         capital_limit=D("5000000"),allocation_scale=D(1))
            local_orders[label]=dict(orders=out["orders"],
                initial_protections=initial_guard["protections"],
                actions={str(w):out["sleeves"][str(w)]["action"] for w in SLEEVES},
                qty={str(w):str(copy_positions[w]["qty"]) if copy_positions[w] else None for w in SLEEVES},
                cash=str(local_snapshot["usdt_free"]),btc=str(local_snapshot["btc"]))
        print("NEXT_DECISION_PROBE",json.dumps(local_orders))
        if (local_orders["control"]["orders"] != next_ref["accepted_orders"]
                or len(local_orders["control"]["initial_protections"]) != 1
                or len(local_orders["treatment"]["initial_protections"]) != 1
                or local_orders["control"]["initial_protections"][0]["stopPrice"]
                   != local_orders["treatment"]["initial_protections"][0]["stopPrice"]
                or local_orders["control"]["actions"]["50"] != "flat"
                or set(local_orders["treatment"]["orders"][0]["sleeves"]) != set(SLEEVES)):
            raise ValueError("one-stop/one-exit paired window did not qualify")
        original_sell=next(row for row in fills
                           if not row["buyer"] and next_ref["decision_ms"] < row["time"]
                           < next_ref["decision_ms"]+60_000)
        if (D(original_sell["qty"]) != D(local_orders["control"]["orders"][0]["quantity"])
                or len([event for event in result["client_events"]
                        if event["method"]=="DELETE" and
                        next_ref["decision_ms"] < event["sent_ms"] < original_sell["time"]]) != 1):
            raise ValueError("control sale and protection cancellation do not match")
        stop=D(local_orders["control"]["initial_protections"][0]["stopPrice"])
        day_bars=[bars[str(candidate_bar+i*DAY)] for i in (1,2,3)]
        if any(D(row["low"]) <= stop for row in day_bars):
            raise ValueError("protected price path differs; cannot use common market exit")
        endpoint=next(row for row in result["daily"].values()
                      if row["timestamp_ms"] == candidate_bar+4*DAY)
        start_cash,start_btc=snapshot["usdt_free"],snapshot["btc"]
        buy_price,sell_price=D(original_buy["price"]),D(original_sell["price"])
        fee,slip=D(".001"),D(".0005")
        prices=[("start",avg_price,"pre"),
                ("post buy",buy_price/(1+slip),"hold")]
        for index,row in enumerate(day_bars):
            for key in (("low","close") if index==0 else
                        ("high","low") if index==2 else
                        ("high","low","close")):
                prices.append((f"day{index} {key}",D(row[key]),"hold"))
        prices.extend([("pre sell",sell_price/(1-slip),"hold"),
                       ("post sell",sell_price/(1-slip),"post"),
                       ("end",D(endpoint["price_usdt"]),"post")])
        pair={}
        for label in ("control","treatment"):
            q=D(control["orders"][0]["quoteOrderQty"] if label=="control"
                else treatment["orders"][0]["quoteOrderQty"])
            sell_qty=D(local_orders[label]["orders"][0]["quantity"])
            gross=(D(original_buy["qty"]) if label=="control" else q/buy_price)
            net=(gross-D(original_buy["commission"]) if label=="control"
                 else gross*(1-fee))
            cash_buy,btc_buy=start_cash-q,start_btc+net
            cash_end=cash_buy+sell_qty*sell_price*(1-fee)
            btc_end=btc_buy-sell_qty
            if cash_buy<0 or btc_end<0:
                raise ValueError("paired wallet exceeds balances")
            values=[]
            for point,price,phase in prices:
                cash,btc=((start_cash,start_btc) if phase=="pre" else
                          (cash_buy,btc_buy) if phase=="hold" else (cash_end,btc_end))
                values.append((point,cash+btc*price))
            peak=values[0][1]
            drawdown=D(0)
            trough=None
            for point,value in values:
                peak=max(peak,value)
                if 1-value/peak>drawdown:
                    drawdown=1-value/peak
                    trough=point
            pair[label]=dict(start=values[0][1],end=values[-1][1],
                             mdd=drawdown,trough=trough,cash=cash_end,btc=btc_end,
                             buy=q,sell_notional=sell_qty*sell_price,
                             fees=q*fee+sell_qty*sell_price*fee)
        base,variant=pair["control"],pair["treatment"]
        if (abs(base["cash"]-D(endpoint["cash_usdt"]))>D("1e-12")
                or abs(base["btc"]-D(endpoint["btc"]))>D("1e-12")
                or abs(base["end"]-D(endpoint["equity_usdt"]))>D("1e-12")
                or base["start"] != variant["start"]):
            raise ValueError("baseline endpoint or equal starting wallet differs")
        print("PAIR_RESULT",json.dumps({
            "same_start_equity_usdt":str(base["start"]),
            "control_end_equity_usdt":str(base["end"]),
            "treatment_end_equity_usdt":str(variant["end"]),
            "treatment_minus_control_usdt":str(variant["end"]-base["end"]),
            "treatment_minus_control_pct_of_start":str(
                (variant["end"]-base["end"])/base["start"]*100),
            "control_local_mdd_pct":str(base["mdd"]*100),
            "treatment_local_mdd_pct":str(variant["mdd"]*100),
            "control_trough":base["trough"],"treatment_trough":variant["trough"],
            "control_turnover_usdt":str(base["buy"]+base["sell_notional"]),
            "treatment_turnover_usdt":str(variant["buy"]+variant["sell_notional"]),
            "control_fees_usdt_at_fill":str(base["fees"]),
            "treatment_fees_usdt_at_fill":str(variant["fees"]),
            "extra_buy_usdt":str(variant["buy"]-base["buy"]),
            "diverted_from_each_original_sleeve_usdt":str(
                base["buy"]/2-variant["buy"]/3),
            "new_sleeve_usdt":str(variant["buy"]/3),
            "same_stop_price_and_single_sell":True,
            "no_stop_cross_in_ohlc_proxy":True,
            "baseline_wallet_reproduced":True,
            "native_or_prospective_result":False,
        }))
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
