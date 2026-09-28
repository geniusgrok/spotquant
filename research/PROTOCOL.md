# Spot account meter

The economic record is one continuous BTCUSDT spot account. Long or cash. No borrow, no short, no futures, no funding, no liquidation, and no leverage. The decision is `spotquant.model.Model`. A completed UTC daily close strictly above SMA(40) is bullish. An ordinary entry needs two such closes, a fresh cross since the last exit, and a close at least half the inclusive 252-day highest close. The account buys at the next daily open. While long, a stop 28% under the running high is modeled by taking that day's high before its low. A gap through the inherited stop sells at the open. A close that is no longer bullish sells at the next open. A close at least 60% above the SMA is a blow-off and sells the next open. A crash reversal is a completed day up at least 6% after a day down at least 8%, while the close is still at least half under the inclusive 400-day highest close. That signal may buy the next open. Until the close is back above the SMA and no more than 20% under that 400-day high, the repair position ignores the SMA exit, the blow-off, and the 4% close. Any other open position sells the next open when a completed close is 4% or more under the entry fill. When that close shares an open with an SMA exit or a blow-off, the recorded kind is `adverse`. There is no same-day re-entry. The 28% distance is wider than Binance spot `trailingDelta` (2000 bips), so the live preview is an amended `STOP_LOSS` `stopPrice`.

This meter is not `session.run`. The live session is read-only, previews a market order after a completed close, and does not fill. Cold start does not buy a regime that was already bullish. The meter's peak starts at the fill open. An entry preview has no fill yet, so its stop is 28% under the completed close and is amended to that fill open. When a crash reversal and an ordinary entry are both armed, the preview is the repair entry, because the meter records that fill as a repair hold. During repair the preview peak is the high since the repair fill. Any other hold uses the bullish-streak high, which can start before the fill. Those differences are why a preview is not the CAGR.

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

The default was chosen on the full window. There is no out-of-sample result.

`evidence/rebuild-20260928/frontier.json` is the earlier SMA-window by trail grid, measured when a single bullish close bought the next open and the trail was 20%. Its highest terminal CNY was SMA 40 / trail 30%. Its highest trail at or under 20% was SMA 40 / trail 20%, recorded as P1.

P2 replaced that default. The search kept the same costs, window, and long-or-cash account, and added confirmation, a fresh-cross latch, a 252-day crash filter, separate entry and exit averages, percent-reversal entries, dip scale-in, and shock-day exits. Ideas taken from the owner's other research were adapted to this account: Donchian channels, a ratchet trail, cooldown, and an equity flatten from the perpetual book; a four-hour ATR impulse and a two-close confirmation from the leveraged book. Shorts, funding, liquidation, isolated margin, and leverage were left out.

Promotion:

1. A row can become the default only when cost-net CAGR is at least 100% and continuous MDD is at most 30%.
2. When several distances of one rule clear both targets, the default is the round value on a flat step that is not next to a distance that misses a target. A higher terminal value on that edge is recorded and not promoted.
3. Until any causal long-or-cash row meets both targets, the default is the highest terminal CNY on this window.

P2 was the highest terminal CNY before this book: SMA 40 for entry and exit, confirm 2, fresh cross, crash filter 0.50, trail 0.28. It did not meet either target. In the same float scan, which matched the official P1 figures to the cent, the highest CAGR with MDD at or under 30% on that earlier search was an exit SMA of 15, confirm 3, no fresh-cross latch, crash filter 0.50, and a trail of 0.28 or wider: about 57.31% CAGR, 29.59% MDD, final CNY about 209,905. It was not promoted.

P3 adds three causal rules on that core and is the default. A close at least 60% above the SMA sells the next open. One trade in the window uses it: the 2020-10-10 entry sells on the 2021-01-08 open. Thresholds from just above the 2021-01-06 extension through the 2021-01-07 extension sell that same open. A crash reversal may enter while repair holds through SMA whipsaws; the trades that use it are the 2020-03-13 reversal, bought on 2020-03-14, and the 2022-11-10 reversal, bought on 2022-11-11. The May 2021 reversal is outside the 50% depth filter. A non-repair position also sells the next open after a close 4% or more under its fill. Against the same rules with that close disabled, the two exits that change are 2022-02-08, sold on 2022-02-14 instead of 2022-02-18, and 2022-03-02, sold on 2022-03-04 instead of 2022-03-05. Other bars labeled `adverse` are sold on the same open the SMA exit would have used.

The 4% distance is the round value on a flat step, which is rule 2 above. On this Decimal meter, 3.8% and 4.0% print the same account. 3.5% through 3.7% print a higher one, final CNY 1,118,235.000942666294587944406, because the February 2022 trade sells one day earlier. 3.4% sells the 2021-01-30 trade, which otherwise runs to 2021-03-25 for about +52.58%, and the account falls to about 88.5% CAGR. 4.2% through 4.4% still clear both targets at a lower terminal value. 4.5% no longer sells either 2022 trade early and reverts to the pre-rule book: final CNY 989,597.0504440564507983512087, CAGR 98.1505%, MDD 31.5786% on 2022-03-18. That book misses both targets and is not registered. P3 keeps 4.0%. The 3.5–3.7% step is one tenth of a point from the distance that deletes the January 2021 winner, so it stays recorded and is not the default.

## Stresses

Same default model, one change at a time:

- fee ×1.5 (`--fee 0.0015`)
- exit slip ×2 and stop slip ×2 (`--exit-slip 0.001 --stop-slip 0.002`)
- seeded skip of 20% of daily opens (`--sequence skip`)
- one seeded 21-day block of opens (`--sequence block`)

The skip and block seeds do not look at prices. Native qualification stays `NOT_QUALIFIED` regardless. Economic qualification is `MET` only on a recorded trial that meets both targets. A stress that misses one target does not revoke the base trial.

## What was searched besides the grid

P2 remains `evidence/rebuild-20260928/P2.json`, source `b30608d43f3039f7cce5c1c72341ced3021007e0`: final CNY 432,658.9697193516804930799377, cost-net CAGR 75.19%, continuous MDD 33.10% on 2021-01-31, 43 closed trades, still long, `targets_met` false. P1 remains the prior 20% trail measurement. The default is now P3. See `evidence/rebuild-20260928/RESULT.md`.

`python -m research.rebuild` writes P3. `python -m research.rebuild --grid` writes `hold-grid.json` under the current constants, including the blow-off, the crash reversal, and the 4% close. It leaves `frontier.json` and the P1 and P2 files in place.

The account is still long at the window end, so the last mark uses the final daily close and does not charge an exit fee. The continuous MDD of P3 is on 2020-03-16, inside the crash-reversal hold that began on 2020-03-14. A lookahead that sells every losing day is not a result of this meter.
