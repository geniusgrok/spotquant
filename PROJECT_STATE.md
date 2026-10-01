# Project state

本轮全部登记方向持续实施，见 research/complete-delivery-PROTOCOL.md。
原始20账户已完整结束，干净源2d1e5fe、原件SHA65cc08a7341eb2ef7d7b30641b783341edd7dc7781e8dfa10ae801b5b8c86022。
其中16账户通过；consensus4因合并卖出比例分配残币超过步长但金额不足保护门而失败，原件保留且不参与采用。
三个实际2500/5000/7500预算账户完成且通过资金/执行/795档案核验，原件SHA8e15ca5fd83c8c89c3c65545f81f079186964efce3abd6566554fa5cce5c79d6。
修复1fca802保留真实残币、逐订单累计实际SELL、晚到终态回包时不提交fold、只读/执行同元数据、战略视图平仓且未来可交易残余仍Unknown。
143离线检查通过；独立23账户/3085fills证明仅consensus4触发新增分支，其余16+3可保留原源与原结果。
正常场景795会话，固定outage跳过6为789，不能称每个outage也实际运行795次。
四组修复后完整重跑正在执行：spot-consensus-corrected.json，clean1fca802，日志/tmp/spot-consensus-corrected.log。
原spot-accounts-final.json虽完成20项运行，只有16项complete；完整可用于决策的混合源集合须待替换4项结束并通过assemble_spot校验。
预算不能缩放或混合策略；若候选通过，须实跑选中策略三个预算并独立列出同策略10000端点的联合资金结果。
旧435/a288残币失败及容量中断均排除。研究TMPDIR=/workspace/scratch/spotquant-complete-tmp，真实state归档规则不变。
原生实际案例0、观察0日，NOT_QUALIFIED；旧78.49%/31.40%为不同日线账本，不能充当实际session结果。
旧native模板/recovery-drill-final对应5faa旧runtime，最终默认决定后须生成当前源新零案例模板与合成恢复演练。
剩余Coin28+真实预算、Spot修复4/可能选中预算、全部归因和决策、独立终审及CI/PR集成继续，不另建进度账本。

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
