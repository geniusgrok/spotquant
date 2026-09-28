# Spot account meter

The economic record is one continuous BTCUSDT spot account. Long or cash. No borrow, no short, no futures, no funding, no liquidation, and no leverage. The decision is `spotquant.model.Model`: a completed UTC daily close strictly above its simple moving average is bullish. The position is bought at the next daily open and, while long, a 20% trailing stop is modeled by taking that day's high before its low. That is the approximation of a resting Binance spot `trailingDelta` of 2000 bips, which is the venue maximum. A close that is no longer bullish sells at the next open and cancels that day's trail. There is no same-day re-entry.

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

`python -m research.rebuild --grid` measures SMA windows `(20, 30, 40, 50, 80, 100, 120, 150, 200)` and trails `0.10, 0.15, 0.20, 0.25, 0.30` on the full window. Parameters were not chosen out of sample.

Promotion:

1. If any row has cost-net CAGR >= 100% and continuous MDD <= 30%, the default is the one with the highest terminal CNY.
2. Otherwise the default is the highest terminal CNY among trails `<= 0.20`, because only those can rest as spot `trailingDelta` while the process is stopped.
3. `best_final` still records the highest terminal CNY with no placeability filter.

No row in the registered grid met both targets. The placeable default is SMA 40 and trail 0.20. SMA 40 and trail 0.30 has a higher simulated terminal CNY and a lower drawdown than 0.20, and it still misses both targets. It is not the default, because a 30% stop is not a native trailingDelta. A once-a-day static stop is a different execution model and is not what the meter simulates.

## Stresses

Same default model, one change at a time:

- fee ×1.5 (`--fee 0.0015`)
- exit slip ×2 and stop slip ×2 (`--exit-slip 0.001 --stop-slip 0.002`)
- seeded skip of 20% of daily opens (`--sequence skip`)
- one seeded 21-day block of opens (`--sequence block`)

The skip and block seeds do not look at prices. Economic qualification stays `NOT_MET` until a recorded trial meets both targets. Native qualification stays `NOT_QUALIFIED` regardless.

## What was searched besides the grid

The grid is the registered account. Separate diagnostic scans on the same window, costs, and long-or-cash constraint (daily breakouts, percent reversals, capitulation entries, hourly moving averages) also failed to produce 100% CAGR with drawdown at or under 30%. A lookahead that buys the exact pivot low and sells on a 30% trail can clear the return target, and the same pivots entered only after a confirmation bounce cannot. That lookahead path is not tradable and is not a result of this meter.
