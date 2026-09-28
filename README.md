# Spotquant

本公开仓库仅用于托管 geniusgrok 的个人量化研究代码和实验记录，项目仅供仓库所有者本人使用。公开可见不代表开放授权、邀请贡献或实盘交易。未经权利人事先书面许可，不得擅自使用、复制、抄袭、修改、改编、传播其原创代码与研究内容，也不得冒名署名。详见 [LICENSE](LICENSE)。

**不得将本仓库作为实盘交易工具使用。** 策略、回测和收益数字仅为研究记录，不是投资建议、收益保证或可执行性证明。历史模拟不能证明真实撮合、余额、网络故障和保护单失效等风险已得到控制；任何擅自使用所产生的行为与损失由使用者自行承担。

研究对象是个人使用的 Binance **BTCUSDT 现货**。只做多或持有 USDT，不借币、不做空、不开合约、不用杠杆。手动启动一个有限会话，会话内持续读取真实行情与账户状态、重复判断，超时或 Ctrl-C 后退出；没有后台守护进程。

**当前命令行禁止真实交易写入。** `run --execute` 在读取配置、凭据和网络之前拒绝。研究账本的经济资格是 `MET`，原生交易资格仍是 `NOT_QUALIFIED`。

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

- 默认模型看多的条件是日线收盘价高于 SMA(40)。入场还要连续两根这样的收盘、上一次退出之后先出现过一根不看多的收盘，并且收盘不低于 252 日最高收盘的一半。收盘高于均线 60% 时，下一根开盘卖出。8% 下跌后的 6% 反转，并且收盘仍比 400 日最高收盘低至少一半时，可以在下一根开盘买入；这笔持仓只受 28% 止损约束，直到收盘回到均线之上、并且距离该 400 日高点不超过 20%。其余持仓如果收盘比成交价低 4% 或更多，下一根开盘卖出。保护是运行高点下方 28% 的 `STOP_LOSS` 价格，随日高上移后改价。28% 宽于现货 `trailingDelta` 的 2000 bips 上限。没有空头入场。
- 预览买单是市价、按报价资产数量；预览保护是带 `stopPrice` 的 `STOP_LOSS`。预览不是委托，也不会被写成订单意图。
- 冷启动只重建模型，不把重启前已经成立的多头当成新信号。

## 经济测量

```sh
python -m unittest discover -s tests -v
python -m research.rebuild P3
```

测量窗口、成本、人民币估值和选参记录见 `research/PROTOCOL.md`。官方数字在 `evidence/rebuild-20260928/`。目标是成本后年化不低于 100%、连续最大回撤不超过 30%。默认试验 P3 两项都达到，经济资格为 `MET`。原生资格仍是 `NOT_QUALIFIED`，命令行不会因此放开下单。

默认试验在 P2 的 SMA(40)、连续两根确认、新鲜穿越、252 日崩盘过滤和 28% 止损之上，增加 60% 偏离退出、400 日深度上的崩盘反转，以及非反转持仓收盘低于成交价 4% 时的次日开盘退出。参数在整个窗口上选定，没有样本外结果。4% 这一档与 3.8% 是同一本账；3.5% 到 3.7% 的期末更高，但紧挨着会卖掉 2021 年 1 月那笔盈利交易的 3.4% 门槛，因此没有升成默认。

| 场景 | 期末人民币 | 成本后 CAGR | 连续 MDD |
|---|---|---|---|
| 基准 `python -m research.rebuild P3` | 1,113,885 | 101.67% | 29.15% |
| 手续费 +50% | 1,072,838 | 100.55% | 29.22% |
| 出场滑点与止损滑点 ×2 | 1,093,453 | 101.12% | 29.19% |
| 随机跳过 20% 日开盘 | 1,080,706 | 100.77% | 30.29% |
| 21 天空窗 | 1,113,885 | 101.67% | 29.15% |

随机跳过的回撤是 30.29%，这一档压力没有同时过线。21 日空窗的种子区间里没有武装好的开盘，结果与基准相同。上一版默认 P2 是期末 432,659 元、年化 75.19%、回撤 33.10%。P1（SMA 40、20% 追踪）是期末 216,652 元、年化 58.06%、回撤 52.77%。窗口结束时仍按最后收盘价持有多头。详见 [经济重建结果](evidence/rebuild-20260928/RESULT.md)。

行情 zip 不入库。日线放在测量器 `--market` 指向的目录（默认 `/tmp/spotquant-market/klines`）。DEXCHUS 原件在 `evidence/rebuild-20260928/inputs/`，加载时核对 SHA-256。

当前恢复入口：`AGENTS.md`、`PROJECT_STATE.md`、`HANDOFF_PROMPT.md`。
