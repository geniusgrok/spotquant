# Handoff

Spotquant is the spot peer of coinquant: Binance BTCUSDT, long or cash, manual bounded session, execute blocked.

The acceptance targets are cost-net CAGR >= 100% and continuous MDD <= 30% from 2020-01-01 to 2026-09-20 exclusive, starting at CNY 10,000. P2 measured 75.19% CAGR and 33.10% MDD (final CNY 432,659, still long, drawdown on 2021-01-31). P1, the prior SMA 40 / 20% trail book, measured 58.06% and 52.77% (final CNY 216,652).

The default is SMA 40, two confirmed bullish closes, a fresh cross after each exit, a close at least half the 252-day highest close, and a 28% stop walked high-before-low. It is the highest terminal CNY in the causal long-or-cash search on this window. A float scan that matched P1 to the cent found a faster exit (SMA 15, confirm 3, no fresh latch) at about 57.31% CAGR and 29.59% MDD. That row has less terminal CNY and was not promoted.

Donchian channels, percent reversals, dip scale-in, shock-day exits, and a volatility-scaled trail did not beat P2 on both metrics. Equity flattening near 30% gave the drawdown cap back and cut the CAGR toward P1. Days this book spent in cash compounded to about 0.19, so the missing return is inside days it already held. Starquant's reported 120% uses both sides and leverage. Coinquant's reported 154% uses isolated leverage. Those mechanisms are outside this account.

Do not add leverage, shorts, or perpetual positions to close the gap. Do not move the window or lower the targets. Do not report a lookahead path as the account. A new rule has to go through `research.account.simulate` and be recorded with `python -m research.rebuild` on a clean `spotquant/` and `research/` tree.

`python -m research.rebuild` writes P2. `python -m research.rebuild --grid` writes `hold-grid.json` and leaves `frontier.json` in place.
