# Active alpha/beta mechanism round —2026-10-02

User approved all eight directions; spec/protocol/plan in research/alpha-beta-*. BTC-only, current consensus/incumbent baselines and original economics/native gates unchanged. Worktree pair /workspace/btc-alpha-beta-next/{spotquant,coinquant}; original mains remain unchanged. Progress ledger is this existing file; task briefs/reports/diffs are external /workspace/btc-alpha-beta-next/review.

Ruling: use mirrored external worktrees to preserve existing ../coinquant/../starquant paths without modifying market/FX inputs — cost if wrong: path repair, not economic changes.
Ruling: use existing PROJECT_STATE/HANDOFF rather than an additional SDD progress file to honor repository continuity rules — cost if wrong: manual reconstruction of task status; commits/reports remain retained.

| Preflight | Producer / consumer or constraint | Result |
|---|---|---|
| Task1 self | Coin four fixed mechanisms plus incumbent; real runner/audit, no default change | consistent |
| Task2 self | Spot six fixed variants plus consensus;20/80 core subpools and real ownership | consistent |
| Task3 self | actual raw curves/calibration, timestamp validation, no scaling | consistent |
| Task4 self | all48+10risk, conditional combos, Coin robustness, reviewed adoption | consistent |
| Task1/3 | Coin nested results + opportunity_ledger consumed by evaluator | fixed schema, preserve source/input hashes |
| Task2/3 | Spot flat results + opportunity_ledger consumed by evaluator | fixed schema, keep consensus baseline |
| Task1/4 | frozen Coin engine, later selected bridge equivalence | no core edits while financial worker active |
| Task2/4 | frozen Spot engine/core ledger, later selected bridge equivalence | no core edits while financial worker active |
| Task3/4 | deterministic calibration/combo output, completed inputs | false complete/native never promote |

Ruling: matched economic selection constraints compare identical stress scenes on unscaled actual accounts; separately rerun training-calibrated base accounts for achieved risk/alpha diagnosis — because the spec separates selection from risk-match claims; cost if wrong: add a risk-based adoption constraint before promotion.
Ruling: test the exact all-compatible eligible combination, never subsets; if that combination is rejected keep the best eligible singleton, ranking any accepted combo by the same worst-stress CAGR with singleton priority on ties — because an unevaluated or inferior combination cannot justify default replacement; cost if wrong: combination remains research-only.

Ruling: Coin trailing high excludes the 4h bar that overlaps actual entry; only bars opening at/after confirmed entry contribute high, with initial anchor at the actual fill — avoids a pre-entry wick becoming a stop under the completed-bar spec; cost if wrong: conservatively later tightening, fully disclosed before outcomes.

Task1: complete — source c2a01d1 + attribution fix31e8b1e; 359 offline checks,13focused and exact real three-session no-op monetary equality. Independent spec PASS/quality APPROVE at ../review/task1-fix1-review.md. Full-window/economic evidence remains Task4; production unchanged.
Task2: running /root/alpha_spot_implementation; BASEa3aed915 (Spot); requirements/report ../review/task2-brief.md /task2-report.md. Task3: pending; Task4: pending.
Ruling: core cold-start retains saved-checkpoint/entries_after/new-day and original execution gates, without a tactical SMA200 fresh-cross latch; original three tactical signals unchanged — permanent core otherwise gains an unregistered initial timing rule; cost if wrong: conservative comparison requires a separately registered variant, never post-outcome switching. Protocol clarified before any full new outcome.


--- Previous completed delivery ---

# Project state

BTC-only完整工程及历史研究已完成，长期入口为Coinquant合约与Spotquant现货，Starquant只留研究参考。工程默认采用P4共识；原100% CAGR/连续MDD<=30%目标仍NOT_MET，真实原生案例0、实际观察0自然日，NOT_QUALIFIED。

全部20现货与28合约候选/压力账户完整且资金/执行审计通过，七个固定经济对照、九个实际预算账户及两套各五配比固定总10k组合已完成，无追加、转账或再平衡。正常795会话、Spot固定outage789；日终共同曲线2454天。预算及组合仅测base，联合连续MDD及四场景压力未验证。

现货consensus唯一通过冻结全部采用门槛，基础51.8503% CAGR/41.2073%连续代理MDD，原等份47.7781%/41.4842%。共识改造保持真实信号，仅将已有新BUY在至少两个实际看多分仓时提高到至少90%可用现金，受原资金上限约束。研究default显式冻结等份。默认源13deeb4的2592组执行等价性通过；当前150项离线检查通过，核心标准库依赖不变。

现货原件clean2d1e5fe保留16有效行及4失败共识行，失败排除。全卖残币/晚终态事务回滚/同归属修复源1fca802只需重跑原4项，修复4项全部完整通过。组装清楚保留16旧源+4新源，不重标测量源；最终集合SHA439edad38e7a2af382597d204f35cd0a6372ca9da98cdcf4c964852329568e92。原等份与共识各2500/5000/7500真实预算均独立795会话；共识预算源7b4b44e/d863ef7。

Coin原995全28保留，exclusive派生3213仅去除slow-trend4项恰在END的负资金费，原24项资金/成交/MDD不变。所有替代未通过冻结压力约束，incumbent保留119.2284%/44.1051%。3213实际2500/5000/7500预算全部完成；资金规模改变成交和权益反馈路径，不能缩放、用小本金CAGR替代10k目标或认定某配比未来最优。

assessment.json明确绑定48项来源、五个实际输入文件SHA、Coin市场/协议/会话身份及终点派生。联合beta从实际合计USDT/CNY日收益回归，不平均策略beta；算术残差/HAC7区间仅描述全样本历史，没有策略选择调整或未来alpha证明。年度2026仅到9月19日；非零余额天数含残币，另列>=5USDT名义日数。Spot OHLC路径/Coin分钟与量上界皆历史代理，不是盘口或原生执行。

当前原生零模板为spot-*-consensus和perp-*-current。recovery-drill-consensus保留纯合成SQLite及会话报告，只读恢复余额/IDs不变、external BTC Unknown；它不计原生案例或实际30自然日。六原生案例、账户核对、保护空窗及实际30日仍须由所有者真实事件完成；本轮没有读取交易所凭据、访问真实账户、发订单/转账/设置。

交付证据、完整指标CSV与图表见evidence/complete-delivery-20261001/ALPHA_BETA.md、RESULT.md、manifest.json；复现及所有者步骤见research/complete-delivery-GUIDE.md。源/资金/默认改造的独立审查记录随证据保留。最终CI与正常集成记录以Spot PR8、Coin PR56及Star PR12的公开状态为准，不再有运行中研究进程。本次用户已经授权全部工程与正常PR集成，无需重复确认；不得据此启动真实账户写入。
