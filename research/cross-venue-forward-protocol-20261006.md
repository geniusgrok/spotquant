# Coinbase/Kraken 跨场方向流：前瞻观察冻结单（2026-10-06）

范围：Spotquant BTCUSDT 现货的研究观察。只复用 archive/pre-slim-20261006 已有的 research/information_capture.py、information_next.py、tradeoff_routes.py 和原始公开收据；订单、账户、凭据、常驻任务均为零。本文写入时，下一次指定观察尚未发生。旧窗口只作开发种子，不计前瞻结果或十个独立成熟窗口。

## 原件核对与旧窗口地位

- 原件：evidence/btc-lifecycle-20261005/research-artifacts.zip 内 task/new-complete-window-capture-recovery/{registered-plan.json,bound-window-plan.json,coinbase-connection-receipt.json,coinbase-messages.ndjson,kraken-connection-receipt.json,kraken-messages.ndjson,result.json,ingest-alias-recovery-result.json}。原始 result.json 为 BLOCK_CAPTURE；同一原始字节的 level2_batch 实际确认名 level2_50 经限定导入修复后才为 CLOSED_RESEARCH_WINDOW。修复结果 SHA256 为 e76b717fce1644d77d2fddef6495336069e4a15c7c99d42a7de6b7b3475cc719；两条原始流 SHA256 分别为 e64245f8ff146e008faed1c1faac028b1ef020e48ad7de2a44158da0075f8cb5 和 0b1ef346f487a2ecd16c660bc51f5a149c3d9a5e168728378be54c7bd288481a。
- 计划注册 2026-10-05 09:16:00.549918863Z（北京时间 17:16:00.549918863）；固定共同事件窗 [09:16:10.549918863, 09:16:30.549918863)Z（北京时间 [17:16:10.549918863,17:16:30.549918863)）；决策 09:16:40.549918863Z（北京时间 17:16:40.549918863）。两连接各一次、无重连，Coinbase/Kraken TLS 与 101 成立。Coinbase 首/末窗内交易 ID 1102504927/1102505249，事件 09:16:10.858491/09:16:30.125289Z，接收 09:16:10.898133943/09:16:30.166733887Z，共 323 条；Kraken 首/末 ID 110223405/110223465，事件 09:16:11.507744/09:16:29.665524Z，接收 09:16:11.591206981/09:16:29.750527568Z，共 61 条。收到决策前的 USDT/USD 簿更新事件 09:16:40.498189Z、接收 09:16:40.536314034Z，bid/ask 0.99967/0.99969。记录真实事件与接收时钟，不以区块或事后聚合时间代替。
- 窗口的 Coinbase/Kraken 买卖失衡分别为 -0.80796112245/-0.88892868235。+0.10/-0.10 双场同向门槛和正需求新增 25% 上限、负需求新风险因子 0.5 已存在于 2026-10-05 06:35:39Z 提交的 tradeoff_routes.py；这是事前通用方向规则，旧窗口负号是收据到来后才测得，没有事前发布该窗口的实测符号预测。
- 原 registered-plan.json 事前锁定 40 秒、10 秒预热、20 秒共同窗口、10 秒尾段、两公开 WS、交易 ID/事件与接收时钟和报价语义源 SHA；未锁定七天价格标签。lifecycle-roadmap.json 说“下一步预注册独立日期、非重叠结果、价格/单场/半预算对照”，对应提交 2026-10-05 09:31:54Z，晚于本次窗口。incremental_information.py 的七天标签检查及单场变量对应提交 14:54:04Z，也晚于窗口；它仍不指定此 WS 窗口的端点成交价、可接收时间、报价换算或端点缺失选择。因此旧窗口的七天起止、入/出价格、结果缺失、对照计算与完整非重叠选窗规则没有作为本窗口的事前联合协议冻结。旧决策机械加七天为 2026-10-12 09:16:40.549918863Z（北京时间 17:16:40.549918863），仅用于下一次错开时间；不是旧窗口的前瞻成熟标签，也不在 10 个中计数。lifecycle-roadmap 的 2026-10-12 14:29:48.514 北京时间是另一期权路线，不能挪作本路线时点。

## 下一次固定待办及不可变取样

下一次手动启动不得早于 2026-10-12 09:20:00Z / 北京时间 17:20:00（目标启动）。若恰在目标纳秒注册，事件窗为 [09:20:10,09:20:30)Z / [17:20:10,17:20:30)，决策为 09:20:40Z / 17:20:40，七天端点为 2026-10-19 09:20:40Z / 北京时间 17:20:40。实际注册时钟只能由新 registered-plan.json 的 registered_ns 得知：T=decision_ns=registered_ns+40,000,000,000；真正端点 H=T+604,800,000,000,000 ns。启动迟到则所有时点同步顺延，绝不把目标钟点伪装成实际接收时钟。下一新观察的决策须不早于前一前瞻观察的 H；按时间登记每个尝试，不能看符号或结果挑窗口。失败尝试原样保留、不移窗补采；当日重叠窗口不计独立样本。

在该时点从归档分支根目录运行以下现有脚本一次。两份公开语义原件只从已有 zip 解出到临时目录，不再远程下载；脚本会自行校验其固定 SHA256（Coinbase 52929f0f85be7632c34f978bd60a153f5dc6b33aab3d41713a9db95565ffd43f；Kraken a7fa3c8796b7a3abb5b27cd8cfd0915a69d0d51e13cd916e3b72917557152ada）。

~~~sh
semantics_dir="$(mktemp -d /tmp/spotquant-cross-semantics.XXXXXX)"
unzip -p evidence/btc-search-next-20261005/search-artifacts.zip stream/coinbase-ws-channels.raw > "$semantics_dir/coinbase-ws-channels.raw"
unzip -p evidence/btc-search-next-20261005/search-artifacts.zip stream/kraken-ws-v2-trade-redirect-target.raw > "$semantics_dir/kraken-ws-v2-trade-redirect-target.raw"
python -m research.information_capture \
  --semantics-root "$semantics_dir" \
  --out /tmp/spotquant-cross-forward-01 \
  --purpose "Spotquant future independent Coinbase/Kraken 7-day observation 01; preregistered 2026-10-06; no retries or trading"
~~~

输出目录必须事先不存在；机器钟须为同步 UTC，开跑前确认两公共连接允许且无凭据。脚本只用 Coinbase matches BTC-USD、heartbeat BTC-USD、level2_batch USDT-USD 及 Kraken trade BTC/USD，限制 40 秒、两次公开连接、每流 4 MiB、每消息 256 KiB；不连 Binance。仅 result.json 为 CLOSED_RESEARCH_WINDOW、且原始收据与 SHA 可核验时，才可记一条来源合格特征。需要保留 registered-plan、原始两流及收据、bound-window-plan、result、所用脚本 SHA、双场逐笔 ID/方向/数量/价格/event_ns/receipt_ns、心跳封边、USDT/USD 簿 event_ns/receipt_ns/bid/ask。缺少 ID、封边、事件或接收钟、两场共同完整窗、决策前可得的 1 秒内新鲜转换簿即为缺失；不补值、不当 NO_EVENT 或利润。

## 只对未来观察生效的七天结果规则

事前方向规则保留原 ±0.10，不重调：双场均 >=+0.10 记正需求并仅研究正向七天 BTC 价格；双场均 <=-0.10 记负需求并仅研究下行尾部及新风险因子 0.5；其他为 NO_EVENT。每个合格窗口按决策 T 立即登记符号与完整 SHA，不等结果后改符号。由同一次 40 秒 Coinbase BTC-USD 原始 matches 选入价：事件和接收均 <=T、T-事件 <=1 秒的最后一笔，若同事件时刻则选较晚接收/交易 ID；禁止 last_match 初始快照代替实际 match。该笔价格为 P0_USD。入端 USDT/USD 取 T 前因果可得、事件与接收有效且报价事件距 T <=1 秒的最新完整簿，记 bid0。

七天终点 H=T+7*86400 秒，退出价仅取随后一次有限、同配对双源 40 秒采集里 Coinbase 第一笔 event_ns >=H 且 <=H+1 秒、receipt_ns <=H+1 秒、event_ns<=receipt_ns 的真实 match，记 P1_USD；同笔同时满足 ID 完整性。退出端取该成交接收前实际收到的最新完整 USDT/USD 簿，event_ns<=receipt_ns<=该成交接收，簿事件距成交接收 <=1 秒，记 ask1。该次采集可手动目标启动于 H-20 秒（留 +/-5 秒手动容差，实际以收据为准），于是 H 落在其 40 秒原始流中，且它自己的新决策在 H 后，两个前瞻七天结果窗不重叠。没有及时采到、价格/报价缺失、来源阻断或时钟不合格，一律 MISSING_ENDPOINT，保留尝试，不延长终点、不借日 K/以后首笔/历史补采。成熟时间是 H 且必须等端点实际收到并核验；若端点缺失则永不补称成熟。

只作非可执行的 USDT 价格代理：P0=P0_USD/bid0，P1=P1_USD/ask1；毛代理 P1/P0-1。费用压力代理固定每边现货费 0.001、滑点 0.0005，净代理为 [P1*(1-0.0005)*(1-0.001)]/[P0*(1+0.0005)*(1+0.001)]-1；另列毛值。两价都是 Coinbase 逐笔标记价，报价仅为 Coinbase USDT/USD，不能声称是 Binance BTCUSDT 成交、订单利润或真实回撤。固定同钟对照：各单场原始买卖失衡（同 ±0.10）、共同窗内 Coinbase 首末真实 match 的连续价格变化、全部窗口恒定 0.5 新风险预算；正需求还对照恒定 0.25 新风险预算。只作方向和增量判断；预算经济比较须以后在相同 BTC 参与度、费用、执行、启动时点的独立钱包中另行事前冻结和授权。未满至少 10 个真实跨越七天且结果合格、互不重叠的前瞻窗口，不声称预测、成本后净益或适合主策略；NO_EVENT 与缺失另计，不拿它们补足候选事件。

本单只冻结一次观察与以后结果口径，不启动采集、账户回测或模型重放。主分支保持运行代码与简短使用说明。
