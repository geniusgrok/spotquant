"""One frozen, read-only attribution screen for original Spotquant BUY decisions."""

import argparse
import gzip
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path


DAY = 86_400_000
START = 1_640_995_200_000  # 2022-01-01 UTC
ZIP_SHA = "c9f9b30ab5f5a551a9a9bf5e9bd99ced3dc934b50d116eee1d9f5d59ff941cc3"
PROJECTION_SHA = "ef32a84a7e89cfd7ea3d76a8c6fa56a1da95546500269a8a0b688047f94a2118"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def date(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()


def gates(prices, at, window):
    close = prices[at]
    sma = sum(prices[at - window + 1:at + 1], D(0)) / window
    prev_sma = sum(prices[at - window:at], D(0)) / window
    bull = close > sma
    prev_bull = prices[at - 1] > prev_sma
    high252 = max(prices[at - 251:at + 1])
    high400 = max(prices[at - 399:at + 1])
    crash_ok = close >= high252 * D(".5")
    cap_enter = (prices[at - 1] / prices[at - 2] <= D(".89")
                 and close / prices[at - 1] >= D("1.07")
                 and close <= high400 * D(".5"))
    return {
        "bull": bull,
        "confirmed": bull and prev_bull,
        "crash_ok": crash_ok,
        "ordinary_enter": bull and prev_bull and crash_ok,
        "cap_enter": cap_enter,
        "armed_price_only": (bull and prev_bull and crash_ok) or cap_enter,
        "extended": close >= sma * D("1.61"),
        "handoff_price": bull and close >= high400 * D(".89"),
        "sma": str(sma),
        "sma_margin_pct": str((close / sma - 1) * 100),
        "high252_margin_pct": str((close / high252 - D(".5")) * 100),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zip", type=Path, required=True)
    p.add_argument("--projection", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if digest(args.zip) != ZIP_SHA or digest(args.projection) != PROJECTION_SHA:
        raise ValueError("frozen input hash mismatch")
    with zipfile.ZipFile(args.zip) as z:
        coin = json.loads(z.read("history/daily.json"))
        composition = json.loads(z.read("daily-composition.json"))
    projection = json.load(gzip.open(args.projection))
    fx_rows = coin["products"]["USDT-USD"]
    assert coin["fields"] == ["time", "low", "high", "open", "close", "base_volume"]
    assert len(fx_rows) == 1723 and all(b[0] - a[0] == 86400 for a, b in zip(fx_rows, fx_rows[1:]))
    fx = {int(r[0]) * 1000: D(str(r[4])) for r in fx_rows}
    bars = {int(k): D(v["close"]) for k, v in composition["bars"].items()}
    days = sorted(bars)
    assert all(b - a == DAY for a, b in zip(days, days[1:]))
    native = [bars[t] for t in days]
    native_at = {t: i for i, t in enumerate(days)}
    usd_days = sorted(set(bars) & set(fx))
    assert len(usd_days) == 1723 and all(b - a == DAY for a, b in zip(usd_days, usd_days[1:]))
    usd = [bars[t] * fx[t] for t in usd_days]  # USDT/USD is USD per USDT.
    usd_at = {t: i for i, t in enumerate(usd_days)}

    decisions = {}
    for row in projection["opportunity_ledger"]:
        if row.get("event") != "decision":
            continue
        buys = [o for o in row.get("accepted_orders", []) if o.get("side") == "BUY"]
        if buys:
            t = row["completed_bar_ms"]
            if t not in decisions or row["decision_ms"] < decisions[t]["decision_ms"]:
                decisions[t] = row

    buys = []
    for allocation in projection["allocations"]:
        intent = json.loads(allocation[1])
        order = intent.get("order", {})
        if order.get("side") == "BUY" and intent["signal_ms"] >= START and allocation[2] == "settled":
            buys.append(intent)
    buys.sort(key=lambda x: x["signal_ms"])
    assert len({x["signal_ms"] for x in buys}) == len(buys)

    insufficient, late_missing, eligible, reconstructed = [], [], [], []
    for intent in buys:
        t = intent["signal_ms"]
        row = decisions.get(t)
        if row is None or row["decision_ms"] < t + DAY + 60_000:
            late_missing.append(date(t))
            continue
        i = usd_at.get(t)
        if i is None or i < 399 or any(usd_days[j] != t - (i-j)*DAY for j in range(i-399, i+1)):
            insufficient.append(date(t))
            continue
        assert row["completed_bar_ms"] == t and D(row["trigger_close"]) == bars[t]
        orig = {str(w): gates(native, native_at[t], w) for w in (30, 40, 50)}
        if {w: orig[w]["bull"] for w in orig} != row["bullish_votes"]:
            raise ValueError(f"original bullish vote mismatch on {date(t)}")
        bought = list(map(str, intent["sleeves"]))
        if any(not orig[w]["armed_price_only"] or intent["repair"][w] for w in bought):
            raise ValueError(f"original entry branch mismatch on {date(t)}")
        if len(reconstructed) < 3:
            reconstructed.append({"date": date(t), "signal_ms": t,
                                  "decision_ms": row["decision_ms"],
                                  "trigger_close": row["trigger_close"],
                                  "approved_sleeves": bought,
                                  "original_bull_votes": row["bullish_votes"],
                                  "original_price_armed": {w: orig[w]["armed_price_only"] for w in bought}})
        converted = {str(w): gates(usd, i, w) for w in (30, 40, 50)}
        changed = [w for w in bought if not converted[w]["armed_price_only"]]
        component_changes = {w: [k for k in ("bull", "confirmed", "crash_ok", "cap_enter", "extended", "handoff_price")
                                  if orig[w][k] != converted[w][k]] for w in ("30", "40", "50")}
        quote = D(intent["order"]["quoteOrderQty"])
        eligible.append({"date": date(t), "signal_ms": t, "decision_ms": row["decision_ms"],
                         "buy_sleeves": bought, "quote_usdt": str(quote), "fx_usdt_usd": str(fx[t]),
                         "native_close_usdt": str(bars[t]), "converted_close_usd": str(usd[i]),
                         "changed_bought_sleeves": changed,
                         "affected_quote_usdt": str(quote * len(changed) / len(bought)),
                         "component_changes": {w: v for w, v in component_changes.items() if v},
                         "gate_details_if_changed": {
                             w: {"original": orig[w], "converted": converted[w]}
                             for w in bought if component_changes[w]}})

    affected = [r for r in eligible if r["changed_bought_sleeves"]]
    q_total = sum((D(r["quote_usdt"]) for r in eligible), D(0))
    q_affected = sum((D(r["affected_quote_usdt"]) for r in eligible), D(0))
    years = sorted({r["date"][:4] for r in affected})
    material = len(affected) >= 2 and len(years) >= 2 and q_affected >= q_total * D(".05")
    daily = projection["daily"]
    valuation = []
    for t in usd_days:
        if str(t) in daily:
            old = D(daily[str(t)]["equity_usdt"])
            valuation.append((t, old, old * fx[t]))
    last_t, last_old, last_usd = valuation[-1]
    report = {"format": "spot-usdt-numeraire-attribution-result-v1",
              "status": "MATERIAL_SCREEN_ONLY" if material else "CLOSE_NO_MATERIAL_DIVERGENCE",
              "qualification": "conditional historical development diagnostic; 2026 Coinbase receipts do not prove old-day availability",
              "input_sha256": {"zip": ZIP_SHA, "projection": PROJECTION_SHA},
              "sample": {"all_post_2022_buy_groups": len(buys), "insufficient_400_day_warmup": insufficient,
                         "missing_modeled_availability": late_missing,
                         "eligible_groups": len(eligible), "first_eligible": eligible[0]["date"] if eligible else None,
                         "last_eligible": eligible[-1]["date"] if eligible else None},
              "original_reconstruction_first_three": reconstructed,
              "screen": {"affected_groups": len(affected), "affected_years": years,
                         "eligible_quote_usdt": str(q_total), "affected_quote_usdt": str(q_affected),
                         "affected_quote_share": str(q_affected / q_total if q_total else D(0)),
                         "material_gate_passed": material},
              "valuation_sensitivity": {"days": len(valuation), "last_day": date(last_t),
                                        "last_equity_at_parity_usd_proxy": str(last_old),
                                        "last_equity_at_fx_usd_proxy": str(last_usd),
                                        "last_fx": str(fx[last_t]),
                                        "max_abs_fx_minus_parity": str(max(abs(fx[t] - 1) for t, _, _ in valuation))},
              "rows": eligible}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "sample": report["sample"],
                      "screen": report["screen"], "first_three": reconstructed}))


if __name__ == "__main__":
    main()
