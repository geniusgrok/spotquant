# BTC完整交付的复现与所有者操作

长期入口为Coinquant合约和Spotquant现货；Starquant保留研究原件。只用BTC，
原始资金、窗口、会话和收益/风险目标不变。研究进程不是账户守护进程。

## 完整研究

先按两仓已有restore工具恢复公共输入和CHECKSUM（含2026-09官方资金费），
在Spot目录生成与冻结SHA完全相同的特征，再运行干净已提交的源：

```sh
python -m research.prepare_inputs --futures-input evidence/complete-delivery-20261001/futures.json --out /tmp/crowding-NEW.json
python -m research.complete_spot --crowding /tmp/crowding-NEW.json --workers 2 --out /tmp/spot-accounts-NEW.json
python -m research.portfolio_spot --crowding /tmp/crowding-NEW.json --out /tmp/spot-budgets-NEW.json
```

在Coin目录使用同一特征：

```sh
python -m research.complete_perp --crowding /tmp/crowding-NEW.json --restore-prints --out /tmp/perp-accounts-NEW.json
```

输出使用新名称；795个起点不可移动。Spot价格为日线open→high(08h)→
low(16h)→close(24h)代理，Coin逐笔只给成交量上界并用分钟路径/缺口包络。
实际有限session/Lifecycle不等于真实盘口或原生证明。全部场景完整、资金
审计和执行门通过后才按PROTOCOL机械选择；中断的complete=false不可晋升。

## 收益与联合资金

2500/5000/7500元每个账户实际重新运行，不缩放10k曲线；联合总额始终
10000元，无追加、再平衡或转账。不能把两个10k账户相加当10k投资组合。

`complete_assessment`校验实际FX/spot archives及各账户冻结输入、795会话，
给出USDT/CNY BTC回归、下跌日beta、费用/资金费、年收益、历史ES5%、
每日水下时长及敞口。OLS截距和HAC7统计仅描述已研究历史，包含非线性
择时影响，不证明未来alpha。日终敞口不是最大日内杠杆，联合日終MDD不是
连续MDD。cash/buyhold/12月DCA明确是固定日开盘、分数BTC经济基线，
不是实际session账户。两项目都持BTC，项目数量不意味着资产分散。

## 成交和恢复

Spot将完整fills保存到SQLite，一天重叠按fillID去重，同毫秒新ID不丢失；
改变过往成交、超期缺口或无法解释的余额仍Unknown。已归属卖出的不足
步长残币保留真实分仓数量，合法新入场合并数量/成本并重建fill高点；
残币不能挂保护时不称已覆盖。旧状态缺可靠归属时不自动接管或推断转账。

每次会话在固定state_dir/sessions保存不可覆盖报告及在线backup。先验证：

```sh
python -m research.restore_check /PRIVATE/state/sessions/REPORT.json
```

核对hash、完整性、账户scope及report/source绑定，再由所有者将backup复制
到隔离目录，从只读status核对实际账户。原目录与恢复目录不能同时管理
一个账户。SQLite完整不等于账户已核对，不授权新风险。不要删状态/换空
目录绕过未知。Coin原备份轮转保留，两项目均需所有者保存异地副本。
`recovery_drill`留的是纯合成入场/备份/重启/外部BTC拒绝演练，非原生验收。

## 原生回包和实际30日

所有者先按README只读核对Demo UID，再显式启动受控Demo（Spot为
demo-check）。资金上限限制规模，不是亏损保险；工具不造信号/代发订单。
真实账户原件和凭据保持在私人目录，不提交公开Git。建立当前源模板：

```sh
python -m research.native_acceptance /PRIVATE/spot/manifest.json --package spotquant --template
python -m research.native_acceptance /PRIVATE/perp/manifest.json --package ../coinquant/coinquant --template
```

填UID、资金上限及六个case文件SHA：entry、partial_protection、reduction、
restart、stopped_trigger、cancel_replace。每个文件有project/environment/
account_uid/execution_code_sha256、origin=owner_captured_binance_demo、
synthetic=false；events含observed_at_ms、endpoint及真实native response。
response需BTCUSDT原生orderId/algoId。部分保护有observed_position_btc，
重启有before_client_ids/after_client_ids/new_submissions，停机触发有
session_ended_at_ms和真实updateTime；现货换保护有non_atomic_gap_acknowledged。

```sh
python -m research.native_acceptance /PRIVATE/spot/manifest.json --package spotquant --archive /PRIVATE/spot/review-NEW
```

结构齐备只设置ready_for_owner_native_review；工具始终native_execution_verified
false、NOT_QUALIFIED、不开放权限。标签是所有者声明，hash不能认证Binance
或防止伪造JSON；所有者须核对原生来源、实际覆盖及非原子空窗。源摘要
不匹配、缺case、重复/已取消止损假覆盖和变更字节均不通过结构检查。

现有`research.operations collect`按实际UTC日期并行只读采集两账户，失败
和缺日不补造。历史/合成记录或工程耗时不计实际30自然日；本轮native案例
0、实际观察0日，不能用一次离线交付让日历经过30天。代理本轮没有调用
凭据、账户、订单、转账或账户设置。
