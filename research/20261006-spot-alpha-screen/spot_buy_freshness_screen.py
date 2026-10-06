import collections,gzip,json,statistics,zipfile
from decimal import Decimal as D
p=json.load(gzip.open('/tmp/spot-baseline-projection.json.gz'))
with zipfile.ZipFile('/tmp/spot-search-artifacts.zip') as z:
    d=json.loads(z.read('spot/diagnosis.json'))
cohorts={int(c['buy_order_id']):c for c in d['cohorts']}
events={x['order_id']:x for x in p['opportunity_ledger'] if x['event']=='fill' and x['buyer']}
rows=[]
for oid,c in cohorts.items():
    e=events[oid]
    age=(e['decision_ms']-e['signal_ms']-86400000)/3600000
    rows.append({'order_id':oid,'year':c['entry_year'],'age_hours_after_bar_close':age,'stale':age>12,'pnl_usdt':D(c['realized_pnl_usdt']),'cost_usdt':D(c['cost_usdt']),'residual_btc':D(c['residual_btc'])})
print('source_cohorts',len(cohorts),'fill_buy_events',len(events),'duplicate_order_events',len([x for x in p['opportunity_ledger'] if x['event']=='fill' and x['buyer']])-len(events))
print('age_hours_min_median_max',min(x['age_hours_after_bar_close'] for x in rows),statistics.median(x['age_hours_after_bar_close'] for x in rows),max(x['age_hours_after_bar_close'] for x in rows))
for period,sub in [('early',[x for x in rows if x['year']<=2021]),('late',[x for x in rows if x['year']>=2022]),('all',rows)]:
  for stale,group in [(True,[x for x in sub if x['stale']]),(False,[x for x in sub if not x['stale']])]:
    print(period,'stale' if stale else 'fresh','n',len(group),'cost',sum(x['cost_usdt'] for x in group),'pnl',sum(x['pnl_usdt'] for x in group),'losers',sum(x['pnl_usdt']<0 for x in group),'residual',sum(x['residual_btc'] for x in group))
