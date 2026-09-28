# Spotquant

本公开仓库仅用于托管 geniusgrok 的个人量化研究代码和实验记录，项目仅供仓库所有者本人使用。公开可见不代表开放授权、邀请贡献或实盘交易。未经权利人事先书面许可，不得擅自使用、复制、抄袭、修改、改编、传播其原创代码与研究内容，也不得冒名署名。详见 [LICENSE](LICENSE)。

**不得将本仓库作为实盘交易工具使用。** 策略、回测和收益数字仅为研究记录，不是投资建议、收益保证或可执行性证明。历史模拟不能证明真实撮合、余额、网络故障和保护单失效等风险已得到控制；任何擅自使用所产生的行为与损失由使用者自行承担。

研究对象是个人使用的 Binance **BTCUSDT 现货**。只做多或持有 USDT，不借币、不做空、不开合约、不用杠杆。手动启动一个有限会话，会话内持续读取真实行情与账户状态、重复判断，超时或 Ctrl-C 后退出；没有后台守护进程。

**当前命令行禁止真实交易写入。** `run --execute` 在读取配置、凭据和网络之前拒绝。经济目标尚未同时达到，资格为 `NOT_QUALIFIED`。

## 所有者本地研究

Python 3.13；项目运行仅使用标准库。

```sh
git clone https://github.com/geniusgrok/spotquant.git
cd spotquant
cp config.example.json config.json
python -m spotquant status --config config.json
python -m spotquant run --config config.json
```

仅供仓库所有者在授权账户上进行本地只读研究。在配置中填写所有者的 Binance `account_uid` 和固定 `state_dir`。实盘凭据只从 `SPOTQUANT_BINANCE_KEY`、`SPOTQUANT_BINANCE_SECRET` 环境变量读取，Demo 凭据只从 `SPOTQUANT_BINANCE_DEMO_KEY`、`SPOTQUANT_BINANCE_DEMO_SECRET` 读取，都不写入配置或仓库。`status` 单次观察；`run` 默认观察 300 秒，每 5 秒重新核对。配置项：

| 配置 | 含义 |
|---|---|
| `account_uid` | 必须与交易所核验的账户一致 |
| `state_dir` | 此账户持续使用的恢复目录 |
| `session_seconds` | 本次运行 1～86400 秒，默认 300 |
| `poll_seconds` | 轮询 1～60 秒，默认 5，不得超过会话时长 |
| `environment` | `live`（默认，`api.binance.com`）或 `demo`（`demo-api.binance.com`）。状态范围是 `binance:BTCUSDT:spot:{live\|demo}:{uid}` |
| `capital_limit_usdt` | 可选，十进制字符串。预览买单只用 `min(可用 USDT, 上限)`。它限制预览规模，不是亏损上限 |

Demo 与实盘共用同一只读适配器。`run --execute` 在两种环境下都同样被阻止。

首次运行从固定起点重建已完成的 UTC 日线，并记下检查点；这一轮不把已经持续的多头状态当成新的买入。之后每根新的已完成日线才会允许预览入场。账户上出现没有本系统成交记录的 BTC（名义达到 5 USDT）时，本轮为未知，不增加风险。同一账户只允许一台机器运行，本地锁覆盖整个会话。

`run --execute` 在访问凭据和网络之前拒绝。不要删除这个阻止来试单。

## 已实现的研究路径

`CLI → session → 只读 Binance 现货 → 预览 → latest.json`。

- 默认模型是日线收盘价高于 SMA(40) 则看多，保护为交易所可挂的 **20% trailingDelta**（2000 bips，现货上限）。没有空头入场。
- 预览买单是市价、按报价资产数量；预览保护是 `STOP_LOSS` + `trailingDelta=2000`。预览不是委托，也不会被写成订单意图。
- 冷启动只重建模型，不把重启前已经成立的多头当成新信号。

## 经济测量

```sh
python -m unittest discover -s tests -v
python -m research.rebuild --grid
python -m research.rebuild P1
```

测量窗口、成本、人民币估值和登记过的参数网格见 `research/PROTOCOL.md`。官方数字在 `evidence/rebuild-20260928/`。目标是成本后年化不低于 100%、连续最大回撤不超过 30%。登记网格里这两项没有同时出现，经济资格保持 `NOT_MET`。

默认试验是 SMA(40) 与 20% 追踪（现货 `trailingDelta` 能挂出的上限内，期末人民币最高的一组）：

| 场景 | 期末人民币 | 成本后 CAGR | 连续 MDD |
|---|---|---|---|
| 基准 `python -m research.rebuild P1` | 216,652 | 58.06% | 52.77% |
| 手续费 +50% | 200,075 | 56.19% | 53.43% |
| 出场滑点与止损滑点 ×2 | 207,733 | 57.07% | 53.12% |
| 随机跳过 20% 日开盘 | 220,498 | 58.47% | 54.00% |
| 21 天空窗 | 216,652 | 58.06% | 52.77% |

网格里期末最高的是 SMA(40) / 30%：241,584 元，年化 60.64%，回撤 49.00%。30% 不能作为现货追踪委托挂出，而且同样没有达到目标。回撤不超过 30% 的登记行数是 0。参数在整个窗口上选定，没有样本外结果。详见 [经济重建结果](evidence/rebuild-20260928/RESULT.md)。

行情 zip 不入库。日线放在测量器 `--market` 指向的目录（默认 `/tmp/spotquant-market/klines`）。DEXCHUS 原件在 `evidence/rebuild-20260928/inputs/`，加载时核对 SHA-256。

当前恢复入口：`AGENTS.md`、`PROJECT_STATE.md`、`HANDOFF_PROMPT.md`。
