# Project state

Spotquant is a read-only Binance BTCUSDT spot research path. No futures and no leverage.

- CLI: `python -m spotquant status|run`. `run --execute` is blocked before config, credentials, and network.
- Default model: SMA(40), confirm 2, fresh cross, crash filter 0.50 on the 252-day highest close, a 60% blow-off, a crash reversal that is still 50% under the 400-day highest close, a 4% close under the entry fill on every position that is not in that reversal, and a stop 28% under the running high. The stop is an amended `STOP_LOSS` price.
- Meter: `python -m research.rebuild` on daily bars, CNY via DEXCHUS, 0.1% conversion each way, spot taker fee 0.1%. The default trial name is P3.
- P3: final CNY 1,113,885, cost-net CAGR 101.67%, continuous MDD 29.15% on 2020-03-16. 37 closed trades, 16 wins, still long. `targets_met` is true.
- P2 remains the prior measurement: final CNY 432,659, CAGR 75.19%, MDD 33.10% on 2021-01-31.
- P1 remains the earlier measurement: final CNY 216,652, CAGR 58.06%, MDD 52.77%.
- Economic qualification: `MET` on P3. Native qualification: `NOT_QUALIFIED`. Execute stays blocked.
- Evidence directory: `evidence/rebuild-20260928/`. Source commit of the P3 measurement: `35afe03`.

The 4% close was chosen on the full sample. 3.8% prints the same account. 3.4% does not qualify. Do not move the window or lower the targets.
