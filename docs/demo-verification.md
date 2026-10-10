# Demo 执行核对

这份说明用来在 Binance Spot Demo 上核对当前 BTCUSDT 现货程序的真实下单路径。策略规则不变。回测摘要里的 `native_execution_verified` 仍由人工结论决定；本工具即使全部通过，也不会把它写成 true。

Demo 的成交和盘口是交易所模拟的。这里能核对的是请求是否签到 Demo 主机、订单身份、回读、止损价、撤单后的空窗、进程中断后的 sqlite 恢复，以及本地账本和交易所历史是否一致。滑点不在核对范围内。

程序只从 `SPOTQUANT_BINANCE_DEMO_KEY` 和 `SPOTQUANT_BINANCE_DEMO_SECRET` 读取 Demo 凭据，不读取主网那一对，也不把凭据写进配置或报告。密钥由账户所有者在 Demo 页面创建。

## 硬边界

- 配置 `environment` 必须是 `demo`。
- `demo-verify` 的 `capital_limit_usdt` 必须是正的十进制字符串，且不超过 `100`。
- 每一笔请求的主机必须是 `https://demo-api.binance.com`。`api.binance.com`、`api1.binance.com` 至 `api4.binance.com`、`api-gcp.binance.com` 和旧的 `testnet.binance.vision` 都会在发送前被拒绝。
- 未知结果只按原客户订单号查询，不另起一笔。
- 杀进程、丢响应、时钟偏移和 429 只有带 `--faults` 才会运行。

## 准备 Demo 密钥

官方说明：<https://github.com/binance/binance-spot-api-docs/blob/master/demo-mode/general-info.md>。REST 基址是 `https://demo-api.binance.com`。

1. 登录 Binance 后进入 Demo Trading。页面上应能看出当前是 Demo（例如提供返回实盘的入口）。在这个界面里创建密钥，不要用主网 API 管理页的密钥。
2. 打开 Demo 的 API 管理。当前入口是 <https://demo.binance.com/en/my/settings/api-management>。选择系统生成的密钥。
3. 打开读取和现货交易权限。不要打开提现。秘密只显示一次，放到下面的环境变量里，不要写入仓库、配置或聊天记录。
4. 在 Demo 现货钱包准备 USDT。默认探针每笔买入 15 USDT；一次完整订单场景会在卖出之后再买一笔，两笔都受 100 USDT 上限约束。余额要覆盖这两笔和手续费。
5. Demo 现货账户通常开着 BNB 手续费折扣（`discount.enabledForAccount` 为 true，折扣资产 BNB），页面上没有开关可以关掉。`demo-verify` 和 `demo-check` 在每次买单前重新读取余额：BNB 可用加冻结恰好为 0 时，按 BTC/USDT 标准费率允许买入，并在报告里写 `demo_bnb_discount_allowance: true`。不把 0.75 的 BNB 折扣算进费率。BNB 余额大于 0 时仍然拒绝。主网只要折扣开着就拒绝，这条豁免不会用到主网。买入之后如果成交的 `commissionAsset` 是 BNB，结果会标成失败。
6. 记下 Demo 账户的数字 UID，填进配置的 `account_uid`。

## 配置

在仓库目录的前台或 tmux 里执行。不要用 `nohup ... &` 启动：那样外壳不会把 SIGINT 交给进程，Ctrl-C 停不掉会话。使用一份专用状态目录，不要指向正在跑策略的 `state_dir`。

每个状态目录第一次运行时写入 `verification_nonce`。客户订单号由该值、目录内序号和操作名一起决定。新的空目录会得到新的 nonce，因此不会和上一份目录里已经发出的订单撞号。同一目录里这个 nonce 保持不变，重启后仍按原客户订单号查询，不会另发一笔。

```sh
cp config.demo.example.json demo-verify.json
```

把 `demo-verify.json` 改成自己的 UID、专用目录和不超过 100 的上限。部署把 `stop_price_percent_band` 设为 true。省略该字段时代码默认 false，会话会发送 28% 目标；Demo 已拒绝那个深度。例如：

```json
{
  "account_uid": "123456789",
  "state_dir": "~/.spotquant/binance-btcusdt-spot-demo-verify",
  "session_seconds": 300,
  "poll_seconds": 5,
  "environment": "demo",
  "capital_limit_usdt": "100",
  "stop_price_percent_band": true
}
```

```sh
export SPOTQUANT_BINANCE_DEMO_KEY='（Demo API Key）'
export SPOTQUANT_BINANCE_DEMO_SECRET='（Demo Secret）'
```

`demo-verify.json` 可以被 git 忽略；不要提交填好的 UID 以外的秘密。秘密本来就不在这个文件里。

## 命令

先做只读预览。这一步会读取账户，不会下单：

```sh
python3 -m spotquant demo-verify --config demo-verify.json
```

订单场景（市价买入、止损、撤单重挂、构造的 4% 退出、构造的均线退出、对子进程发 SIGINT）：

```sh
python3 -m spotquant demo-verify --config demo-verify.json --execute --authorize-uid 123456789
```

只跑其中一个场景时重复 `--scenario`。名称是 `market-buy`、`stop-place`、`stop-replace`、`adverse-exit`、`sma-exit`、`graceful-stop`。

故障场景：

```sh
python3 -m spotquant demo-verify --config demo-verify.json --execute --authorize-uid 123456789 --faults
```

故障名称是 `clock-skew`、`rate-limit`、`network-loss`、`kill-restart`。不带 `--faults` 时点名这些场景会被拒绝。

价格带探针。不加 `--execute` 时只读 `exchangeInfo` 和均价，列出均价下方 15%、20%、25%、28% 的止损价，不下单：

```sh
python3 -m spotquant demo-band-probe --config demo-verify.json
```

确认交易所对 `stopPrice` 的真实回答时才下单。它使用 `<state_dir>/band-probe`，不写策略检查点。账户里已有可交易的 BTC 或挂单时拒绝，不会接管。买到探针后逐个深度提交 `STOP_LOSS`，把 Binance 的 `code` 和 `msg` 原样记下来；挂上的单先撤掉，最后把探针卖回：

```sh
python3 -m spotquant demo-band-probe --config demo-verify.json --execute --authorize-uid 123456789
```

对账（只读，比较 `latest.json`、sqlite 和 Demo 的 `allOrders` / `myTrades`）：

```sh
python3 -m spotquant demo-reconcile --config demo-verify.json
```

报告默认写在状态目录的 `verification/` 下：

| 文件 | 内容 |
| --- | --- |
| `demo-verification.json` | 每个场景的机器可读结果 |
| `demo-verification.md` | 同一份结果的简表 |
| `reconcile.json` | 对账通过或失败，以及不一致列表 |
| `reconcile.md` | 对账简表 |

`--out 目录` 可以改写位置。这些文件含有账户和订单号，不要提交。

退出码：`pass` 为 0。`failed`、`unknown` 或 `blocked` 为 2。`unknown` 表示还有未确认订单，程序没有把它当成失败发送而重下。

## 每个场景看什么

### market-buy

用真实 `Binance.submit` 发送一笔约 15 USDT 的 BTCUSDT 市价买单，客户订单号在发送前写入 sqlite。回读状态应为 `FILLED`，并有 `myTrades` 成交。记录里的 `resent` 为 false。这一笔是执行探针，不是 SMA40 信号。

### stop-place

`stop_target` 仍是成交均价（若成交之后又有会话报价，则取两者较高者）再下降 28%，然后按 tick 向下取整。峰值来自这笔成交和成交后的报价，不用未完成日线的高点。`band_floor` 是按当前快照里的过滤器算出的下限。核对 `stop_price` 和交易所回读的 `exchange_stop_price`。状态应为 `NEW`。

Binance 文档把 `PERCENT_PRICE_BY_SIDE` 写成订单 `price` 的限制。Demo 探针已经看到它约束 `STOP_LOSS` 的 `stopPrice`：均价下方 15% 被接受，20%、25% 和 28% 返回 `-1013`，`msg` 为 `Filter failure: PERCENT_PRICE_BY_SIDE`。当时 BTCUSDT 的 `askMultiplierDown` 是 0.8。不要把 0.8 写进代码；每次从该交易对的 `exchangeInfo` 读取。`avgPriceMins` 为 0 时参考价是最新价，否则是交易所均价；BTCUSDT 上这是大约 5 分钟的均价。

`stop_price_percent_band` 为 false（省略该字段时的代码默认）时，`stop_price` 等于 28% 目标，且不低于这一仓已经确认的止损，`clamped` 为 false。部署把它设为 true。那时 `stop_price` 是 28% 目标、缓冲后的下限（参考价乘实时 `askMultiplierDown` 再乘 1.001，按 tick 向上取整）和已有止损三者中的较高者，只上移，`clamped` 可以是 true。靠近峰值时，0.8 乘 1.001 大约比 5 分钟均价低 20%。

止损被拒绝时，错误里有 Binance 的 `msg`。策略会话会立刻市价卖出这仓；这一笔验证探针若因此卖不掉，对应场景记 `unprotected`。退出场景只会卖掉自己刚买的 BTC。28% 这个策略参数没有改。

### stop-replace

先撤掉上一张止损，再挂一张。本地空窗 `unprotected_window_ms` 是撤单确认到新止损回读确认的时间。`exchange_unprotected_window_ms` 是新单 `time` 减去旧单 `updateTime`。报价没有抬高保护价时，程序按同一挂出价格重挂，并写上 `forced_rereplace: true` 和 `simulated_trigger: true`。报价已经更高时，新价格按同一规则上移，且不会低于正在撤的那张止损；`simulated_trigger` 为 false。`stop_price_percent_band` 为 true 且价格带下限已经更高时，同样只上移。新价格如果在确认规则下已经无法挂出，这一步在撤单之前失败，原来的止损还在。结束后应只剩一张 `NEW` 止损。

### adverse-exit

用真实的 4% 不利收盘公式构造价格，调用真实的持仓决策。决策理由应说明按 4% 卖出。然后撤掉止损，用真实市价卖出。记录里 `simulated_trigger` 为 true：价格不是当时行情走出来的。

### sma-exit

若上一笔退出后账户已空，会再买一笔探针并先挂止损，再构造「会话价在 SMA 的 0.5% 以内」的决策并真实卖出。`simulated_trigger` 为 true。决策理由应提到 0.5% 和 SMA。

### graceful-stop

子进程跑的是 `demo-check --execute`，但使用 `<state_dir>/graceful-session`，不使用已经写下验证订单的目录。验证订单还没有模型检查点时，会话每一轮都会报 `missing model checkpoint for durable state`，来不及做一次真正的观察。独立目录避免这件事。

账户里还有可交易的 BTC 或未完成订单时，这一步立刻失败，原因是新的执行状态不能接管已有持仓。它不会为了等一个不会出现的成功轮次而空转。先把验证探针卖平，再跑这一步。

父进程在账户已平时，等到子进程的 `graceful-session/latest.json` 出现一轮成功观察（`cycles >= 1`、`observation_current` 为 true、并且有 `model_bull`）再发送 SIGINT。验证目录自己的 `latest.json` 在全部场景结束后写成最终结论，包含 `graceful-stop` 和 `kill-restart`；它不会覆盖子目录里的会话报告。子进程会话时长至少 900 秒，父进程最多等约 900 秒。日线补齐可能要几分钟。成功时 `stop_reason` 为 `interrupted` 或 `requested`，`closeout_attempted` 为 true，记录里 `real_cycle` 为 true。收尾期间不再开新的买单。这个场景本身不负责把验证探针的仓位卖光；完整顺序里，卖出场景排在它前面。探针止损如果放不进去，对应场景会把自己买来的 BTC 卖回。

请在前台或 tmux 里看这一步。`nohup ... &` 不会把 SIGINT 送到进程。

`graceful-session` 里成功之后会有会话检查点。那是子目录，不挡住原验证目录里的下一轮订单场景。不要把策略的 `state_dir` 指到 `graceful-session`，也不要删除策略正在使用的目录。

### clock-skew（需要 --faults）

把签名时间戳拨快 120 秒，向 Demo 发签名 GET。应看到一次 `-1021`，客户端清掉时钟偏移并重试成功。记录里不应出现 POST。这是故意制造的时钟偏移，用来核对现有的一次 GET 重试。

### rate-limit（需要 --faults）

传输层注入一次 HTTP 429 和 `Retry-After: 1`，请求不会打到 Demo。下一笔调用应在退避结束前被拒绝，并且不发出网络请求。退避写入状态目录，等 1 秒过期后再发一笔真实的签名 GET。`simulated_transport` 为 true。

### network-loss（需要 --faults）

真实发送一笔市价买单，然后丢掉响应。程序按原客户订单号查询，`post_count` 必须是 1。查到成交后会再卖出，把 Demo 账户平回。若查询仍不能确认，场景状态是 `unknown`，不会再发第二笔买单。

若买单在发送前就被拒绝（例如手续费模式不是 BTC/USDT），`post_count` 为 0，场景状态是 `blocked`，`reason` 是交易所或适配器给出的原因，而不是「响应丢失后又重发」。

### kill-restart（需要 --faults）

先在 sqlite 写下一条没有交易所编号的未知买单（不发送），再让子进程握住状态锁，然后 `SIGKILL`。重启后再查询这条身份。通过标准是：意图列表不变、交易所挂单不变、这条记录仍是未知、错误说明里有 never resubmitted。

这条未知买单会留在库里。随后的 `demo-reconcile` 应报告 `unconfirmed_local_order` 并且不通过。这是预期结果。不要删掉 sqlite 来换一份通过的对账。

## 对账失败时

`reconcile.md` 的表列出类型、身份和说明。常见类型：

| 类型 | 含义 |
| --- | --- |
| `missing_exchange_order` | sqlite 有订单号，交易所历史里没有 |
| `unconfirmed_local_order` | 本地仍是未知或在途，交易所没有对应订单。不能把它当成未发送 |
| `untracked_exchange_order` | 窗口内的交易所订单不在 sqlite 里 |
| `stop_price` / `order_quantity` / `order_status` | 价格、数量或状态不一致 |
| `missing_exchange_fill` / `missing_local_fill` | 成交只在一侧 |
| `latest_environment` | `latest.json` 不是 demo |
| `latest_secret` | 报告里出现了凭据字段名 |

窗口从本地最早的订单或成交往前 60 秒；本地还没有记录时，看最近 24 小时。这段时间里的其他手工单也会被算作未入账。核对时使用专用 Demo 账户。

## 结果模板

把一轮真实 Demo 运行抄到下面。不要粘贴密钥。滑点一栏保持「未验证」。

```text
日期（UTC）：
Demo UID：
state_dir：
代码版本（SPOTQUANT_SOURCE_SHA 或 git rev-parse HEAD）：
capital_limit_usdt：

场景            结果    订单号 / 客户订单号    备注
market-buy
stop-place      止损价：        成交价：        峰值：
stop-replace    本地空窗 ms：    交易所空窗 ms：   forced_rereplace：
adverse-exit    simulated_trigger：true
sma-exit        simulated_trigger：true
graceful-stop   stop_reason：    closeout_attempted：    real_cycle：
clock-skew      -1021 次数：      是否重试 POST：
rate-limit      simulated_transport：true    退避期间请求数：
network-loss    post_count：     是否按原身份查回：
kill-restart    意图是否增加：    交易所挂单是否变化：

对账：通过 / 未通过
不一致条数：
报告路径：

滑点：未验证（Demo 成交和深度是模拟的）
native_execution_verified：false
主网执行：未用本轮结果声明
```

价格带探针已经记下：15% 的 `stopPrice` 被接受，20%、25% 和 28% 被 `-1013` `Filter failure: PERCENT_PRICE_BY_SIDE` 拒绝。所有者只有拿真实 Demo 密钥跑过一轮核对，才能确认这一轮的市价单、空窗毫秒数、SIGINT 收尾是否写完报告、SIGKILL 之后交易所上是否仍只有原来的订单，以及 `-1021` 是否来自 Demo 自己的时钟。注入的 429 和两笔构造价格的退出，只能说明客户端分支和卖单路径，不能说明行情触发过这些条件。
