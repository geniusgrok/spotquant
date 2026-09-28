# Handoff

Spotquant is the spot peer of coinquant: Binance BTCUSDT, long or cash, manual bounded session, execute blocked.

The acceptance targets are cost-net CAGR >= 100% and continuous MDD <= 30% from 2020-01-01 to 2026-09-20 exclusive, starting at CNY 10,000. P1 measured 58.06% CAGR and 52.77% MDD (final CNY 216,652). The registered daily SMA and trail grid does not meet either joint target; zero rows have MDD <= 30%. The default is SMA 40 with a 20% trail because that is the highest terminal CNY among trailingDelta values Binance spot can actually rest (maximum 2000 bips). SMA 40 with a 30% trail simulated 60.64% CAGR and 49.00% MDD. It is higher and still short of both targets; 30% is not a native trailingDelta.

Do not add leverage, shorts, or perpetual positions to close the gap. Do not move the window or lower the targets. Do not report a lookahead buy-at-the-low path as the account. A new rule has to go through `research/account.py` (or replace that single meter) and be promoted only by the written grid rule.

`python -m research.rebuild P1` writes the current default. `python -m research.rebuild --grid` rewrites `frontier.json`.
