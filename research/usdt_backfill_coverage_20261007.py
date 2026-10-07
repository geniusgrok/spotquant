"""Source-bound count only: conditional CoinGecko cap coverage of original Spot BUYs.

Stops before slope signs, outcomes or wallets if the frozen support gate is unmet.
"""
import argparse,gzip,hashlib,json,zipfile
from decimal import Decimal as D
from pathlib import Path

DAY=86_400_000
ACCOUNT_SHA='4c937ecf80f60d957486a752562c8ab8dfee4c06fa2e5b38b135cfb24ec38872'
SEARCH_SHA='c9f9b30ab5f5a551a9a9bf5e9bd99ced3dc934b50d116eee1d9f5d59ff941cc3'
DAILY_SHA='6a35dadcbe9228d95191a2d2716bc2c2d9f01aa8be08638718325fbefb376009'
BACKFILL_SHA='5585da24ce972871b71173484235366207f4f9186d29539da747516993249894'
URL='https://api.coingecko.com/api/v3/coins/tether/market_chart?vs_currency=usd&days=365&interval=daily&precision=full'


def read_checked(path,sha):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('pinned source hash differs')
    return raw


def daily_points(raw):
    doc=json.loads(raw,parse_float=D)
    out=[]
    for key in ('market_caps','prices'):
        rows=doc[key]
        if not isinstance(rows,list):raise ValueError('market-chart array missing')
        values={};last=-1
        for item in rows:
            if (not isinstance(item,list) or len(item)!=2 or type(item[0]) is not int
                    or item[0]<=last):raise ValueError('market-chart event order')
            stamp,value=item;last=stamp
            if stamp%DAY:continue
            value=D(str(value))
            if not value.is_finite() or value<=0:raise ValueError('market-chart daily value')
            values[stamp]=value
        out.append(values)
    if set(out[0])!=set(out[1]):raise ValueError('cap/price day mismatch')
    return {day:(out[0][day],out[1][day]) for day in out[0]}


def coverage(account_path,search_path,backfill_path,receipt_path):
    account=json.loads(gzip.decompress(read_checked(account_path,ACCOUNT_SHA)))
    if account['source']['git_head']!='74bd6e035e36531c517029077c1a9e2e5ec44516' or account['source']['dirty']:
        raise ValueError('original account identity differs')
    result=account['results']['crowding-interaction-base']
    if not result['complete'] or not result['audit']['passed'] or result['execution_unresolved_sessions']:
        raise ValueError('original account incomplete')
    with zipfile.ZipFile(search_path) as archive:
        if hashlib.sha256(Path(search_path).read_bytes()).hexdigest()!=SEARCH_SHA:
            raise ValueError('search artifact changed')
        daily=archive.read('daily-composition.json')
    if hashlib.sha256(daily).hexdigest()!=DAILY_SHA:raise ValueError('BTC daily source changed')
    btc=json.loads(daily)['bars']
    cap=daily_points(read_checked(backfill_path,BACKFILL_SHA))
    receipt=json.loads(Path(receipt_path).read_bytes())
    if (receipt['url']!=URL or receipt['body_sha256']!=BACKFILL_SHA
            or receipt['http_status']!=200 or receipt['body_file']!=Path(backfill_path).name
            or receipt['received_ms']<receipt['started_ms']):raise ValueError('backfill receipt differs')
    decisions=[]
    for row in result['opportunity_ledger']:
        if row.get('event')!='decision':continue
        buys=[order for order in row['accepted_orders']
              if order.get('side')=='BUY' and order.get('type')=='MARKET']
        if buys:
            if len(buys)!=1:raise ValueError('more than one external BUY per manual decision')
            decisions.append(row)
    decisions.sort(key=lambda row:row['decision_ms'])
    unique={}
    for row in decisions:
        key=row['completed_bar_ms']
        if key in unique:
            prior=unique[key]
            old=[o for o in prior['accepted_orders'] if o['side']=='BUY']
            new=[o for o in row['accepted_orders'] if o['side']=='BUY']
            if old!=new:raise ValueError('one signal has conflicting accepted BUY')
            continue
        unique[key]=row
    decisions=list(unique.values())
    reasons={key:0 for key in ('outside_calendar','missing_three_cap_days','missing_btc_controls',
                              'missing_seven_day_endpoint','missing_fill')}
    aligned=[]
    for row in decisions:
        t=row['decision_ms'];day=row['completed_bar_ms']
        if t>=receipt['received_ms']:
            # Such a row could have strict receipt provenance, but this archived
            # original account predates the response and must never be relabeled.
            raise ValueError('old account unexpectedly contains a post-receipt decision')
        if not min(cap)<=t<max(cap)+DAY+600_000:
            reasons['outside_calendar']+=1;continue
        eligible=[stamp for stamp in cap if stamp+600_000<=t]
        stamp=max(eligible,default=-1)
        if any(stamp-i*DAY not in cap for i in (0,1,2)):
            reasons['missing_three_cap_days']+=1;continue
        if any(str(day-i*DAY) not in btc for i in range(21)):
            reasons['missing_btc_controls']+=1;continue
        # Coverage only: 20 prior completed returns give momentum and RMS,
        # without computing a regression or looking at candidate slope signs.
        closes=[D(btc[str(day-i*DAY)]['close']) for i in range(21)]
        if any(value<=0 for value in closes):raise ValueError('BTC control close invalid')
        if str(day+7*DAY) not in btc:
            reasons['missing_seven_day_endpoint']+=1;continue
        if not any(f['buyer'] and t<f['time']<t+60_000 for f in result['fills']):
            reasons['missing_fill']+=1;continue
        aligned.append(t)
    independent=[]
    for t in aligned:
        if not independent or t>=independent[-1]+7*DAY:independent.append(t)
    halves=(len(independent)//2,len(independent)-len(independent)//2)
    return dict(source='CONDITIONAL_BACKFILL',strict_pit_new_buy=0,
                backfill_received_ms=receipt['received_ms'],
                daily_first_ms=min(cap),daily_last_ms=max(cap),daily_count=len(cap),
                original_accepted_buy_count=len(decisions),
                calendar_aligned_with_controls_and_mature_endpoint=len(aligned),
                nonoverlap_seven_day_count=len(independent),chronological_half_counts=halves,
                rejected=reasons,minimum_support_met=(len(independent)>=10 and min(halves)>=3),
                slope_signs_or_outcomes_inspected=False,wallet_run=False)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('account','search_zip','backfill_raw','backfill_receipt'):
        p.add_argument(name,type=Path)
    a=p.parse_args()
    print(json.dumps(coverage(a.account,a.search_zip,a.backfill_raw,a.backfill_receipt),indent=2))
