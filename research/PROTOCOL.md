# Spot account meter

The economic record is one continuous BTCUSDT spot account. Long or cash. No borrow, no short, no futures, no funding, no liquidation, and no leverage. The decision is `spotquant.model.Model`. A completed UTC daily close strictly above SMA(40) is bullish. Entry requires two consecutive bullish closes, a fresh cross since the last exit, and a close at least half of the inclusive 252-day highest close. The account buys at the next daily open. While long, a stop 28% under the running high is modeled by taking that day's high before its low. A gap through the inherited stop sells at the open. A close that is no longer bullish sells at the next open. There is no same-day re-entry. The 28% distance is wider than Binance spot `trailingDelta` (2000 bips), so the live preview is an amended `STOP_LOSS` `stopPrice`.

This meter is not `session.run`. The live session is read-only, previews a market order after a completed close, and does not fill. Cold start does not buy a regime that was already bullish. Those differences are why a preview is not the CAGR.

## Window and valuation

- Start `2020-01-01T00:00:00Z`, end `2026-09-20T00:00:00Z` exclusive.
- Initial CNY 10,000. No additions.
- USDT is valued at USD par. FRED DEXCHUS applies from 17:00 UTC on its observation date. The rate is ex-post valuation, not a decision input. SHA-256 of the committed file is `733c2bbccfd42448d72f8b7a7ee2cd744f1b3263ac1be88e34260b9c7b2ec874`.
- Conversion haircut 0.1% when CNY becomes USDT at the start, and again when equity is marked back to CNY.
- Spot VIP0 taker fee 0.1% each side. Entry slip 0.05%, exit slip 0.05%, stop slip 0.1%.
- A favorable mark updates the equity peak only. An adverse mark also updates the continuous drawdown.
- Daily bars are the official path because the daily files from the model origin through the end are contiguous. Hourly files inside the window have gaps. A 10% participation cap does not change fills at this account size: even a much larger terminal balance is far below 10% of BTCUSDT daily quote volume. No separate depth trial is reported.

## Selection

The default was chosen on the full window. There is no out-of-sample result.

`evidence/rebuild-20260928/frontier.json` is the earlier SMA-window by trail grid, measured when a single bullish close bought the next open and the trail was 20%. Its highest terminal CNY was SMA 40 / trail 30%. Its highest trail at or under 20% was SMA 40 / trail 20%, recorded as P1.

P2 replaces that default. The search kept the same costs, window, and long-or-cash account, and added confirmation, a fresh-cross latch, a 252-day crash filter, separate entry and exit averages, percent-reversal entries, dip scale-in, and shock-day exits. Ideas taken from the owner's other research were adapted to this account: Donchian channels, a ratchet trail, cooldown, and an equity flatten from the perpetual book; a four-hour ATR impulse and a two-close confirmation from the leveraged book. Shorts, funding, liquidation, isolated margin, and leverage were left out.

Promotion:

1. A row becomes the default only when cost-net CAGR is at least 100% and continuous MDD is at most 30%, choosing the highest terminal CNY among such rows.
2. Until that happens, the default is the highest terminal CNY among causal long-or-cash rows measured on this window.

No searched row met both targets. The highest terminal CNY is SMA 40 for entry and exit, confirm 2, fresh cross, crash filter 0.50, trail 0.28. That is P2. In the same float scan, which matched the official P1 figures to the cent, the highest CAGR with MDD at or under 30% was an exit SMA of 15, confirm 3, no fresh-cross latch, crash filter 0.50, and a trail of 0.28 or wider: about 57.31% CAGR, 29.59% MDD, final CNY about 209,905. It was not promoted. The shipped model uses one SMA for entry and exit, so that row was not remeasured by `research.rebuild`.

## Stresses

Same default model, one change at a time:

- fee ×1.5 (`--fee 0.0015`)
- exit slip ×2 and stop slip ×2 (`--exit-slip 0.001 --stop-slip 0.002`)
- seeded skip of 20% of daily opens (`--sequence skip`)
- one seeded 21-day block of opens (`--sequence block`)

The skip and block seeds do not look at prices. Economic qualification stays `NOT_MET` until a recorded trial meets both targets. Native qualification stays `NOT_QUALIFIED` regardless.

## What was searched besides the grid

The recorded default is `evidence/rebuild-20260928/P2.json`, source `b30608d43f3039f7cce5c1c72341ced3021007e0`: final CNY 432,658.9697193516804930799377, cost-net CAGR 75.19%, continuous MDD 33.10% on 2021-01-31, 43 closed trades, still long, `targets_met` false. P1 remains in the same directory as the prior 20% trail measurement. See `evidence/rebuild-20260928/RESULT.md`.

`python -m research.rebuild` writes P2. `python -m research.rebuild --grid` writes `hold-grid.json` under the current confirm, fresh-cross, and crash-filter constants. It leaves `frontier.json` in place.

On this book's own path, days spent in cash compounded to about 0.19, and the largest rally missed while flat was about 10%. The open gap to a 100% CAGR is inside the days already held. Forcing an equity flatten near 30% on the prior SMA book cut the CAGR back toward the P1 result. A lookahead that sells every losing day inside those holds can clear a much higher CAGR with almost no drawdown. That path is not tradable and is not a result of this meter.
