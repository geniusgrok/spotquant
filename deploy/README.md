# 在一台 Ubuntu 上无人值守运行

目标机器是 Ubuntu 24.04，Python 3.13（可用 deadsnakes），代码在 `/opt/spotquant`，进程用户是不能登录的 `spotquant`。密钥只放在 `/etc/spotquant/*.env`，由 systemd 的 `EnvironmentFile` 注入。这台机器上若还有别的代理或服务，安装脚本不会停用或改写它们。

已发布回测的 795 次会话是历史抽样：开始时刻都在某个 UTC 整点，每次 300 秒、每 5 秒轮询一次，大多数有会话的日子只有一次，并不是要把那些历史钟点原样搬到 VPS。生产定时器在每天 UTC 00:05 跑同长度的一次会话，这时新的日线开盘已经到达。会话之间，已挂上的原生止损留在交易所。

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

然后编辑（权限保持 `root:spotquant`、`0640`）：

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
  /usr/bin/python3.13 -m spotquant notify-test --config /etc/spotquant/demo.json
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
3. 资金上限、UID 和环境必须是实盘自己的。`stop_price_percent_band` 只有在 Demo 探针确认之后才设为 true。
4. 创建实盘开关文件。没有这个文件，实盘单元即使被启动也会被 systemd 跳过：

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

在状态目录放下 `HALT` 文件后，定时会话仍会跑，用来维护止损，但不再新买。回撤停买由配置 `max_drawdown_halt_pct` 控制（相对已记录峰值，例如 `"0.25"`）。不写这项就没有回撤停买。

取消本状态目录自己的挂单，并只卖出账本里记下的仓位：

```sh
sudo -u spotquant env $(grep -v '^#' /etc/spotquant/live.env | xargs) \
  /usr/bin/python3.13 -m spotquant kill-switch \
  --config /etc/spotquant/live.json --authorize-uid 你的UID --confirm
```

没有 `--confirm` 不会发单。账本之外的 BTC 不会被卖掉。

数据库备份在每天 UTC 00:20，用 SQLite 在线备份，保留最近 14 份，目录是 `/var/backups/spotquant/demo` 或 `live`。

告警主题以 `[spotquant][告警]` 开头，每日心跳以 `[spotquant][心跳]` 开头。同一类告警 6 小时内不重复发送。心跳每 UTC 日一封。每小时检查一次心跳；超过 26 小时没有心跳会告警。

会发告警的情况：状态未知、人工接管、保护失败、报告写失败、退出码非零、止损无法挂出、回撤或 HALT 停买、错过会话。

## 上线检查

- `python3.13 -m unittest discover -s tests` 在部署用的代码上通过。
- `notify-test` 能收到信，主题前缀正确。
- Demo 定时器已 enable，实盘定时器未 enable，`LIVE_ENABLED` 不存在。
- Demo 和实盘的 `state_dir` 不同，env 文件不是世界可读。
- 手动放一个 `HALT`，下一次会话日志里出现停买，且没有新的买单。
- 看一次 `journalctl`，确认会话在 UTC 00:05 附近结束，而不是一直挂着。
- 备份目录里出现 `intents-*.sqlite`。
