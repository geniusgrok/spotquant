# Project state

Spotquant is a read-only Binance BTCUSDT spot research path. No futures and no leverage.

- CLI: `python -m spotquant status|run`. `run --execute` is blocked before config, credentials, and network.
- Default model: SMA(40), confirm 2, fresh cross, crash filter 0.50 on the inclusive 252-day highest close, a close at least 60% above the SMA, a crash reversal (8% down, then 6% up, still at least half under the inclusive 400-day highest close), a repair hold that ignores the SMA exit, the blow-off, and the 4% close until the close is back above the SMA and no more than 20% under that high, and a stop 28% under the running high. The stop is an amended `STOP_LOSS` price. The meter's peak starts at the fill open. An entry preview, and a hold with no recorded fill, anchor the stop on the completed close. A buy that follows the entry preview is recorded from account trades, and that preview peak starts at the fill. When a crash reversal and an ordinary entry are both armed, the preview is the repair entry. The 4% close sells the next open and does not cap the loss. A daily history that stops before the current UTC day is unknown. A failed observation does not keep the previous model view. Path marks use 00:00 UTC; the daily curve and the final mark use the end of that UTC day. Low-before-high prints the same P3 account. The skip stress still misses the drawdown cap and does not revoke P3.
- Meter: `python -m research.rebuild` on daily bars, CNY via DEXCHUS, 0.1% conversion each way, spot taker fee 0.1%. The default trial name is P3.
- P3: final CNY 1,113,885, cost-net CAGR 101.67%, continuous MDD 29.15% on 2020-03-16. 37 closed trades, 16 wins, still long. `targets_met` is true.
- P2 remains the prior measurement: final CNY 432,659, CAGR 75.19%, MDD 33.10% on 2021-01-31.
- P1 remains the earlier measurement: final CNY 216,652, CAGR 58.06%, MDD 52.77%.
- Economic qualification: `MET` on P3. Native qualification: `NOT_QUALIFIED`. Execute stays blocked for that native reason.
- Evidence directory: `evidence/rebuild-20260928/`. Source commit of the P3 measurement: `b02c4af`.

The 4% close was chosen on the full sample. 3.8% prints the same account. 3.4% does not qualify. Do not move the window or lower the targets.
