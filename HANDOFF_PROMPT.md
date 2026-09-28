# Handoff

Spotquant is the spot peer of coinquant: Binance BTCUSDT, long or cash, manual bounded session, execute blocked.

The acceptance targets are cost-net CAGR >= 100% and continuous MDD <= 30% from 2020-01-01 to 2026-09-20 exclusive, starting at CNY 10,000. P3 measures 101.67% CAGR and 29.15% MDD (final CNY 1,113,885, still long, drawdown on 2020-03-16). P2 measured 75.19% and 33.10% (final CNY 432,659). P1, the SMA 40 / 20% trail book, measured 58.06% and 52.77% (final CNY 216,652).

The default keeps SMA 40, two confirmed bullish closes, a fresh cross, the 252-day crash filter, and the 28% stop. It also sells a close 60% above the SMA, may buy the open after an 8% down day and a 6% up day while price is still 50% under the 400-day highest close, and sells any position that is not in that repair the next open after a close 4% under the fill. The 4% step matches 3.8%. The 3.5–3.7% step has a higher terminal value and was not selected because 3.4% sells the 2021-01-30 winner. Selection is full-sample. There is no out-of-sample result.

Starquant's reported 120% uses both sides and leverage. Coinquant's reported 154% uses isolated leverage. Those mechanisms stay outside this account. `run --execute` stays blocked. Native qualification stays `NOT_QUALIFIED`. Economic qualification on the recorded P3 trial is `MET`. The seeded 20% skip stress misses the drawdown cap.

Do not add leverage, shorts, or perpetual positions. Do not move the window or lower the targets. Do not report a lookahead path as the account. A replacement rule has to go through `research.account.simulate` and be recorded with `python -m research.rebuild` on a clean `spotquant/` and `research/` tree.

`python -m research.rebuild` writes P3. `python -m research.rebuild --grid` writes `hold-grid.json` and leaves `frontier.json`, P1, and P2 in place.
