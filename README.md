# Spotquant

个人使用的 Binance BTCUSDT 现货交易程序。当前策略为 SMA30 / 40 / 50 分仓、ATR 止损和拥挤度入场调整。只做多或持有 USDT，不借币、不做空、不使用杠杆。默认只读，手动启动有限会话，没有后台守护进程；仅依赖 Python 3.13 标准库。

## 配置与只读运行

在仓库目录内使用 Python 3.13，无需安装第三方包。

```sh
cp config.example.json config.json
python3 -m spotquant status --config config.json
python3 -m spotquant run --config config.json
python3 -m spotquant snapshot --config config.json --out /tmp/spotquant-snapshot.json
```

先填写 `config.json` 的 `account_uid`，使用核验一致的 Binance UID；`state_dir` 使用该账户固定目录。配置不存凭据。

| 字段 | 用法 |
|---|---|
| `environment` | `live`（默认）或 `demo`，账户、凭据和状态分别隔离 |
| `session_seconds` | 会话时长 1～86400 秒，默认 300 |
| `poll_seconds` | 轮询间隔 1～60 秒，默认 5，不超过会话时长 |
| `capital_limit_usdt` | 可选正值十进制字符串，限制全账户持仓名义；不是亏损上限 |

主网凭据从 `SPOTQUANT_BINANCE_KEY`、`SPOTQUANT_BINANCE_SECRET` 读取，Demo 从 `SPOTQUANT_BINANCE_DEMO_KEY`、`SPOTQUANT_BINANCE_DEMO_SECRET` 读取。两套凭据互不回退。

`status` 观察一次；`run` 在期限内读取行情、账户和订单状态，输出策略与订单预览。`snapshot` 导出新鲜只读账户 JSON，输出文件必须不存在。`run --execute` 被程序阻止，主网下单不可用。

## Demo 会话

```sh
cp config.demo.example.json demo.json
python3 -m spotquant demo-check --config demo.json
python3 -m spotquant demo-check --config demo.json --execute --authorize-uid <DEMO_UID>
```

填写专用 Demo UID、固定状态目录和正值资金上限。执行需显式 `--execute`，且 `--authorize-uid` 与配置一致。冷启动等待新鲜穿越，不强造入场；SELL 优先于新 BUY，不预支预计卖出所得。

订单发送前保存原生身份和分仓权重，部分成交按实际回读归属。未知委托、成交或余额变化阻止新增风险；丢失回包后查询原身份，不重复发送未知订单。ATR14 保护距离为 `clip(4 × ATR14 / 已完成收盘, 10%, 30%)`，不会放宽确认生效的止损。现货更换止损需先撤旧单再挂新单，存在保护空窗。

Demo BUY 前读取当前手续费和交易所最小额，按最新成交价估算扣费及数量取整后的可保护数量。启用第三资产手续费折扣、费率接口不可用，或历史第三资产费用尚未估值时拒绝新增 BUY；程序不更改交易所设置，已有持仓的止损与减仓仍可继续。第三资产成交费会保留原资产数量并标记财务估值未完成；BTC/USDT 余额及订单归属仍须核对。止损穿价判断使用新取的最新成交价，均价只用于名义金额过滤。明确拒绝的止损会尝试市价减仓；网络超时或交易所内部错误仍按未知订单原身份查询。会话到期即停止新增 BUY；正常到期后如有未决订单，最多在后续 30 秒内尝试一次原身份恢复及保护收尾。无法确认的剩余仓位在本地报告中标为人工接管。

会话结束后保留确认的原生保护；进程停止期间不再计算移动止损或模型退出。Demo 回读和只读快照不代表主网执行已验证。

固定状态目录的 `latest.json` 包含 `risk_state`（余额、已确认止损覆盖、未保护数量、第三资产费用估值及观察时间）、`execution_evidence`（报价、成交/止损回读、费用及滑点）和 `runtime_identity`。可选环境变量 `SPOTQUANT_SOURCE_SHA` 记录部署提交，未设置时为空。停止后的快照并非实时账户状态；`manual_takeover` 为真或未保护数量未知时需人工核对交易所。不要公开提交账户报告。

## 账户状态

固定 `state_dir` 保存 `intents.sqlite`、成交归属、有限观察记录、`latest.json` 和执行锁。同一账户只使用一台机器、一个客户端，Demo 与主网不共用目录。重启继续原模型和订单身份；不兼容状态拒绝接管，不删除数据库或换空目录绕过。持有中的袖不补仓，关闭后须满足原 fresh-cross；残币仍保留归属并计入持仓和风险，达到数量步长的残币保守阻止新增 BUY。

## 离线检查与排障

```sh
python3 -m compileall -q spotquant tests
python3 -m unittest discover -s tests
```

检查使用离线输入，不连接账户。`blocked` 表示当前权限或配置不允许操作；`unknown` 表示无法确认数据、订单或账户状态，不能当作已成交。先核对 UID、环境、固定状态目录、凭据变量及报告原因；状态不兼容或余额/订单归属不一致时保留原文件，人工核对，不清空重试。

开发约定见 [AGENTS.md](AGENTS.md)。历史代码、回测及交付材料见 [归档分支](https://github.com/geniusgrok/spotquant/tree/archive/pre-slim-20261008)。

项目供仓库所有者个人使用，公开可见不授予第三方使用许可，详见 [LICENSE](LICENSE)。
