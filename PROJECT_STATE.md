# Project state

Spotquant is a read-only Binance BTCUSDT spot research path. No futures and no leverage.

- CLI: `python -m spotquant status|run`. `run --execute` is blocked before config, credentials, and network.
- Default model: SMA(40), trailingDelta 20% (2000 bips). Chosen as the highest terminal CNY among trails the venue can rest, after no registered row met both targets.
- Meter: `python -m research.rebuild` on daily bars, CNY via DEXCHUS, 0.1% conversion each way, spot taker fee 0.1%.
- Economic qualification: `NOT_MET`. Native qualification: `NOT_QUALIFIED`.
- Evidence directory: `evidence/rebuild-20260928/`.

Do not describe the registered grid as having cleared 100% CAGR and 30% drawdown. A successor may replace the model only with another causal long-or-cash rule measured by the same meter, window, and costs.
