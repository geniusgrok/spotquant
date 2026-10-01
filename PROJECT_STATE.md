# Project state

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

2026-10-01 第三轮已接通 P4 的共享 session/Lifecycle，所有者显式 Demo
入口、持久订单分仓权重、跨日恢复、只读原生导出与两账户并发报告已实现。
108 项离线测试通过；共享会话回放 18 项通过，原件和命令见
`evidence/third-round-20261001/RESULT.md`。P4 日线经济账户的全部共有经济
字段/成交/每日曲线逐项复现，仍 78.49%/31.40%，NOT_MET。
原生 Demo/主网未运行，真实账户观察 0 日，NOT_QUALIFIED。
历史第二轮/P6基线与离线原型保留在对应证据，不代表当前共享执行原生通过。

2026-10-01: P5 rule deletions implemented and measured in
`evidence/simplify-20261001/RESULT.md`. All four deletions were rejected by
the registered matched-scenario rule; P4 remains the single default.
Reproduce with `python3 -m research.rebuild --simplify`. Long-term use is
one BTC spot project plus one perpetual project; the rule-deletion CLI is
research only and does not introduce another production model.

Spotquant is a default read-only Binance BTCUSDT spot path with explicit owner Demo validation. No futures and no leverage.

- CLI: `python3 -m spotquant status|run|snapshot|demo-check`. Explicit Demo uses matching UID and positive capital ceiling; live writes stay blocked. `run --execute` is blocked before config, credentials, and network.
- Default book P4: sleeves SMA 30, 40, and 50 on one USDT pool. Each sleeve: confirm 2, fresh cross, crash filter 0.50 on the inclusive 252-day highest close, a close at least 61% above the SMA, a crash reversal (11% down, then 7% up, still at least half under the inclusive 400-day highest close), a repair hold that ignores the SMA exit, the blow-off, and the 4% close until the close is back above the SMA and no more than 11% under that high, the 4% close, and a stop 28% under the running high. The stop is an amended `STOP_LOSS` price. An armed sleeve buys the free USDT plus the estimated proceeds of that open's exits, divided by the sleeves that hold nothing after those exits. The preview merges the sleeves of one open into one market buy and one market sell. The meter's peak starts at the fill open. An entry preview, and a hold with no recorded fill, anchor the stop on the completed close. A buy that follows the entry preview is recorded from account trades, sleeves of one signal day share it, and each sleeve's preview peak starts at the fill. A balance drop is recorded only when account sells leave exactly the coins of recorded sleeves. The 4% close sells the next open and does not cap the loss. A daily history that stops before the current UTC day is unknown. A failed observation keeps neither the model view nor `followed_position`. The observations table keeps the latest 1000 rows. Path marks use 00:00 UTC; the daily curve and the final mark use the end of that UTC day.
- Meter: `python3 -m research.rebuild` on daily bars, CNY via DEXCHUS, 0.1% conversion each way, spot taker fee 0.1%. The default trial name is P4.
- P4: final CNY 490,442, cost-net CAGR 78.49%, continuous MDD 31.40% on 2024-10-13. 123 closed trades, 42 net wins (43 gross), sleeves 40 and 50 still long. `targets_met` is false. The seeded skip stress prints 75.01% and 32.63%. The seeded 21-day buy skip contains no armed open. A fixed 21-day outage from 2020-03-01, which freezes signal exits and stop tightening, prints 65.08% and 31.40%.
- P3 (single SMA 40, the earlier default) remains the in-sample upper bound: final CNY 1,113,885, CAGR 101.67%, MDD 29.15% on 2020-03-16, 37 trades. P2 remains 75.19% and 33.10%. P1 remains 58.06% and 52.77%.
- ETHUSDT with the frozen rules: P4 71.00% and 42.75%, single SMA 40 76.31% and 43.42%. Forward ledger from 2026-09-20: no fill through 2026-09-27.
- Economic qualification: `NOT_MET` on P4. Native qualification: `NOT_QUALIFIED`. Public run --execute and live writes stay blocked; owner Demo validation is separate.
- Evidence: `evidence/sleeves-20260929/` (P4, source `dd25cfa5d98de139356b0d91ec0c7d2132edfd5e`), `evidence/forward/`, and the unchanged `evidence/rebuild-20260928/` (P1, P2, P3, frontier).

Everything was chosen on the full sample. Do not move the window or lower the targets.
