# BTC完整交付的复现与所有者操作

长期入口为Coinquant合约和Spotquant现货；Starquant保留研究原件。只用BTC，
原始资金、窗口、会话和收益/风险目标不变。研究进程不是账户守护进程。

## 完整研究

先按两仓已有restore工具恢复公共输入和CHECKSUM（含2026-09官方资金费），
在Spot目录生成与冻结SHA完全相同的特征，再运行干净已提交的源：

```sh
python -m research.prepare_inputs --futures-input evidence/complete-delivery-20261001/futures.json --out /tmp/crowding-NEW.json
mkdir -p /workspace/scratch/spotquant-complete-tmp
TMPDIR=/workspace/scratch/spotquant-complete-tmp python -m research.complete_spot --crowding /tmp/crowding-NEW.json --workers 2 --out /tmp/spot-accounts-NEW.json
TMPDIR=/workspace/scratch/spotquant-complete-tmp python -m research.portfolio_spot --candidate default --crowding /tmp/crowding-NEW.json --out /tmp/spot-baseline-budgets-NEW.json
TMPDIR=/workspace/scratch/spotquant-complete-tmp python -m research.portfolio_spot --candidate consensus --crowding /tmp/crowding-NEW.json --out /tmp/spot-consensus-budgets-NEW.json
```

在Coin目录使用同一特征：

```sh
python -m research.complete_perp --crowding /tmp/crowding-NEW.json --restore-prints --out /tmp/perp-accounts-NEW.json
```

输出使用新名称；795个起点不可移动。Spot价格为日线open→high(08h)→
low(16h)→close(24h)代理，Coin逐笔只给成交量上界并用分钟路径/缺口包络。
实际有限session/Lifecycle不等于真实盘口或原生证明。全部场景完整、资金
审计和执行门通过后才按PROTOCOL机械选择；中断的complete=false不可晋升。
每个回放会话验证临时SQLite备份，单个账户临时目录约需5GB；两个研究
worker加一个预算进程应预留至少20GB。TMPDIR只改变研究临时路径，
不删除真实state_dir的归档，也不改变资金、会话或成交。

## 收益与联合资金

2500/5000/7500元每个账户实际重新运行，不缩放10k曲线；联合总额始终
10000元，无追加、再平衡或转账。不能把两个10k账户相加当10k投资组合。当前默认是已通过四场景采用规则的P4共识仓位；研究default仍冻结原等份基线。联合报告分别列基线与选中策略的完整五种配比，不能混用不同策略预算和10k末端。

`complete_assessment`校验实际FX/spot archives及各账户冻结输入、795会话，
给出USDT/CNY BTC回归、下跌日beta、费用/资金费、年收益、历史ES5%、
每日水下时长及敞口。OLS截距和HAC7统计仅描述已研究历史，包含非线性
择时影响，不证明未来alpha。日终敞口不是最大日内杠杆，联合日终MDD不是
连续MDD。2026年收益只覆盖至9月19日，未完整一年；非零BTC持仓日数包含残币，另列名义至少5USDT日数，不能解释为信号次数。cash/buyhold/12月DCA、固定25/50/75%BTC及prior20return RMS的40%年波动上限策略，明确是固定日开盘、分数BTC经济基线，
不是实际session账户。两项目都持BTC，项目数量不意味着资产分散。

## 成交和恢复

Spot将完整fills保存到SQLite，一天重叠按fillID去重，同毫秒新ID不丢失；
改变过往成交、超期缺口或无法解释的余额仍Unknown。已归属卖出的不足
步长残币保留真实分仓数量。合并全卖的比例分配可能留下略大于一个数量步长但名义不足5USDT的残币；仅经完整终态、确为全组取整卖出、逐订单实际SELL累计及严格小额门证明后，战略视图才视为平仓，真实拥有数量不消失。晚到终态回读回滚事务等待恢复，刻意减仓/部分成交/旧状态歧义不按平仓处理。合法新入场合并数量/成本并重建fill高点；
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

## 原始测量与当前默认的区别

现货完整集合保留每项实际测量源：16项原等份/减仓/过滤结果来自2d1e5fe，
原共识4项失败原件不参与选择；替代共识4项来自修复源1fca802，
实际共识3预算来自7b4b44e（相同Python源码摘要）。详细完整40位SHA见
`evidence/complete-delivery-20261001/RESULT.md`。零影响证明针对旧源与
已审查修复文件绑定，`assemble_spot`还验证不可变测量Git树。当前默认
抽取同一共识函数，研究基线显式consensus=False；独立2592组可执行
委托/决策/保护等价性验证通过，不伪称旧16账户由当前源重新测量。

复查原始组合时可在隔离Git worktree的7b4b44e完整快照中使用已保留的
原件、修复4项和证明；该证明刻意拒绝不同执行文件，不能在新默认源上
重新贴标签。若重新研究，直接在干净当前源完整运行全部20项、基线及
共识各3预算，使用新输出名并重新计算源摘要。

当前运行源零案例模板是`spot-native-template-consensus.json`，验收
`spot-native-acceptance-consensus.json`，合成演练为`recovery-drill-consensus`。
早期`*-final`文件来自旧源，只作历史。源摘要变化后应在私人目录生成
新模板，不修改旧验收输出。所有模板仍留空账户和资金，零原生/零观察，
不会由工程脚本自动启动Demo。

## 完整归因和固定资金的复算

Coin预算使用同一primary schedule和同一已封存特征，三个账户独立建账：

```sh
python -m research.complete_perp --crowding /tmp/crowding-NEW.json --restore-prints --portfolio-budgets --out /tmp/perp-budgets-NEW.json
```

回算已交付原件，在Spot目录使用新的输出名：

```sh
python -m research.complete_assessment --spot evidence/complete-delivery-20261001/spot-accounts-verified.json --perp ../coinquant/evidence/complete-delivery-20261001/perp-exclusive-accounts.json --spot-budgets evidence/complete-delivery-20261001/portfolio-spot-accounts-final.json --spot-selected-budgets evidence/complete-delivery-20261001/portfolio-spot-consensus.json --perp-budgets ../coinquant/evidence/complete-delivery-20261001/portfolio-perp-accounts.json --out /tmp/assessment-NEW.json
python evidence/complete-delivery-20261001/render_report.py --spot evidence/complete-delivery-20261001/spot-accounts-verified.json --perp ../coinquant/evidence/complete-delivery-20261001/perp-exclusive-accounts.json --assessment /tmp/assessment-NEW.json --outdir /tmp/btc-report-NEW
python evidence/complete-delivery-20261001/export_plots.py --spot evidence/complete-delivery-20261001/spot-accounts-verified.json --perp ../coinquant/evidence/complete-delivery-20261001/perp-exclusive-accounts.json --assessment /tmp/assessment-NEW.json --outdir /tmp/btc-report-NEW/plots
```

图表导出可选依赖matplotlib，核心运行和CI仍只需标准库。归因还验证Coin
预算的完整市场/协议/会话身份与full28一致，并显式保留budget测量源和
exclusive终点派生源，不能只凭相同FX来拼不同市场输入。报告脚本只渲染
已通过资金审计的冻结结果，不调参或改变采用决策。

实际预算与联合配比只测基础场景，没有四种组合压力或连续联合MDD证明。
合约不同本金产生不同成交和权益反馈路径，不可按比例缩放、用小本金CAGR
替代10000元原目标，或把历史最高配比称为最优。公共交付的manifest.json
逐文件绑定字节数和SHA256，排除自身、进度文件和__pycache__；核对清单
后再复算。合成SQLite仅用于恢复检查，不是原生账户证明。

从Spot仓库根目录验证整个公共交付清单：

```sh
python - <<'PYVERIFY'
from pathlib import Path
import json, hashlib
manifest = json.loads(Path('evidence/complete-delivery-20261001/manifest.json').read_text())
for name, item in manifest['files'].items():
    data = Path(name).read_bytes()
    assert len(data) == item['bytes'], name
    assert hashlib.sha256(data).hexdigest() == item['sha256'], name
print('All public delivery file hashes verified')
PYVERIFY
```

独立审查脚本和结果同样归档。review_final_btc_assessment.py保留审查时的
/workspace和/tmp公共输入路径；在相同布局和恢复输入下运行，可重新核对
48账户及10组合共116组双币回归，不能据此认证真实交易所回包。
