"""Research hypotheses through existing pooled Spot session and Lifecycle."""
from copy import copy
from decimal import Decimal as D
from unittest.mock import patch

from research import complete_spot as meter, edge_spot as edge, persistent_routes as routes
from spotquant import preview


class Policy(edge.Policy):
    def __init__(self,name,venue,features,bars):
        super().__init__('crowding-interaction',venue,features,
                         risk={'scale':'.75' if name=='uniform' else '1','sha256':None})
        self.name,self.bars=name,bars
        self.identity.update(persistent_name=name,persistent_spec_sha256=routes.f.sha(routes.SPEC.read_bytes()))
        self.changed=set()

    def __call__(self,views,owned,snapshot,**kwargs):
        working={w:copy(v) for w,v in views.items()}
        positions=kwargs['positions']
        if self.name in ('state-trend','state-range'):
            for w,v in working.items():
                if D(owned[w]) or v.last is None:continue
                new=bool(routes.signal(self.bars,v.last,self.name,'spot')) and v.crash_ok and not v.extended
                if new and not (v.enter or v.cap_enter):self.changed.add(('BUY',w,v.last))
                v.enter=new
                # Retain the existing separate capitulation and all held exits.
        original=preview._position_decision
        def position(v,snap,quantity,price):
            base=original(v,snap,quantity,price)
            if self.name not in ('repair-stall','repair-rebreak') or not v.repair or base['action']!='hold':return base
            p=positions[v.sma_window]
            stall=v.last+routes.f.DAY>=p['first_ms']+14*routes.f.DAY and v.close<=D(p['entry_fill'])
            keys=range(v.last-20*routes.f.DAY,v.last,routes.f.DAY)
            rebreak=all(t in self.bars for t in keys) and v.close<min(self.bars[t][2] for t in keys)
            if stall if self.name=='repair-stall' else rebreak:
                self.changed.add(('SELL',v.sma_window,v.last))
                return preview._exit(v,quantity,'persistent research '+self.name)
            return base
        with patch.object(preview,'_position_decision',position):
            result=super().__call__(working,owned,snapshot,**kwargs)
        if self.name in ('entry-chase','entry-chase-soft'):
            follows=self.state.get('follows') or {}
            for order in list(result['orders']):
                if order['side']!='BUY':continue
                day=min((follows.get(str(w)) or {}).get('signal_ms',views[w].last) for w in order['sleeves'])
                keys=list(range(day-13*routes.f.DAY,day+routes.f.DAY,routes.f.DAY))
                if not all(t in self.bars and t-routes.f.DAY in self.bars for t in keys):continue
                atr=sum(max(self.bars[t][1]-self.bars[t][2],abs(self.bars[t][1]-self.bars[t-routes.f.DAY][3]),
                            abs(self.bars[t][2]-self.bars[t-routes.f.DAY][3])) for t in keys)/14
                if D(snapshot['avg_price'])>self.bars[day][3]+atr:
                    factor=D('.5') if self.name=='entry-chase-soft' else D(0)
                    for w in order['sleeves']:self.changed.add(('BUY',w,views[w].last))
                    edge.resize(result,order,D(order['quoteOrderQty'])*factor)
            protection_views={w:preview.decision_view(v,positions.get(w),kwargs['owners']) for w,v in working.items()}
            result['protections']=preview._merge_protections(
                {w:result['sleeves'][str(w)] for w in working},protection_views,snapshot,next(iter(protection_views.values())))
        result['order']=result['orders'][0] if result['orders'] else None
        self.journal[-1].update(persistent_name=self.name,accepted_orders=result['orders'])
        return result


def measure(name,bars,starts,fx,features,causal_bars):
    policies=[]
    def create(_candidate,venue,_features):
        policy=Policy(name,venue,features,causal_bars);policies.append(policy);return policy
    with patch.object(meter,'Policy',create),patch.object(meter,'configured',edge.configured):
        row=meter.measure('crowding-interaction','base',bars,starts,fx,{})
    policy=policies[0]
    owners=edge.execution.allocation_owners((edge.json.loads(a[1]),edge.json.loads(a[3])) for a in row['allocations'])
    treatments=0
    for fill in row['fills']:
        owner=owners.get(str(fill['order_id']))
        if owner and any((owner['order']['side'],w,owner['signal_ms']) in policy.changed for w in owner['sleeves']):treatments+=1
    row.update(candidate=name,research_identity=policy.identity,opportunity_ledger=policy.journal,
               treatment_fill_events=treatments,changed_decision_signals=len(policy.changed),
               window_account_finished=row.pop('complete'),complete=False,cagr=None,
               meaning='Independent cold finite shared-session research wallet, not original full economic or native qualification')
    return row
