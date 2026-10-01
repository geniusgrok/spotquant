"""Render descriptive reports from frozen audited accounts; never changes selection."""
import csv,json
from pathlib import Path
from validate_exports import validated


def percent(x): return f'{100*float(x):.2f}%'


def render(spot_path,perp_path,assessment_path,out):
 spot,perp,a=validated(spot_path,perp_path,assessment_path)
 out=Path(out);out.mkdir(parents=True,exist_ok=True)
 if any((out/name).exists() for name in ('all-account-summary.csv', 'ALPHA_BETA.md')):
  raise ValueError('choose a fresh report directory; never overwrite evidence')
 assert len(a['candidate_attribution'])==48
 lines=['# BTC 全方向实验、alpha/beta 与固定总资金结果','', '完整48项候选/压力账户、实际预算账户与七项固定经济对照均已交付。只用BTC，长期保留Coinquant合约与Spotquant现货，Starquant作为研究证据。工程默认：合约保留incumbent，现货采用consensus。原始目标未调整，两个项目经济资格仍NOT_MET；实际原生案例0、实际观察0自然日，NOT_QUALIFIED。','', '## 十二项候选基础场景','', '| 项目/候选 | CAGR | 连续代理MDD | BTCUSDT beta | 年化算术残差 | HAC7描述95%区间 |','|---|---:|---:|---:|---:|---|']
 all_rows=[]
 for kind,bundle in [('spot',spot),('perp',perp)]:
  rows=list(bundle['results'].items()) if kind=='spot' else [(c+'/'+s,r) for c,ss in bundle['results'].items() for s,r in ss.items()]
  for name,row in rows:
   assert row['complete'] and row['audit']['passed']
   stats=a['candidate_attribution'][kind+'/'+name];reg=stats['usdt_btc_regression'];ci=reg['residual_arithmetic_annualized_normal95_descriptive']
   all_rows.append({'project':kind,'candidate_scenario':name,'cagr':row['cagr'],'continuous_proxy_mdd':row['mdd'],'daily_mdd':stats['metrics']['daily_mdd'],'final_cny':row['final_cny'],'usdt_beta_btc':reg['beta_btc'],'annual_arithmetic_residual':reg['residual_arithmetic_annualized'],'hac7_normal95_low':ci[0],'hac7_normal95_high':ci[1],'daily_vol_annual':stats['metrics']['daily_volatility_annualized'],'daily_es5_loss':stats['metrics']['daily_es5_loss'],'longest_underwater_days':stats['metrics']['longest_daily_underwater_days'],'fees_usdt':stats['fees_usdt'],'funding_paid_usdt':stats['funding_paid_usdt'],'fill_count':stats['fill_count'],'material_btc_closing_days':stats['days_with_closing_btc_notional_at_least_usdt5'],'short_closing_days':stats['days_with_closing_short_position'],'native_execution_verified':False})
   if name.endswith('-base') or name.endswith('/base'):
    lines.append(f"| {kind}/{name} | {percent(row['cagr'])} | {percent(row['mdd'])} | {reg['beta_btc']:.3f} | {percent(reg['residual_arithmetic_annualized'])} | [{percent(ci[0])}, {percent(ci[1])}] |")
 lines+=['', '原Coin账户CAGR沿用365.2425天/年；统一日度归因、组合及算术残差使用365.25，末端相同但CAGR有极小年化差异（incumbent约0.00353个百分点），冻结采用仍用原账户口径。所有回归使用2454个共同日终收益：USDT账户对BTCUSDT，CNY账户对同日期汇率转换的BTC，保留双币种结果。beta是日度线性敏感度；截距按365.25算术年化，包含择时非线性、成本、币种和代理路径影响，并非可独立交易的alpha。HAC7/正态95%区间未修正全样本策略选择，不能据此宣称未来alpha或显著性；残差与beta不能相加解释几何CAGR。','', '现货共识的beta从0.377升至0.397，收益改善部分来自承担更多BTC敞口；年化日波动35.62%→37.36%，历史最差5%日平均损失4.24%→4.44%，尽管连续代理MDD略降。因此没有把全部4.07个百分点CAGR改善称为alpha，也没有称风险全面下降。合约条件空头实际产生13次空仓段、43个负仓位日和6次停机BUY平空，beta下降却把基础连续代理MDD推至67.29%，拒绝采用。','', '合约tail-sizing基础CAGR121.07%/MDD41.94%，优于incumbent119.23%/44.11%，但400ms延迟场景年化100.90%低于对应104.77%超过登记容忍，拒绝。去宏观候选原先仅作依赖诊断，不能晋升；其他候选也未通过全部冻结约束。不同费用或延迟可能改变后续持仓/保护/入场序列，某场景收益更高不说明费用为收益源。','', '## 固定总额10,000元的实际资金组合','', '每个2500/5000/7500账户独立重新运行795次会话，10k末端使用同策略完整账户。两项目各10k不能相加冒充总10k。无追加、转账或再平衡；下表日终MDD与原目标的连续MDD口径不同，不能当作通过风险目标。','']
 for title,key in [('冻结原等份现货/原合约对照','joint_fixed_capital'),('已选现货共识/原合约','joint_selected_fixed_capital')]:
  lines += ['### '+title,'','| 初始现货/合约 CNY | 期末合计 CNY | CAGR | 日终MDD | 最大日终总名义/权益 |','|---|---:|---:|---:|---:|']
  for r in a[key]:
   m=r['metrics'];lines.append(f"| {r['spot_initial_cny']}/{r['perp_initial_cny']} | {m['final_cny']:,.2f} | {percent(m['cagr'])} | {percent(m['daily_mdd'])} | {r['maximum_closing_gross_exposure_over_equity']:.3f} |")
 lines+=['', f"原等份现货与合约的日收益相关系数为{a['daily_return_correlation_spot_perp']:.3f}。低于1不代表跨资产分散；两个账户都受BTC与同一交易场所影响。组合数据用于评估固定资金分配的历史结果，不根据它再选择策略或自动配置实际账户。日终名义比不是最大日内杠杆，USDT有汇率和稳定币风险。",'', '## 固定经济对照','', '| 对照 | 成本后CAGR | 日终MDD | 期末CNY |','|---|---:|---:|---:|']
 for name,v in a['passive_controls'].items():
  m=v['metrics'];lines.append(f"| {name} | {percent(m['cagr'])} | {percent(m['daily_mdd'])} | {m['final_cny']:,.2f} |")
 lines+=['', 'cash指USDT现金并按人民币估值，因FX并非恒定10k人民币。固定25/50/75%BTC只约束起始比例，随后不再平衡，BTC上涨会令持币权重增加。12月DCA仅分配原10k池而非每月追加。volatility_control40仅用prior20已完成日收益RMS、40%目标、100%BTC上限；目标波动率不保证实现。所有对照是分数BTC/日开盘经济路径，使用相同现货费率与滑点，期末盯市不假设卖出；它们不是实际session/Lifecycle策略，不参与采用。','', '## 采用默认的分年度表现','', '| 年份/覆盖范围 | Spot consensus | Coin incumbent |','|---|---:|---:|']
 sa=a['candidate_attribution']['spot/consensus-base'];pa=a['candidate_attribution']['perp/incumbent/base']
 for year,period in a['calendar_return_periods'].items():
  lines.append(f"| {year}: {period['start_utc']} 至 {period['end_exclusive_utc']} exclusive | {percent(sa['calendar_returns'][year])} | {percent(pa['calendar_returns'][year])} |")
 lines+=['', '2026年只到9月19日，不是全年或年化预测。非零BTC余额天数含保留残币，另输出名义至少USDT5的天数；该数也不是策略信号次数。最大水下时长、尾部损失、上跌日捕获、资金费、费用和每个压力场景完整指标均保存在JSON/CSV。','', '## 可复算的交付文件','', '- [完整48项归因和实际资金组合](assessment.json)，输入文件SHA、每项测量源、混合Spot来源和归因源明确绑定。','- [全部48项指标CSV](all-account-summary.csv)；[十二项基础结果图](plots/candidate-risk-return.svg)；[实际资金权益图](plots/fixed-capital-equity.svg)；[描述性alpha/beta及区间](plots/descriptive-alpha-beta.svg)。','- [Spot原件与修复来源](RESULT.md)；Coin原995全28原件完整保存，exclusive派生只移除slow-trend四项恰在END的负资金费，其余24项资金/成交/原MDD不变；原995原件不被修改或重标为新源。派生绑定每个原row SHA，独立审计确认无END交易/保护影响，原最大MDD均在END之前。','- [当前操作/复现指南](../../research/complete-delivery-GUIDE.md)。原生六案例、所有者账户核对、原生保护空窗和实际30自然日必须由真实事件完成；合成恢复演练不计原生证明或日历时间。','', '本交付没有读取交易所凭据、访问真实账户、发订单、转账或修改账户设置。Starquant差异诊断已经独立交付，保留长久研究参考；两个长期入口仍只有Coinquant和Spotquant。','']
 with (out/'all-account-summary.csv').open('x',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(all_rows[0]));w.writeheader();w.writerows(all_rows)
 with (out/'ALPHA_BETA.md').open('x') as f:f.write('\n'.join(lines))

if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser(description=__doc__)
 for n in ('spot','perp','assessment','outdir'):p.add_argument('--'+n,required=True)
 v=p.parse_args();render(v.spot,v.perp,v.assessment,v.outdir)
