# 在一台 Ubuntu 上无人值守运行

目标机器是 Ubuntu 24.04，Python 3.13（可用 deadsnakes），代码在 `/opt/spotquant`，进程用户是不能登录的 `spotquant`。密钥只放在 `/etc/spotquant/*.env`，由 systemd 的 `EnvironmentFile` 注入。这台机器上若还有别的代理或服务，安装脚本不会停用或改写它们。

已发布回测的 795 次会话是历史抽样：开始时刻都在某个 UTC 整点，每次 300 秒、每 5 秒轮询一次，大多数有会话的日子只有一次，并不是要把那些历史钟点原样搬到 VPS。生产定时器在每天 UTC 00:45（北京时间 08:45）跑同长度的一次会话，这时新的日线开盘已经到达。Demo 和实盘共用这一时刻。会话之间，已挂上的原生止损留在交易所。

## 安装

在检出的仓库里，用 root 执行。脚本可以重复运行。已有的配置和 env 不会被覆盖。

```sh
sudo sh deploy/install.sh
```

同时启用 Demo 的每日会话、心跳检查和备份：

```sh
sudo sh deploy/install.sh --enable-demo
```

脚本不会启用实盘。

安装前需要已有 `/usr/bin/python3.13`。脚本发现没有就退出，不会自行安装软件包。

示例 JSON 把 `stop_price_percent_band` 写成 true，这是部署选择：挂出价取 28% 目标、缓冲后的价格带下限和已有止损中的较高者，只上移。程序在省略该字段时仍默认 false，那样会发送 28% 目标。然后编辑（权限保持 `root:spotquant`、`0640`）：

| 文件 | 作用 |
| --- | --- |
| `/etc/spotquant/demo.json` | Demo 的 UID、状态目录、300 秒会话、资金上限 |
| `/etc/spotquant/demo.env` | Demo API 密钥 |
| `/etc/spotquant/live.json` | 实盘配置，状态目录必须和 Demo 分开 |
| `/etc/spotquant/live.env` | 实盘 API 密钥 |
| `/etc/spotquant/notify.env` | 邮件。变量名固定，安装时若文件已存在则不覆盖 |

邮件只认这六个名字：`SPOTQUANT_SMTP_HOST`、`SPOTQUANT_SMTP_PORT`、`SPOTQUANT_SMTP_USER`、`SPOTQUANT_SMTP_PASSWORD`、`SPOTQUANT_SMTP_FROM`、`SPOTQUANT_SMTP_TO`。当前机器是 `smtp.qq.com` 和端口 `465`（隐式 TLS，`SMTP_SSL`）。端口写成 `587` 时改用 STARTTLS。不要用 25。`SPOTQUANT_SMTP_TO` 用逗号分隔多个收件人。`SPOTQUANT_WEBHOOK_URL` 可留空，只作为邮件之外的可选通道。会话、心跳检查和失败告警的单元都会再加载这份 `notify.env`。

发一封测试信：

```sh
sudo systemctl start spotquant-failed@demo.service
```

更直接：

```sh
sudo -u spotquant env $(grep -v '^#' /etc/spotquant/notify.env | xargs) \
  /usr/local/bin/sq notify-test --config /etc/spotquant/demo.json
```

主题应是 `[spotquant][心跳] 通知测试`。

## 启用和停用

Demo：

```sh
sudo systemctl enable --now spotquant-session@demo.timer \
  spotquant-watch@demo.timer spotquant-backup@demo.timer
sudo systemctl stop spotquant-session@demo.timer \
  spotquant-watch@demo.timer spotquant-backup@demo.timer
sudo systemctl disable spotquant-session@demo.timer \
  spotquant-watch@demo.timer spotquant-backup@demo.timer
```

看日志：

```sh
sudo journalctl -u spotquant-session@demo.service -n 100
```

两次会话叠在一起时，`flock` 和状态目录里的 `execution.lock` 会让后一次直接跳过，不重复下单。

## 从 Demo 换到实盘

1. 停用并 disable 上面三个 Demo 定时器。确认没有 `spotquant-session@demo.service` 还在跑。
2. 把 `/etc/spotquant/live.json` 和 `live.env` 填好。`state_dir` 用 `/var/lib/spotquant/live`，不要和 Demo 共用。
3. 资金上限、UID 和环境必须是实盘自己的。部署把 `stop_price_percent_band` 设为 true。Demo 已确认 `STOP_LOSS` 的 `stopPrice` 受 `PERCENT_PRICE_BY_SIDE` 约束：均价下方 15% 接受，20%、25%、28% 返回 `-1013` `Filter failure: PERCENT_PRICE_BY_SIDE`。当时 BTCUSDT 的 `askMultiplierDown` 是 0.8，代码按交易对读取，不写死。开关为 true 时，挂出价是 28% 目标、缓冲下限和已有止损中的较高者，只上移；靠近峰值时大约比 5 分钟均价低 20%。代码默认仍是 false，删掉这个字段会改回发送 28% 目标。`capital_limit_usdt` 只限制新买的名义，不是亏损上限；相对峰值停止新买的是 `max_drawdown_halt_pct`。
4. 创建实盘开关文件。没有这个文件，实盘的会话、心跳检查和备份即使被启动也会被 systemd 跳过：

```sh
sudo touch /etc/spotquant/LIVE_ENABLED
```

5. 明确启用实盘定时器。安装脚本不会做这一步：

```sh
sudo systemctl enable --now spotquant-session@live.timer \
  spotquant-watch@live.timer spotquant-backup@live.timer
```

要停实盘：disable 上述定时器，并删除 `/etc/spotquant/LIVE_ENABLED`。

## 停买、急停和备份

在状态目录放下 `HALT` 文件后，定时会话仍会跑，用来维护止损，但不再新买。`kill-switch` 总会先写这个文件（kill-switch always writes HALT first），先于查询、快照、撤单和卖出。恢复新买只做一件事：删除该状态目录里的 `HALT`。删除之后下一次买入仍要新的穿越。回撤停买由配置 `max_drawdown_halt_pct` 控制（相对已记录峰值，例如 `"0.25"`）。不写这项就没有回撤停买。

取消本状态目录自己的挂单，并只卖出账本里记下的仓位：

```sh
sudo -u spotquant env $(grep -h -v '^#' /etc/spotquant/live.env /etc/spotquant/notify.env | xargs) \
  /usr/local/bin/sq kill-switch \
  --config /etc/spotquant/live.json --authorize-uid 你的UID --confirm
```

没有 `--confirm` 不创建状态文件、不写 `HALT`、不发通知、不发单。账本之外的 BTC 不会被卖掉。UID 和状态锁通过且带 `--confirm` 之后，程序第一步就把 `state_dir/HALT` 原子写好（kill-switch always writes HALT first），先于退避恢复、查询、快照、撤单和卖出。`HALT` 内容以 `kill` 开头。写下之后、任何查询之前，同一笔本地提交把当前账面标成必须新穿越，包括已经武装的提前入场。之后快照、退避、查询或校验失败，这条限制仍在；数据库提交若没完成，下一次会话在文件还在时补上。手工放的空文件只停买。第一次查询失败，或者仓位已经只剩不可卖尘埃、甚至已经是空仓，也同样写入。写 `HALT` 失败则中止、不发单，并告警。急停订单的时钟可以是成交时刻。普通会话用本账户的订单身份接管这些未决单，不另发新号。通知失败不改已经完成的成交，也不再发一轮交易失败告警。卖单走原来的退出生命周期：意图、客户订单号和撤单号在发送前写入状态。报告里的 `sold` 只统计这一次退出里已经回读确认的市价成交，同一次退出重启后仍能对上，不含更早的周期，也不把尚未回读的回执当成成交。后来又完成过另一仓再回到空仓时，不再把更早那次退出算进这次 `sold`。撤单期间如果止损先成交，退出数量按归账后的剩余再算。已知数量规则不合格时不先撤掉还有效的止损。十二次动作后仍有可卖仓位才告警并要求人工接管；第十二次已经卖成尘埃则算完成。没有另外的墙钟时限。`HALT` 只停止新买，退出和止损照常。恢复交易：确认仓位和止损之后，删除该状态目录里的 `HALT`。删除之后，当前账面不能沿用这次 touch 再入，包括已经入账的那一次；下一次买入要新的穿越。更早卖单上的 rearm 不改。空仓确认不会把上一笔退出身份套到后来的新仓，也不会在那一仓已经平掉之后继续沿用它。成功退出只告警停买，不把退出码写成非零。手动命令用 `grep -h`，避免把文件名拼进变量名。限频退避在写入 `HALT` 之后、第一次快照之前接上；快照失败、写 `HALT` 失败或退避记录损坏也会发告警。

数据库备份在每天 UTC 01:05，排在 00:45 的会话结束之后，用 SQLite 在线备份，保留最近 14 份，目录是 `/var/backups/spotquant/demo` 或 `live`。

告警主题以 `[spotquant][告警]` 开头，每日心跳以 `[spotquant][心跳]` 开头。同一类告警 6 小时内不重复发送。心跳每 UTC 日一封，由 00:45 的会话写出。每小时检查一次；心跳超过 26 小时算错过会话，因此错过当天 00:45 之后，大约从次日 UTC 02:45 起的整点检查会告警。

会发告警的情况：状态未知、人工接管、保护失败、报告写失败、退出码非零、止损无法挂出、回撤或 HALT 停买、错过会话。

手动命令和定时器都走 `/usr/local/bin/sq`。它把 `SPOTQUANT_SOURCE_SHA` 设成 `/opt/spotquant` 里 `git rev-parse HEAD` 的结果；那个目录不是 git 检出时，改用安装时写下的 `/etc/spotquant/source_sha`。因此 systemd 跑出来的 `runtime_identity.source_sha` 不是空的。每次安装都会按当前检出重写这份 SHA。

单元里的 `UMask=0077` 让新写出的 `latest.json`、sqlite、心跳和备份是 `0600`。状态目录本身仍是 `0750`。已经写成 `0644` 的文件不会被这次设置改掉，需要的话在状态目录里把它们改成 `0600`。

## 其他资产

`snapshot` 是只读导出。账户里除 BTC 和 USDT 以外，只要还有余额，导出就失败，原因是 `other assets prevent a complete BTC/USDT account export`。Demo 里的 5000 USDC 会触发这句话。这不是 `run --execute` 的路径。

`run --execute` 和定时器里的 `ops-run` 不看 `other_assets`。账户里有 USDC 或其他非 BTC/USDT 余额时，会话不会因此拒绝买入。买单仍取决于 USDT 可用余额、资金上限，以及手续费模式。实盘上只要 BNB 抵扣对账户和 BTCUSDT 都开着，新买单就会被拒绝，原因是 `buy fee mode is not confirmed as BTC/USDT`；BNB 余额是不是 0 都不改变这一点。Demo 上只有 BNB 余额大于 0 时才走这条拒绝，BNB 为 0 时仍按 BTC/USDT 手续费买入。USDC 不参与手续费模式判断。权益和 `max_drawdown_halt_pct` 只按 USDT 现金加 BTC 市值计算，不把 USDC 算进去。其他币不会记成策略持仓，`kill-switch` 也不会卖掉它们。

实盘请用一个只放 BTC 和 USDT 的子账户。这样 `snapshot` 能导出，回撤停买对着的是整户权益，别的币也不会和策略账本混在一起。

## 上线检查

- `python3.13 -m unittest discover -s tests` 在部署用的代码上通过。
- `notify-test` 能收到信，主题前缀正确。
- Demo 定时器已 enable，实盘定时器未 enable，`LIVE_ENABLED` 不存在。
- Demo 和实盘的 `state_dir` 不同，env 文件不是世界可读。
- 手动放一个 `HALT`，下一次会话日志里出现停买，且没有新的买单。
- 看一次 `journalctl`，确认会话在 UTC 00:45（北京时间 08:45）附近结束，而不是一直挂着。
- 备份目录里出现 `intents-*.sqlite`。
