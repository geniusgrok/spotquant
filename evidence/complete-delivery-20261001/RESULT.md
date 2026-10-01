# BTC 现货完整交付

**采用 P4 共识仓位作为默认预览和受控 Demo 路径。** 冻结的五个候选、四个场景全部获得完整且通过资金与执行审计的结果，共识是唯一通过原先登记采用门槛的候选。原 100% CAGR / 连续 MDD≤30% 目标仍 `NOT_MET`；原生执行 `NOT_QUALIFIED`，实际原生案例0，观察0自然日。

只用 BTCUSDT，2020-01-01 至 2026-09-20 UTC exclusive，初始 CNY10,000，无追加资金、杠杆或转账。正常账户实际运行795个300秒/5秒会话；固定2020年3月停机场景跳过6个起点，实际789次。停止后客户端不决策，已挂场所保护仍可触发。价格是日线 O→H(08h)→L(16h)→C(24h) 的历史代理，连续代理MDD不代表历史盘口实测。

## 冻结候选与四场景

| 候选 | 场景 | 成本后 CAGR | 连续代理 MDD | 期末 CNY | 审计 |
|---|---|---:|---:|---:|---|
| default | base | 47.7781% | 41.4842% | 137,897.86 | 通过 |
| default | fee150 | 46.8843% | 41.6981% | 132,390.26 | 通过 |
| default | slip2 | 46.8709% | 41.7277% | 132,308.65 | 通过 |
| default | outage | 47.7781% | 41.4842% | 137,897.86 | 通过 |
| consensus | base | 51.8503% | 41.2073% | 165,529.10 | 通过 |
| consensus | fee150 | 50.8899% | 41.4221% | 158,621.56 | 通过 |
| consensus | slip2 | 50.8761% | 41.4513% | 158,523.93 | 通过 |
| consensus | outage | 51.8503% | 41.2073% | 165,529.10 | 通过 |
| downside | base | 46.3597% | 41.6560% | 129,245.18 | 通过 |
| downside | fee150 | 45.4700% | 41.8782% | 124,057.63 | 通过 |
| downside | slip2 | 45.4584% | 41.9068% | 123,991.21 | 通过 |
| downside | outage | 46.3597% | 41.6560% | 129,245.18 | 通过 |
| funding | base | 43.0631% | 41.4850% | 110,902.55 | 通过 |
| funding | fee150 | 42.2062% | 41.6967% | 106,515.20 | 通过 |
| funding | slip2 | 42.1924% | 41.7257% | 106,445.71 | 通过 |
| funding | outage | 43.0631% | 41.4850% | 110,902.55 | 通过 |
| basis | base | 47.7756% | 41.4885% | 137,882.08 | 通过 |
| basis | fee150 | 46.8816% | 41.7018% | 132,373.72 | 通过 |
| basis | slip2 | 46.8680% | 41.7313% | 132,291.47 | 通过 |
| basis | outage | 47.7756% | 41.4885% | 137,882.08 | 通过 |

consensus基础年化51.8503%，比等份基线47.7781%提高4.0721个百分点；连续代理MDD从41.4842%降至41.2073%。最低压力场景年化50.8761%。所有四场景均满足与对应基线的约束，采用标准没有因结果调整。downside、funding、basis均未通过全场景约束；basis也没有达到基础改善幅度。

## 改造的实际作用

保留三个SMA分仓、入场/退出/保护规则、冷启动和新鲜穿越要求。只有存在真实新BUY且至少两个分仓处于实际看多的enter/hold时，提高该聚合买单到至少90%可用现金，并遵守原资金上限；不会生成新信号或重新平衡既有持仓。分仓订单显示聚合买单的建议份额，持久归属和执行仍按真实聚合成交。研究`default`显式保留原等份仓位，以后也不会因默认入口改变而偷换基线。

研究发现并修复了合并全卖的取整残币：真实BTC和费用继续完整保存，只有确认全量终态、意图确为整组全卖、逐订单累计实际SELL一致且残余满足严格小额条件时，战略视图才视为平仓。晚到终态回读时事务回滚，后续恢复处理；部分成交、刻意减半和旧状态歧义仍作为持仓或Unknown。只读与执行共享归属元数据，未来变成可交易金额的残币仍阻断新增风险。

## 实际预算账户

三个选中策略预算各重新运行795次会话，不能按10k曲线缩放。

| 初始 CNY | 策略 | 期末 CNY | CAGR | 连续代理 MDD |
|---:|---|---:|---:|---:|
| 2500 | consensus | 41,370.893634 | 51.8441% | 41.2045% |
| 5000 | consensus | 82,759.631018 | 51.8489% | 41.2065% |
| 7500 | consensus | 124,139.765799 | 51.8490% | 41.2075% |

原等份策略的2500/5000/7500预算另行保留，不能与共识策略10k末端混合。联合资金报告使用实际预算账户，总原始资金固定10k，无转账、再平衡或追加；联合日终MDD不能声称连续MDD通过。

## 证据来源与复现

原始20项矩阵保持原件：clean2d1e5fe31be610f2c840b3f077531f242fbe9a14，16项通过，共识4项因合并卖出残币失败并被排除。独立回放23账户/3085fills，证明新增处理分支仅影响原失败共识4项；其余16项和原3预算保持原源，不伪称新源重测。共识4项全部在clean1fca802ec89445e1b89dcc3d600dbfed721b8e34完整重跑，最终集合明确为16旧源+4新源。assemble_spot同时验证原件、逐账户SHA、审核脚本、执行源码及不可变Git测量树；没有把失败结果用于选择。

选中共识3预算测量于clean7b4b44e2ea7ba764ccf84e4bf6601b75fa6e646d，Python源码SHA d863ef763b84192e23aea53b5ca01fcdbf05ffaaf3d30233856c842c154540ee，与修复4项一致。默认入口落地源13deeb46350d0ac0311b6820c606645cff0974ff，独立2592组订单/决策/保护等价性比较通过；每分仓建议金额的修正不改变可执行聚合委托。146项离线检查通过。

| 原件 | SHA256 |
|---|---|
| [spot-accounts-final.json](spot-accounts-final.json) | `65cc08a7341eb2ef7d7b30641b783341edd7dc7781e8dfa10ae801b5b8c86022` |
| [spot-consensus-corrected.json](spot-consensus-corrected.json) | `cf075b86ba5df590e078a7c626c53cb20937be955cfb6ad8d41eae19b78645e8` |
| [spot-accounts-verified.json](spot-accounts-verified.json) | `439edad38e7a2af382597d204f35cd0a6372ca9da98cdcf4c964852329568e92` |
| [portfolio-spot-accounts-final.json](portfolio-spot-accounts-final.json) | `8e15ca5fd83c8c89c3c65545f81f079186964efce3abd6566554fa5cce5c79d6` |
| [portfolio-spot-consensus.json](portfolio-spot-consensus.json) | `b7fe8020df2c58e2910c57ef856a61b5905702a69bca30874cddc9c578755b0d` |
| [grouped-residual-zero-effect-proof.json](grouped-residual-zero-effect-proof.json) | `8ec61b66e008be4283903bf1c4207c1466d6ef7bee33bc371fd79bfae084f8a0` |

完整命令、历史快照与当前默认区分、原生六案例和恢复步骤见[交付指南](../../research/complete-delivery-GUIDE.md)。老`research.rebuild`的78.49%/31.40%是另一日线账本，不能用来衡量当前默认。所有历史都已研究，归因回归截距/HAC7区间仅为描述，不证明未来alpha，也不能与beta项相加解释几何CAGR。

当前共识运行源的零案例模板/验收输出为spot-native-template-consensus.json、spot-native-acceptance-consensus.json。recovery-drill-consensus留存纯合成会话、不可覆盖报告与SQLite备份：只读恢复余额/提交ID不变，外部0.1BTC偏差为Unknown；它不是原生案例。早期*-final模板/演练对应旧源，只保留历史。
