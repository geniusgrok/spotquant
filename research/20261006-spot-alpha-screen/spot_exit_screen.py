import collections, gzip, json, hashlib, statistics
from decimal import Decimal as D
from pathlib import Path
from datetime import datetime, timezone
p=json.load(gzip.open('/tmp/spot-baseline-projection.json.gz'))
alloc_by_id={}
for row in p['allocations']:
    intent,response=json.loads(row[1]),json.loads(row[3])
    if 'orderId' in response:
        alloc_by_id[int(response['orderId'])]=intent
fills=sorted(p['fills'],key=lambda f:(f['time'],f['id']))
fill_events={x['id']:x for x in p['opportunity_ledger'] if x['event']=='fill'}
decisions=collections.defaultdict(list)
for x in p['opportunity_ledger']:
    if x['event']=='decision' and x.get('accepted_orders'):
        decisions[x['decision_ms']].append(x)
books=collections.defaultdict(collections.deque)
market=[]; stop=[]; mismatches=[]
for f in fills:
    intent=alloc_by_id[f['order_id']]
    ev=fill_events[f['id']]
    weights={int(k):D(v) for k,v in intent['weights'].items()}
    weight_total=sum(weights.values())
    qty,quote,fee=(D(f[k]) for k in ('qty','quote','commission'))
    if f['buyer']:
        net=qty-(fee if f['commission_asset']=='BTC' else 0)
        spend=quote+(fee if f['commission_asset']=='USDT' else 0)
        for s,w in weights.items():
            books[s].append({'left':net*w/weight_total,'cost_per_btc':spend/net})
        continue
    removed=qty+(fee if f['commission_asset']=='BTC' else 0)
    proceeds=quote-(fee if f['commission_asset']=='USDT' else 0)
    pnl=D(0); pnl_by_sleeve={}; fragments=0
    for s,w in weights.items():
        amount=removed*w/weight_total; sleeve_pnl=D(0)
        while amount>D('1e-23'):
            if not books[s]: raise ValueError('missing owned BTC')
            lot=books[s][0]; take=min(amount,lot['left'])
            sleeve_pnl+=proceeds*take/removed-lot['cost_per_btc']*take
            lot['left']-=take; amount-=take; fragments+=1
            if lot['left']<=D('1e-23'): books[s].popleft()
        pnl+=sleeve_pnl; pnl_by_sleeve[s]=sleeve_pnl
    dp=D(ev['decision_price']) if ev.get('decision_price') else None; fp=D(f['price'])
    matches=[]
    for dec in decisions.get(ev.get('decision_ms'),[]):
        for order in dec['accepted_orders']:
            if order['side']=='SELL' and order['type']==intent['order']['type'] and set(order.get('sleeves',[]))==set(weights) and D(order.get('quantity','0'))==D(intent['order'].get('quantity','0')):
                matches.append(dec)
    if intent['order']['type']=='MARKET' and len(matches)!=1: mismatches.append({'order_id':f['order_id'],'decision_matches':len(matches)})
    action={str(s):matches[0]['sleeves'][str(s)]['action'] for s in weights} if len(matches)==1 else None
    record={'order_id':f['order_id'],'fill_id':f['id'],'year':datetime.fromtimestamp(f['time']/1000,timezone.utc).year,
      'sleeves':sorted(weights),'actions':action,'qty':str(qty),'pnl_usdt':str(pnl),'pnl_by_sleeve':{str(k):str(v) for k,v in pnl_by_sleeve.items()},
      'fragments':fragments,'signal_to_decision_hours':(ev['decision_ms']-ev['signal_ms'])/3600000 if ev.get('decision_ms') is not None and ev.get('signal_ms') is not None else None,
      'decision_to_fill_ms':f['time']-ev['decision_ms'] if ev.get('decision_ms') is not None else None,'decision_vs_fill_bp':float((dp-fp)/dp*10000) if dp else None,
      'decision_vs_fill_usdt':str((dp-fp)*qty) if dp else None,'fee_usdt':str(fee if f['commission_asset']=='USDT' else fee*fp),
      'decision_matches':len(matches)}
    (market if intent['order']['type']=='MARKET' else stop).append(record)
counts=collections.Counter(f['order_id'] for f in fills)
print('source_sha256',hashlib.sha256(Path('/tmp/spot-baseline-projection.json.gz').read_bytes()).hexdigest())
print('fill_counts',len(fills),len(counts),'partial_multi_fill_orders',sum(c>1 for c in counts.values()),'max_fills_per_order',max(counts.values()))
print('market_orders',len(market),'stop_orders',len(stop),'match_mismatches',len(mismatches),mismatches[:5])
print('market_actions',dict(collections.Counter(tuple(sorted(x['actions'].items())) if x['actions'] else None for x in market)))
for label,rows in [('market',market),('stop',stop)]:
    print(label,'orders',len(rows),'fragments',sum(x['fragments'] for x in rows),'pnl',sum(D(x['pnl_usdt']) for x in rows),'negative_orders',sum(D(x['pnl_usdt'])<0 for x in rows),'negative_gross',sum(min(D(x['pnl_usdt']),D(0)) for x in rows))
    print(label,'by_year',dict(sorted((year,{'orders':len(v),'negative_orders':sum(D(x['pnl_usdt'])<0 for x in v),'pnl':str(sum(D(x['pnl_usdt']) for x in v))}) for year,v in ((y,[x for x in rows if x['year']==y]) for y in sorted(set(x['year'] for x in rows))))))
    print(label,'by_sleeve', {str(s):{'orders':sum(s in x['sleeves'] for x in rows),'pnl':str(sum(D(x['pnl_by_sleeve'].get(str(s),'0')) for x in rows))} for s in [30,40,50]})
    print(label,'signal_hours_unique',dict(collections.Counter(round(x['signal_to_decision_hours'],3) if x['signal_to_decision_hours'] is not None else None for x in rows).most_common(10)))
    print(label,'decision_fill_ms',dict(collections.Counter(x['decision_to_fill_ms'] for x in rows)))
    bps=[x['decision_vs_fill_bp'] for x in rows if x['decision_vs_fill_bp'] is not None]
    print(label,'adverse_bp_min_med_max', (min(bps),statistics.median(bps),max(bps)) if bps else None,'sum_quote',sum(D(x['decision_vs_fill_usdt']) for x in rows if x['decision_vs_fill_usdt'] is not None))
    print(label,'fees',sum(D(x['fee_usdt']) for x in rows))
print('remaining_btc',p['btc'],'position_dust', {s:x['dust'] for s,x in p['positions'].items()})
Path('/tmp/spot-exit-screen.json').write_text(json.dumps({'source_sha256':hashlib.sha256(Path('/tmp/spot-baseline-projection.json.gz').read_bytes()).hexdigest(),'market':market,'stop':stop,'mismatches':mismatches,'remaining_btc':p['btc']},indent=2))
