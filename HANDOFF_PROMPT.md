# Handoff

本次完整交付已开始实施全部登记方向，见 research/complete-delivery-PROTOCOL.md。
完整20组账户正在实际 session.run/Lifecycle 上运行，记录源43521a2；3种实际
资金预算账户源a288297，不能按10k曲线缩放。完成前不引用部分年化或晋升。
127项离线检查已通过；执行行情是明确的日OHLC高→低线性代理，不是原生盘口。
持久增量fills、不可覆盖会话报告、在线backup、restore-check已实现；原件恢复
演练保留在 evidence/complete-delivery-20261001/recovery-drill，未知物质外部BTC
不接管。原生验收工具只核验提供的文件结构，不能认证Binance或放开权限；
真实native证据和实际观察日数仍为0，NOT_QUALIFIED。旧78.49%/31.40%仅为
不同日线账本的历史记录，不能冒称新的会话账户结果。剩余归因、采用决策、
完整审查和CI/PR集成随全账户审计完成，不另建进度账本。


Third-round entry: `evidence/third-round-20261001/RESULT.md`. P4 owner Demo
and synthetic replay now share session.run/cycle and execution.Lifecycle.
Durable allocations resolve multiple/equal groups; unknown sent identities
never resubmit; prepared reductions and stop replacements recover across days.
108 offline tests and the 18-case replay pass. Native exports and concurrent
account collection are implemented; actual account observations remain zero.
No native Demo/Live closure or 30-day proof. Public run --execute stays blocked.
P4 daily economic replay is unchanged: 78.49%/31.40%, NOT_MET; it is not a
historical replay of the new execution lifecycle. Older second-round prototype
results remain in history and must not be described as native execution.

Read `evidence/simplify-20261001/RESULT.md` for P5: all registered rule
deletions rejected; P4 remains the default. Long-term destination is one BTC
spot and one BTC perpetual project. Research may retain multiple candidates.

Spotquant is the spot peer of coinquant: Binance BTCUSDT, long or cash, manual bounded session, default read-only, owner Demo validation, live execution blocked.

The acceptance targets are cost-net CAGR >= 100% and continuous MDD <= 30% from 2020-01-01 to 2026-09-20 exclusive, starting at CNY 10,000. They were not lowered. The default book P4 (SMA sleeves 30, 40, and 50 on one USDT pool) measures 78.49% CAGR and 31.40% MDD (final CNY 490,442, 123 trades, drawdown on 2024-10-13), so its economic qualification is `NOT_MET`. P3, the single SMA 40 book, measured 101.67% and 29.15% (final CNY 1,113,885, drawdown on 2020-03-16) and is kept unchanged as the in-sample upper bound: SMA 35, 45, 30, and 50 alone print 59.08%, 94.83%, 58.93%, and 70.37%, and the top three trades carry about 48% of the positive log return. P2 measured 75.19% and 33.10%. P1, the SMA 40 / 20% trail book, measured 58.06% and 52.77%. The owner chose P4 to trade in-sample return for less dependence on one parameter. Say so, and say `NOT_MET`, when quoting P4.

P4 was declared before it was measured (`research/PROTOCOL.md`, "P4 protocol"): sleeves fixed once; the blow-off and crash-reversal distances at the centers of the plateaus on which the single SMA 40 book prints identical trades (0.61, 0.11, 0.07, 0.11); the 4% close kept because 3.5%, 4.0%, and 4.5% all pass; volatility scaling tested once and not adopted (MDD 31.15%, CAGR 65.62%). ETHUSDT with the frozen rules: P4 71.00% and 42.75%, single SMA 40 76.31% and 43.42%. `evidence/forward/forward.json` is the 2026-09-20 backfill and is not a pre-registered sample. The live ledger starts 2026-09-29 in `evidence/forward/forward-20260929.json`. ETH still being profitable does not show the rule is robust off BTC; the ETH drawdown is about 43%.

Live path: each sleeve is a `Model`. An entry preview anchors the stop on the completed close; a hold does too until a followed fill is recorded, and the stop then starts at that fill. Sleeves previewed on one signal day share one fill equally. A balance drop is recorded as a sleeve exit only when account sells leave exactly the coins of recorded sleeves; a full transfer with no sell is unknown. A crash reversal is previewed as a repair entry even when the ordinary entry is also armed. The 4% close sells the next open and does not cap the loss. Checkpoints recompute the crash filter and the crash reversal, and reject a rehashed flag that the closes do not support. A daily page that stops before the current UTC day is unknown. A failed observation keeps neither the model view nor `followed_position`. The observations table keeps the latest 1000 rows. Selection is full-sample. There is no out-of-sample result.

Starquant's current causal baseline is 117.31%; Coinquant M10 is 118.24%.
Both are leveraged perpetual research under different execution/schedule/FX
assumptions; they cannot be ranked against each other or mixed into this spot
account. `run --execute` stays blocked. Native qualification is `NOT_QUALIFIED`.

Do not add leverage, shorts, or perpetual positions. Do not move the window or lower the targets. Do not report a lookahead path as the account. A replacement rule has to go through `research.account.simulate_sleeves` (one sleeve prints the same account as `simulate`), be declared in the protocol before it is measured, and be recorded with `python3 -m research.rebuild --suite` on a clean `spotquant/` and `research/` tree. Writing into `evidence/rebuild-20260928/` is refused.
