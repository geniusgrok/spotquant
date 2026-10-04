"""Five-family research sizing through existing Spot ownership and protection."""
from decimal import Decimal as D
from unittest.mock import patch

from research import edge_spot as edge, complete_spot as meter, nine_routes as r
from spotquant import preview


class Policy(edge.Policy):
    def __init__(self,family,expression,venue,features,book,context):
        if family not in ('etf-demand','option-insurance','old-coin-supply'):raise ValueError('unregistered Spot source')
        super().__init__('crowding-interaction',venue,features,risk={'scale':'1','sha256':None})
        self.family,self.expression,self.book,self.context=family,expression,book,context
        self.identity.update(nine_spec=r.sha(r.SPEC.read_bytes()),family=family,
                             expression=expression,feature_book=book.sha256)

    def __call__(self,views,owned,snapshot,**kwargs):
        result=super().__call__(views,owned,snapshot,**kwargs)
        at=self.venue.now_ms;feature=self.book.at(self.family,at,self.context(at))
        for order in list(result['orders']):
            if order['side']!='BUY':continue
            effect=r.alpha_expression(self.family,self.expression,feature,order)
            # Unqualified additional information remains pending; it cannot rewrite old missing-input safeguards.
            if effect.get('factor') is not None:
                edge.resize(result,order,D(order['quoteOrderQty'])*D(effect['factor']))
        views={w:preview.decision_view(v,kwargs['positions'].get(w),kwargs['owners']) for w,v in views.items()}
        result['protections']=preview._merge_protections({w:result['sleeves'][str(w)] for w in views},views,snapshot,next(iter(views.values())))
        result['order']=result['orders'][0] if result['orders'] else None
        self.journal[-1].update(nine_family=self.family,feature_status=feature['status'],accepted_orders=result['orders'])
        return result


def measure(family,expression,bars,starts,fx,features,book,context):
    policies=[]
    def create(_name,venue,_features):
        policy=Policy(family,expression,venue,features,book,context);policies.append(policy);return policy
    with patch.object(meter,'Policy',create),patch.object(meter,'configured',edge.configured):
        row=meter.measure('crowding-interaction','base',bars,starts,fx,{})
    row.update(research_identity=policies[0].identity,opportunity_ledger=policies[0].journal,
               finite_alpha_research=True,native_execution_verified=False)
    return row
