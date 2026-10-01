# Handoff

本轮全部登记方向持续实施，见 research/complete-delivery-PROTOCOL.md。
已修复已归属SELL后的分数BTC遗失，保留真实残币并正确重建新入场；
200会话资金/恢复审计及四情景串并行逐字段验证通过。实际20候选账户与
3预算账户从干净源2d1e5fe运行，资金代码为独立审查通过的53df29c。
旧43521a2中断矩阵、a288297残币失败预算及/tmp容量失败运行均排除，
不据部分年化晋升。TMPDIR已移至/workspace/scratch/spotquant-complete-tmp，
保留真实运行归档规则；完整输出spot-accounts-final.json及
portfolio-spot-accounts-final.json完成后再审计。131项离线检查通过。
当前归因helper a7c738d增加固定BTC/现金及滞后波动基准、全部候选币种
匹配回归。新的recovery-drill-final合成入场/备份/隔离重启保持余额与
submissionIDs，物质外部BTC仍Unknown。原生工具只查文件结构，真实
native案例0、实际观察0日，NOT_QUALIFIED。旧78.49%/31.40%来自不同日线
账本，不能充当实际会话结果。剩余完整账户、归因/选择、独立审查及
CI/PR集成继续；不另建进度账本。

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
