# P4 真实会话执行与两账户观察 — 2026-10-01

本轮把 P4 执行接入实际 `session.run/cycle → execution.Lifecycle`。
默认只读；所有者显式 Demo 与离线回放使用同一协调器，主网写入仍关闭。
没有调用交易账户、使用凭据或下单。原生资格仍 NOT_QUALIFIED。

## 已实现及验证

- SQLite 在发送前保存稳定身份、原参数、信号日和分仓权重；原生订单
  编号与成交关联，支持部分成交、多订单和等量分仓。未知响应只查询原身份。
- 停机后的止损成交经回读归属到分仓；跨日恢复已准备的卖单；未发送的
  过期入场和被替代的保护准备有明确终态，不留永久未决记录。
- 部分卖出后的剩余仓位继续保护。现货锁币要求先确认撤单再挂替代止损；
  此空窗、最低名义、残币和实际触发语义仍须原生验证，不假称原子替换。
- Demo 请求使用实际 POST/DELETE，专用 UID、资金上限、状态范围和默认
  只读门均受检查。账户双读变化、外部委托/成交或无法解释的资金变化
  阻断新增风险。结果附执行代码 SHA，失效观察清除旧视图。
- `snapshot` 导出真实读取的现金、BTC（包括锁定）、原生止损数量与未覆盖
  数量；其他有余额资产导致未知，不遗漏资产后声称完整账户。
- 两账户并发只读采集，用合约 mark 统一估值，分别显示可用资金、保护、
  清算距离和入场余单；汇总净/总敞口，不把抵消后的净值冒充低总风险，
  不把保证金再次加进权益。未知、过期或超过 5 秒的跨账户差异拒绝汇总。
- 每次账户采集保留实际日期和原件哈希。重复同日不增加天数；失败记录
  保留，30 日统计只数同一账户组合的真实采集日，不能用行情回填补天数。

105 项完整离线测试和 compileall 通过。共享会话回放 17 项通过，见
[execution-final.json](execution-final.json)。它包括部分入场、ACK 丢失、
恢复、止损改价和进程停止后的模拟触发，使用合成行情，没有收益结论。
回放源码 `9f61c681436909fa6ba4ed9e5b22ffcfa4a60b3d`，Python 摘要
`18ec1fe7fb216647a7dda57c4d08da6000a390e265489cac170ae0b65c1d45c8`。
较早两份本轮回放只保留在 Git 历史 `9f61c68`。

经济复现 [P4-third-round.json](P4-third-round.json) 的全部共有经济字段、
完整成交与每日曲线逐项匹配原 P4：期末 CNY 490,442.50，CAGR 78.49%，
连续 MDD 31.40%，123 次平仓。它仍是日线经济测量器，不是六年的实际
会话/订单簿回放。100%/30% 目标仍 NOT_MET，未改变模型或降低目标。

## 所有者操作

复制 `config.demo.example.json`，填写专用 Demo UID、固定状态目录和上限，
Demo 凭据仅放本机原有环境变量。默认 300 秒/5 秒，无守护进程。

```sh
python -m spotquant demo-check --config demo.json
python -m spotquant demo-check --config demo.json --execute --authorize-uid <DEMO_UID>
python -m spotquant status --config demo.json
```

冷启动等待真实的新鲜穿越，不为验收强制生成入场。保留每次 `latest.json`
及原状态库、执行代码 SHA、交易所订单/成交回读。分别记录实际入场、分仓
归属、部分成交（若发生）、改单空窗、重启/正常停止及停进程后的保护触发；
未发生的事件记录为未验证。未知时先只读核对，不删除库或换空目录重开。
此入口不开放主网，也不自动认定 Demo 闭环通过。

分别使用 Spotquant 和 Coinquant 的实际配置进行账户观察。在 Spotquant 根目录：

```sh
python -m spotquant snapshot --config spot-demo.json --out /tmp/spot-fresh.json
# Coinquant 根目录：python -m coinquant snapshot --config perp-demo.json --out /tmp/perp-fresh.json
python -m research.operations combine /tmp/spot-fresh.json /tmp/perp-fresh.json
python -m research.operations collect --spot-config spot-demo.json --perp-config ../coinquant/perp-demo.json --perp-repo ../coinquant --out private-observations/2026-10-01.json
```

实际运行时选新的文件名，把私有账户记录保留在仓库之外。采集命令不带交易
参数，失败也记录实际观察日。两账户须同为 Demo 或同为 live；同一账户不能
被不同项目共管。清算和出场费用未进入线性 ±10% 冲击，不能据此授权新风险。
当前真实账户观察日为 0，原生交易/30 日验收均未完成。

长期保留一个现货系统与一个合约系统。现货 P4 与合约共用的是只读风险报告，
没有跨账户调钱、自动对冲或重复下单的第三执行服务。
