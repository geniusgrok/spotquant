# Spot account meter

The economic record is one continuous BTCUSDT spot account. Long or cash. No borrow, no short, no futures, no funding, no liquidation, and no leverage. The decision is `spotquant.model.Model`, one instance per sleeve. The default book is P4: three sleeves with SMA windows 30, 40, and 50 on one USDT pool (`research.account.simulate_sleeves`). The single-sleeve SMA 40 book is P3 (`research.account.simulate`); with one sleeve the two meters print the same trades, final CNY, and drawdown. The rest of this paragraph describes one sleeve. A completed UTC daily close strictly above the sleeve's SMA is bullish. An ordinary entry needs two such closes, a fresh cross since that sleeve's last exit, and a close at least half the inclusive 252-day highest close. The account buys at the next daily open. An armed sleeve spends the pool divided by the sleeves that hold nothing after that open's exits. While long, a stop 28% under the running high is modeled by taking that day's high before its low. A gap through the inherited stop sells at the open. A close that is no longer bullish sells at the next open. A close at least 61% above the SMA is a blow-off and sells the next open. A crash reversal is a completed day up at least 7% after a day down at least 11%, while the close is still at least half under the inclusive 400-day highest close. That signal may buy the next open. Until the close is back above the SMA and no more than 11% under that 400-day high, the repair position ignores the SMA exit, the blow-off, and the 4% close. Any other open position sells the next open when a completed close is 4% or more under the entry fill. When that close shares an open with an SMA exit or a blow-off, the recorded kind is `adverse`. There is no same-day re-entry. The 28% distance is wider than Binance spot `trailingDelta` (2000 bips), so the live preview is an amended `STOP_LOSS` `stopPrice`.

This meter is not `session.run`. The live session is read-only, previews a market order after a completed close, and does not fill. Cold start does not buy a regime that was already bullish. The meter's peak starts at the fill open. An entry preview has no fill yet, so its stop is 28% under the completed close. A hold with no recorded fill uses that same close. The bullish-streak high can start before the fill and is not the stop. A buy that follows an entry preview is recorded from the account trades; its preview peak then starts at that fill. When a crash reversal and an ordinary entry are both armed, the preview is the repair entry, because the meter records that fill as a repair hold. During repair the preview peak is the high since that fill. The 4% close sells the next open and does not cap the loss at 4%. Walking the registered P3 rules with the low tested before the high prints the same fills, the same final CNY, and the same 29.15% drawdown, because the 28% stop does not sell on this path. That check was made on the P3 book only. The registered convention remains high before low. A path on which the stop does sell can differ. Those differences are why a preview is not the CAGR.

## Window and valuation

- Start `2020-01-01T00:00:00Z`, end `2026-09-20T00:00:00Z` exclusive.
- Initial CNY 10,000. No additions.
- USDT is valued at USD par. FRED DEXCHUS applies from 17:00 UTC on its observation date. The rate is ex-post valuation, not a decision input. SHA-256 of the committed file is `733c2bbccfd42448d72f8b7a7ee2cd744f1b3263ac1be88e34260b9c7b2ec874`.
- Open, high, low, and flat cash marks inside a bar use that bar's 00:00 UTC timestamp. The daily CNY series and the final mark use the last millisecond of the UTC day, so a 17:00 print applies to the close curve on its date and to the drawdown path from the next open. Valuing the registered path at the end of each UTC day leaves the fills and the final CNY unchanged and prints continuous MDD 29.37%, still inside the 30% bound. The registered trial keeps the open-timestamp path.
- Conversion haircut 0.1% when CNY becomes USDT at the start, and again when equity is marked back to CNY.
- Spot VIP0 taker fee 0.1% each side. Entry slip 0.05%, exit slip 0.05%, stop slip 0.1%.
- A favorable mark updates the equity peak only. An unfavorable mark also updates the continuous drawdown. The word `adverse` elsewhere means the 4% close under the entry fill, not this mark.
- Daily bars are the official path because the daily files from the model origin through the end are contiguous. Hourly files inside the window have gaps. A 10% participation cap does not change fills at this account size: even a much larger terminal balance is far below 10% of BTCUSDT daily quote volume. No separate depth trial is reported.

## Selection

The P3 default was chosen on the full window. There is no out-of-sample result. P4 keeps that window: its constants come from the P3 plateaus and a sleeve set fixed before it was measured, but the P3 search and the diagnostics listed in the P4 protocol were read first.

`evidence/rebuild-20260928/frontier.json` is the earlier SMA-window by trail grid, measured when a single bullish close bought the next open and the trail was 20%. Its highest terminal CNY was SMA 40 / trail 30%. Its highest trail at or under 20% was SMA 40 / trail 20%, recorded as P1.

P2 replaced that default. The search kept the same costs, window, and long-or-cash account, and added confirmation, a fresh-cross latch, a 252-day crash filter, separate entry and exit averages, percent-reversal entries, dip scale-in, and shock-day exits. Ideas taken from the owner's other research were adapted to this account: Donchian channels, a ratchet trail, cooldown, and an equity flatten from the perpetual book; a four-hour ATR impulse and a two-close confirmation from the leveraged book. Shorts, funding, liquidation, isolated margin, and leverage were left out.

Promotion:

1. A row can become the default only when cost-net CAGR is at least 100% and continuous MDD is at most 30%.
2. When several distances of one rule clear both targets, the default is the round value on a flat step that is not next to a distance that misses a target. A higher terminal value on that edge is recorded and not promoted.
3. Until any causal long-or-cash row meets both targets, the default is the highest terminal CNY on this window.

P2 was the highest terminal CNY before this book: SMA 40 for entry and exit, confirm 2, fresh cross, crash filter 0.50, trail 0.28. It did not meet either target. In the same float scan, which matched the official P1 figures to the cent, the highest CAGR with MDD at or under 30% on that earlier search was an exit SMA of 15, confirm 3, no fresh-cross latch, crash filter 0.50, and a trail of 0.28 or wider: about 57.31% CAGR, 29.59% MDD, final CNY about 209,905. It was not promoted.

P3 adds three causal rules on that core. It was the default until P4. A close at least 60% above the SMA sells the next open. One trade in the window uses it: the 2020-10-10 entry sells on the 2021-01-08 open. Thresholds from just above the 2021-01-06 extension through the 2021-01-07 extension sell that same open. A crash reversal may enter while repair holds through SMA whipsaws; the trades that use it are the 2020-03-13 reversal, bought on 2020-03-14, and the 2022-11-10 reversal, bought on 2022-11-11. The May 2021 reversal is outside the 50% depth filter. A non-repair position also sells the next open after a close 4% or more under its fill. Against the same rules with that close disabled, the two exits that change are 2022-02-08, sold on 2022-02-14 instead of 2022-02-18, and 2022-03-02, sold on 2022-03-04 instead of 2022-03-05. Other bars labeled `adverse` are sold on the same open the SMA exit would have used.

The 4% distance is the round value on a flat step, which is rule 2 above. On this Decimal meter, 3.8% and 4.0% print the same account. 3.5% through 3.7% print a higher one, final CNY 1,118,235.000942666294587944406, because the February 2022 trade sells one day earlier. 3.4% sells the 2021-01-30 trade, which otherwise runs to 2021-03-25 for about +52.58%, and the account falls to about 88.5% CAGR. 4.2% through 4.4% still clear both targets at a lower terminal value. 4.5% no longer sells either 2022 trade early and reverts to the pre-rule book: final CNY 989,597.0504440564507983512087, CAGR 98.1505%, MDD 31.5786% on 2022-03-18. That book misses both targets and is not registered. P3 keeps 4.0%. The 3.5–3.7% step is one tenth of a point from the distance that deletes the January 2021 winner, so it stays recorded and is not the default.

## Stresses

Same default model, one change at a time:

- fee ×1.5 (`--fee 0.0015`)
- exit slip ×2 and stop slip ×2 (`--exit-slip 0.001 --stop-slip 0.002`)
- seeded skip of 20% of daily opens (`--sequence skip`)
- one seeded 21-day block of opens (`--sequence block`)

The skip and block seeds do not look at prices. Native qualification stays `NOT_QUALIFIED` regardless. Economic qualification is `MET` only on a recorded trial that meets both targets. A stress that misses one target does not revoke a base trial that met both. P4 meets neither target, so its economic qualification is `NOT_MET` and its stresses are context.

## What was searched besides the grid

P2 remains `evidence/rebuild-20260928/P2.json`, source `b30608d43f3039f7cce5c1c72341ced3021007e0`: final CNY 432,658.9697193516804930799377, cost-net CAGR 75.19%, continuous MDD 33.10% on 2021-01-31, 43 closed trades, still long, `targets_met` false. P1 remains the prior 20% trail measurement. The default is now P3. See `evidence/rebuild-20260928/RESULT.md`.

`python3 -m research.rebuild --suite` writes the P4 evidence set under `evidence/sleeves-20260929/`: `P4.json`, the four stresses, the 4% variants, the volatility test, `P3-reproduction.json`, the ETHUSDT check, `plateau.json`, and `suite.json` with the mechanical decisions. `python3 -m research.rebuild P4` writes one trial; `--book single --sma 40` runs the P3 book. Writing into `evidence/rebuild-20260928/` is refused, so P1, P2, P3, and `frontier.json` stay as measured. `python3 -m research.rebuild --grid` writes one-sleeve `hold-grid.json` under the current constants. `python3 -m research.forward` extends the append-only forward ledger.

The account is still long at the window end, so the last mark uses the final daily close and does not charge an exit fee. The continuous MDD of P3 is on 2020-03-16, inside the crash-reversal hold that began on 2020-03-14. A lookahead that sells every losing day is not a result of this meter.

## P4 protocol (declared 2026-09-29, before any P4 number is computed)

Why: the P3 account clears both targets at SMA 40 only. Its neighbors SMA 35 and SMA 45 print 59.1% and 94.8% CAGR, SMA 30 and SMA 50 print 58.9% and 70.4%, and the top three trades carry about 48% of the positive log return. The owner accepted a lower in-sample return in exchange for less dependence on one parameter. The targets are not changed, not withdrawn, and the window is not moved.

What is already known and therefore not counted as out-of-sample: the P3 numbers, the SMA neighbor table above, the ablations of the 4% close, the blow-off, and the crash reversal, and a float scan of close-curve drawdowns for several sleeve sets (35/40/45 about 89.8% CAGR, 30/40/50 about 82.3%, 20/40/80 about 79.7%). Those diagnostics were read before this protocol. They can bias the choice of sleeve set. The set below was chosen for equal spacing and a middle sleeve equal to the P3 window, and it is chosen once.

Declared, in this order:

1. Book. Three sleeves with SMA windows 30, 40, and 50. Each sleeve is a `Model` with the constants of that window. There is one shared USDT balance. When a sleeve is armed at an open, it buys with the free USDT divided by the number of sleeves that are flat at that open. Sleeves buy and sell independently; a sleeve exit sells only that sleeve's coins. Every fee, slip, stop, and CNY conversion is the P3 value. The drawdown path marks the combined equity at the same open, high, and low points as the single-sleeve meter. Only the long sleeves are valued at the high or the low. The combined stop is each sleeve's own 28% stop.
2. Equivalence. With one sleeve of window 40, this engine must print the trades and the final CNY of `research.account.simulate`. A test enforces it.
3. Plateau-centered thresholds. For each of the blow-off extension, the crash-reversal drop, bounce and depth, and the handoff distance, scan the single-sleeve SMA 40 book in steps of 0.005 around the P3 value. The contiguous interval that prints the P3 trades exactly is the plateau. The adopted value is the midpoint of that interval rounded to 0.01, and it is adopted only if that rounded value itself prints the P3 trades exactly. If not, the P3 value stays. All sleeves use one value.
4. The 4% close. Measure the sleeves book with the close off and at 3.5%, 4.0%, and 4.5%. The close is included only if all three distances print a continuous MDD no higher than the book without it and a CAGR no more than one point lower. Drawdowns are compared at 1e-9; the Decimal context carries 28 digits, and two paths that trough on the same day can differ in the last of them. Otherwise the default is off.
5. Volatility scaling. One test only. At an entry, the sleeve buys `min(1, 0.70 / v)` of its share, where `v` is the sample standard deviation of the last 30 daily log returns at the signal close, times the square root of 365. The rest stays in the pool. It is adopted only if continuous MDD falls by at least one point and CAGR falls by no more than three points against the book adopted in step 4. Otherwise it is recorded and not adopted.
6. Qualification. The targets are unchanged: cost-net CAGR at least 100% and continuous MDD at most 30%. Economic qualification of P4 is recomputed from its own numbers. If P4 misses a target it is recorded as `NOT_MET`. P3 stays in the evidence unchanged as the in-sample upper bound and is not deleted or hidden.
7. Stresses. The same four stresses as P3 (fee x1.5, exit and stop slip x2, seeded 20% skip, seeded 21-day block), registered on the adopted P4.
8. Second asset. The frozen P4 rules, with no re-fit, run on ETHUSDT over the same window and costs. It is a check, not a selection. A bad ETH result is reported.
9. Forward ledger. From 2026-09-20 00:00 UTC, the frozen P4 rules run on BTCUSDT in a USDT ledger with a cold start: all cash, and a regime that is already bullish waits for a fresh cross. The SHA-256 of the rule constants is pinned in `spec.json`. The ledger is extended as daily files are published and is never edited backward.

The meter's numbers come from `python3 -m research.rebuild` on a clean committed tree.

## P4 results

Recorded in `evidence/sleeves-20260929/` from source `3ac2c663128c6a8b423ccb9a29fc7476735ee1cf`; the summary is `RESULT.md` there, and `neighbors.json` holds the single-sleeve SMA 30, 35, 40, 45, and 50 accounts.

- Plateau centers: blow-off 0.61, reversal drop 0.11, bounce 0.07, depth 0.50, handoff 0.11. Together they print the P3 trades.
- The 4% close is included: all three tested distances pass step 4.
- Volatility scaling is not adopted: MDD 31.15% against 31.40%, CAGR 65.62% against 78.49%.
- P4: final CNY 490,442, cost-net CAGR 78.49%, continuous MDD 31.40% on 2024-10-13, 123 trades. Neither target is met, so economic qualification is `NOT_MET`. The seeded skip stress prints 75.01% and 32.63%.
- ETHUSDT with the frozen rules: P4 71.00% and 42.75%, single SMA 40 76.31% and 43.42%.
- Forward ledger: no fill through 2026-09-27.
