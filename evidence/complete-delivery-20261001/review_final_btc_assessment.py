import sys,json,hashlib,subprocess,csv,bisect,math,statistics
from pathlib import Path
from decimal import Decimal as D
from datetime import datetime,timezone
sys.path.insert(0,'/workspace/spotquant')
from research.market import load_daily,file_digest
from research.rebuild import START_MS,END_MS
DAY=86400000;root=Path('/workspace/spotquant');ev=root/'evidence/complete-delivery-20261001';coin=root.parent/'coinquant';ce=coin/'evidence/complete-delivery-20261001'
paths={'spot':ev/'spot-accounts-verified.json','perp':ce/'perp-exclusive-accounts.json','spot_budgets':ev/'portfolio-spot-accounts-final.json','perp_budgets':ce/'portfolio-perp-accounts.json','spot_selected_budgets':ev/'portfolio-spot-consensus.json'}
inputs={k:json.loads(p.read_text()) for k,p in paths.items()};report=json.load(open(ev/'assessment.json'))
for k,p in paths.items():assert hashlib.sha256(p.read_bytes()).hexdigest()==report['inputs'][k]
assert report['qualification']=='NOT_QUALIFIED' and not report['native_execution_verified']
assert report['spot_selection']['selected_research_candidate']=='consensus' and report['perp_selection']['selected_research_candidate']=='incumbent'
for k in ('market_identity','protocol_sha256','schedule_sha256','fx_sha256','crowding_sha256'):assert inputs['perp']['inputs'][k]==inputs['perp_budgets']['inputs'][k]
source=inputs['perp_budgets']['inputs']['source'];assert not source['dirty']
names=sorted(subprocess.check_output(['git','ls-tree','-r','--name-only',source['git_head'],'--','coinquant','research'],cwd=coin).decode().splitlines());h=hashlib.sha256()
for name in names:
 if name.endswith('.py'):h.update(name.encode()+b'\0'+subprocess.check_output(['git','show',source['git_head']+':'+name],cwd=coin)+b'\0')
assert h.hexdigest()==source['python_sources_sha256']
protocol=subprocess.check_output(['git','show',source['git_head']+':research/complete-delivery-PROTOCOL.md'],cwd=coin);assert hashlib.sha256(protocol).hexdigest()==inputs['perp_budgets']['inputs']['protocol_sha256']
fxpath=root.parent/'starquant/data/usdcny_frankfurter.json';data=json.load(open(fxpath))['rates'];days=sorted(data)
assert hashlib.sha256(fxpath.read_bytes()).hexdigest()==inputs['spot']['fx_sha256']==inputs['perp_budgets']['inputs']['fx_sha256']
def fx(t):
 day=datetime.fromtimestamp(t/1000,timezone.utc).date().isoformat();i=bisect.bisect_left(days,day)-1
 return D(str(data[days[i]]['CNY'])) if i>=0 else D('6.9615')
starts=json.load(open(coin/'research/session_schedule.json'))['primary']['starts_ms']
assert hashlib.sha256((coin/'research/session_schedule.json').read_bytes()).hexdigest()==inputs['spot']['schedule_sha256']
assert hashlib.sha256(json.dumps(starts,separators=(',',':')).encode()).hexdigest()==inputs['perp_budgets']['inputs']['schedule_sha256']
market=Path('/tmp/spotquant-market/klines');assert file_digest(market)==inputs['spot']['market_sha256'];bars=[b for b in load_daily(market,END_MS,require_through=END_MS) if START_MS<=b[0]<END_MS]
assert len(bars)==2454
budget_summary={}
for label,r in inputs['perp_budgets']['results'].items():
 assert r['candidate']=='incumbent' and r['scenario']=='base' and r['initial_cny']==label and r['complete'] and r['audit']['passed'] and not r['execution_unresolved']
 assert [s['start_ms'] for s in r['sessions']]==starts and len(r['sessions'])==795
 assert all(not s['execution_unresolved'] for s in r['sessions'])
 qty=entry=realized=fees=D(0);seen=set()
 for t in r['trades']:
  assert t['id'] not in seen;seen.add(t['id']);q=D(t['qty']);p=D(t['price']);change=q if t['side']=='BUY' else -q
  fees+=q*p*D('.00075')
  if not qty or qty*change>0:entry=(abs(qty)*entry+q*p)/(abs(qty)+q)
  else:
   realized+=min(abs(qty),q)*(p-entry)*(1 if qty>0 else -1)
   if q>abs(qty):entry=p
  qty+=change
  if not qty:entry=D(0)
 totals={}
 for t in r['funding_ledger']:totals[t['incomeType']]=totals.get(t['incomeType'],D(0))+D(t['income'])
 assert set(totals)<={'COMMISSION','REALIZED_PNL','FUNDING_FEE','INSURANCE_CLEAR'}
 assert abs(qty-D(r['position']))<D('1e-20') and abs(realized-totals.get('REALIZED_PNL',D(0)))<D('1e-15')
 assert abs(fees-D(r['fees']))<D('1e-15') and abs(D(r['funding'])+totals.get('FUNDING_FEE',D(0)))<D('1e-15')
 wallet=D(label)/fx(START_MS)*D('.999')+sum(totals.values(),D(0));equity=wallet+qty*(D(r['final_mark'])-entry)
 assert abs(equity-D(r['final_usdt']))<D('1e-15')
 assert abs(equity*fx(END_MS)*D('.999')-D(r['final_cny']))<D('1e-15')
 budget_summary[label]={'sessions':len(r['sessions']),'fills':len(r['trades']),'final_cny':r['final_cny'],'cagr':r['cagr']}
def curve(row,kind,initial):
 raw=list(row['daily'].values()) if isinstance(row['daily'],dict) else row['daily'];tk,qk,ck=('timestamp_ms','btc','cash_usdt') if kind=='spot' else ('stamp_ms','quantity_btc','wallet_usdt');raw=sorted(raw,key=lambda r:r[tk]);i=0
 last={tk:START_MS,qk:'0',ck:str(D(initial)/fx(START_MS)*D('.999'))};out=[]
 for day,op,hi,lo,close,v in bars:
  target=day+DAY
  while i<len(raw) and raw[i][tk]<=target:last=raw[i];i+=1
  q=D(last[qk]);assert not q or last[tk]==target
  usd=D(last['equity_usdt']) if q else D(last[ck]);assert usd>0
  out.append({'usd':float(usd),'cny':float(usd*fx(target)*D('.999')),'q':float(q),'price':float(close)})
 assert abs(out[-1]['cny']-float(row['final_cny']))<1e-5
 return out
def returns(values,initial):return [values[0]/initial-1]+[b/a-1 for a,b in zip(values,values[1:])]
def ols(y,x):
 n=len(x);mx=sum(x)/n;my=sum(y)/n;ss=sum((v-mx)**2 for v in x);beta=sum((a-mx)*(b-my) for a,b in zip(x,y))/ss;alpha=my-beta*mx
 errors=[b-alpha-beta*a for a,b in zip(x,y)];influence=[e*(1/n-mx*(a-mx)/ss) for a,e in zip(x,errors)]
 var=sum(v*v for v in influence)
 for lag in range(1,8):var+=2*(1-lag/8)*sum(influence[i]*influence[i-lag] for i in range(lag,n))
 se=math.sqrt(max(0,var)*n/(n-2))
 return beta,alpha,se
usd_btc=[];cny_btc=[];p=bars[0][1];cp=p*fx(START_MS)
for b in bars:
 usd_btc.append(float(b[4]/p-1));nc=b[4]*fx(b[0]+DAY);cny_btc.append(float(nc/cp-1));p=b[4];cp=nc
rows={'spot/'+k:('spot',r) for k,r in inputs['spot']['results'].items()};rows.update({'perp/'+c+'/'+s:('perp',r) for c,rr in inputs['perp']['results'].items() for s,r in rr.items()});assert len(rows)==48 and set(rows)==set(report['candidate_attribution'])
curves={}
def check_reg(actual,values,initial,benchmark):
 beta,alpha,se=ols(returns(values,initial),benchmark)
 for key,v in [('beta_btc',beta),('intercept_daily',alpha),('intercept_hac7_standard_error_daily',se)]:assert abs(actual[key]-v)<1e-10,(key,actual[key],v)
 for sign,v in zip((-1,1),actual['residual_arithmetic_annualized_normal95_descriptive']):assert abs(v-365.25*(alpha+sign*1.96*se))<1e-9
 assert not actual['prospective_alpha_proven']
for key,(kind,row) in rows.items():
 assert row['complete'] and row['audit']['passed'];curves[key]=c=curve(row,kind,10000);stats=report['candidate_attribution'][key]
 check_reg(stats['usdt_btc_regression'],[r['usd'] for r in c],float(D(10000)/fx(START_MS)*D('.999')),usd_btc)
 check_reg(stats['cny_btc_regression'],[r['cny'] for r in c],10000,cny_btc)
 assert abs(stats['metrics']['final_cny']-c[-1]['cny'])<1e-7
joint_summary={}
for key,sc in [('joint_fixed_capital','default'),('joint_selected_fixed_capital','consensus')]:
 joint_summary[key]=[];assert len(report[key])==5
 for j in report[key]:
  pieces=[]
  for kind,budget,candidate in [('spot',j['spot_initial_cny'],sc),('perp',j['perp_initial_cny'],'incumbent')]:
   if budget==0:continue
   if budget==10000:c=curves['spot/'+candidate+'-base' if kind=='spot' else 'perp/incumbent/base']
   else:
    bundle=inputs['spot_selected_budgets'] if kind=='spot' and sc=='consensus' else inputs[kind+'_budgets'];r=bundle['results'][str(budget)];assert r['candidate']==candidate and D(r['initial_cny'])==D(budget);c=curve(r,kind,budget)
   pieces.append(c)
  assert j['spot_initial_cny']+j['perp_initial_cny']==10000 and not j['continuous_joint_mdd_verified'] and j['fixed_accounts_no_transfers']
  usd=[sum(c[i]['usd'] for c in pieces) for i in range(2454)];cny=[sum(c[i]['cny'] for c in pieces) for i in range(2454)]
  assert max(abs(a-b) for a,b in zip(cny,j['daily_equity_cny']))<1e-7
  check_reg(j['usdt_btc_regression'],usd,float(D(10000)/fx(START_MS)*D('.999')),usd_btc);check_reg(j['cny_btc_regression'],cny,10000,cny_btc)
  peak=10000;dd=0
  for v in cny:peak=max(peak,v);dd=max(dd,1-v/peak)
  for field,v in [('final_cny',cny[-1]),('daily_mdd',dd),('cagr',(cny[-1]/10000)**(365.25/2454)-1)]:assert abs(v-j['metrics'][field])<max(1e-8,abs(v)*1e-12)
  gross=max(sum(abs(c[i]['q'])*c[i]['price'] for c in pieces)/usd[i] for i in range(2454));assert abs(gross-j['maximum_closing_gross_exposure_over_equity'])<1e-12
  joint_summary[key].append({'spot_budget':j['spot_initial_cny'],'final_cny':cny[-1],'daily_mdd':dd,'usdt_beta':j['usdt_btc_regression']['beta_btc']})
records=list(csv.DictReader(open(ev/'all-account-summary.csv')));assert len(records)==48
for record in records:
 key=record['project']+'/'+record['candidate_scenario'];_,r=rows[key];stats=report['candidate_attribution'][key]
 for name,want in [('cagr',r['cagr']),('continuous_proxy_mdd',r['mdd']),('final_cny',r['final_cny']),('daily_mdd',stats['metrics']['daily_mdd']),('usdt_beta_btc',stats['usdt_btc_regression']['beta_btc']),('annual_arithmetic_residual',stats['usdt_btc_regression']['residual_arithmetic_annualized'])]:assert abs(float(record[name])-float(want))<1e-10
assert len(list(csv.DictReader(open(ev/'plots/candidate-summary.csv'))))==12
summary={'assessment_sha256':hashlib.sha256((ev/'assessment.json').read_bytes()).hexdigest(),'coin_budget_sha256':hashlib.sha256(paths['perp_budgets'].read_bytes()).hexdigest(),'coin_budget_source':source,'coin_budgets':budget_summary,'accounts':48,'currency_regressions_verified':116,'joint_curves':joint_summary,'csv_rows':48,'base_plot_csv_rows':12}
print(json.dumps(summary,indent=2))
