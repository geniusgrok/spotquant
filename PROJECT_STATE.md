# Project state

Spotquant is a read-only Binance BTCUSDT spot research path. No futures and no leverage.

- CLI: `python -m spotquant status|run`. `run --execute` is blocked before config, credentials, and network.
- Default model: SMA(40), confirm 2, fresh cross, crash filter 0.50 on the 252-day highest close, stop 28% under the running high. The stop is an amended `STOP_LOSS` price.
- Meter: `python -m research.rebuild` on daily bars, CNY via DEXCHUS, 0.1% conversion each way, spot taker fee 0.1%.
- P2: final CNY 432,659, cost-net CAGR 75.19%, continuous MDD 33.10% on 2021-01-31. 43 closed trades, still long. `targets_met` is false.
- P1 remains the prior measurement: final CNY 216,652, CAGR 58.06%, MDD 52.77%.
- Economic qualification: `NOT_MET`. Native qualification: `NOT_QUALIFIED`.
- Evidence directory: `evidence/rebuild-20260928/`. Source commit of the P2 measurement: `b30608d`.

Do not describe the registered grid as having cleared 100% CAGR and 30% drawdown. A successor may replace the model only with another causal long-or-cash rule measured by the same meter, window, and costs.
