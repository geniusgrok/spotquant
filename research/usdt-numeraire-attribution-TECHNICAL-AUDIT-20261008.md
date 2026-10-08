# BTCUSDT 计价单位归因：技术复核（2026-10-08）

本补记核验既有[事前规格](usdt-numeraire-attribution-spec-20261006.json)、[只读复现脚本](usdt_numeraire_attribution.py)与[原结果](usdt-numeraire-attribution-RESULT.md)，不改冻结条件。提交先后为规格 `a36f05488c5237e2ef0730cfcd906fbc473a0873`（2026-10-06 16:28:25 UTC）、脚本 `25c4dae4b6b1c90a35a9e0fabc9c0e4a15d6007a`（16:31:46 UTC）、原结果 `94bd7b80e4347aebfa10601618a62788799c5351`（16:33:27 UTC）。所复核投影绑定旧生产源码 `74bd6e035e36531c517029077c1a9e2e5ec44516`，并非当前 main 的新前瞻结果。

原输入只读复用：`evidence/btc-search-next-20261005/search-artifacts.zip`（1,701,694 字节，SHA256 `c9f9b30ab5f5a551a9a9bf5e9bd99ced3dc934b50d116eee1d9f5d59ff941cc3`），以及 `evidence/btc-flow-risk-20261004/spot-baseline-projection.json.gz`（SHA256 `ef32a84a7e89cfd7ea3d76a8c6fa56a1da95546500269a8a0b688047f94a2118`）。从 ZIP 的 `daily-composition.json` 取 Binance BTCUSDT 完整 UTC 日收盘，从 `history/daily.json` 取 Coinbase USDT-USD 同日收盘；`USDT-USD` 是每 USDT 的 USD 数量，故 `BTCUSDT × USDT-USD = USD/BTC`。不倒数、不用未认证成交量，也不拿新 BTC 价配旧汇率。Coinbase 的 `time` 是桶起点，`close` 是桶内末笔；[官方定义](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)提示历史桶可能不完整。该档案的 1,723 个日桶无日期缺口，但不能证明桶内所有成交或两场所末笔同时发生。

原账按 `signal_ms` 日桶起点去重，2022-01-01 起共有 40 个原 BUY 决策组。完整单位比较需从 2022-01-01 开始连续 400 个转换后收盘，才同时覆盖 SMA30/40/50、252 收盘高点与 400 日崩跌修复分支；2022-02-09、2022-03-02、2022-03-17、2023-01-12 四组预热不足，保持缺失，没有用旧 BTCUSDT 补 USD 序列。余下 36 组第一笔为 2023-02-19，最后一笔为 2026-08-10。原 BUY 都不属于修复分支，但复核仍计算该分支价格门。

先逐一验证最早三个完整预热组：原 `trigger_close`、三个袖套的 `bullish_votes`、实际批准袖套的原价格门与原账一致；换算价格门均保持通过。

| 信号日桶起点 UTC | 原决策 UTC | 原批准 SMA 袖套 | BTCUSDT 收盘 | 同日 USDT-USD | 换算 USD/BTC |
| --- | --- | --- | ---: | ---: | ---: |
| 2023-02-19 00:00 | 2023-02-20 03:00:02.400 | 30 | 24,271.76 | 1.00028 | 24,278.5560928 |
| 2023-02-27 00:00 | 2023-02-28 09:00:01.400 | 30 | 23,492.09 | 1.00005 | 23,493.2646045 |
| 2023-03-15 00:00 | 2023-03-16 08:00:02 | 30、40、50 | 24,285.66 | 1.00404 | 24,383.7740664 |

日桶键不是收盘可得时间。冻结规格只在原决策晚于桶起点加一天再加 60 秒时使用该日收盘；36 组均通过这一**模拟**延迟。但 `history/receipts.json` 中 Coinbase 历史响应实际在 **2026-10-05 01:17:14–17 UTC** 才被收到，无法证明旧决策当时已取得同值或同一历史版本。此筛选只作条件开发归因，严格历史当时可得性未认证。

36 组中只有 2024-02-06 的一个已批准 30 日袖套出现价格门分歧：在此前一日，原 BTCUSDT 收盘 42,708.70 略高于 SMA30 42,697.6843333；使用当日 USDT-USD 0.99873 后，USD/BTC 收盘 42,654.459951 略低于 USD SMA30 42,682.4266346。2024-02-06 当日两种计价均高于各自 SMA30，分歧仅是两日确认。其余已批准价格门布尔判定未变。预定重大性先要求至少两组且跨两年，本次一组、一年，故该关已失败，无需构造账户变体或按结果改阈值。单独的公开汇率敏感性：样本中 USDT-USD 离 1 最大 0.782%（2023-03-12）；末日 2026-09-19 汇率为 0.99965。汇率变动本身不是账户收益。

原始价格条件只复核已有批准决策；实际持仓归属、fresh-cross、资金与 SELL 优先未重演，不能推出新 BUY。结论 `CLOSE_NO_MATERIAL_DIVERGENCE`：这条计价归因改造关闭，不入 main；总体 100% 年化 / 30% 回撤目标不因本次诊断而达成。后续独立信息验证沿用[跨场所前瞻观察协议](cross-venue-forward-protocol-20261006.md)，不重跑已失败的日线溢价符号路线。

在归档分支根目录的最小复现（仅读旧文件、不下载市场、不触账户）：

```sh
python research/usdt_numeraire_attribution.py \
  --zip evidence/btc-search-next-20261005/search-artifacts.zip \
  --projection evidence/btc-flow-risk-20261004/spot-baseline-projection.json.gz \
  --out /tmp/spot-usdt-numeraire-result.json
```

本次局部输出 SHA256 `225cb5d55711a98c923ababec74b563b1f40759d725a77c4eebb4c16d0ab3ce1`；未做账户回放、全量软件检查或运行代码修改。
