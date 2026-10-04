"""Fixed core/tactical candidate, reusing actual fill-owned subpool accounting."""
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

from research import alpha_spot as alpha, complete_spot as complete
from research.edge_spot import resize
from spotquant.crowding import evaluate

SPEC = Path(__file__).with_name('structure-spec.json')


class Policy(alpha.Policy):
    def __init__(self, unused, venue, features):
        super().__init__('combo', venue, combo=('atr-stop', 'core-slow'))
        self.features = features
        self.identity.update(candidate='core-tactical',
            structure_spec_sha256=hashlib.sha256(SPEC.read_bytes()).hexdigest(),
            feature_sha256=features.sha256)

    def __call__(self, views, owned, snapshot, **kwargs):
        decision = super().__call__(views, owned, snapshot, **kwargs)
        # Core owns its existing SMA200/28% exit policy and 20% initial cash.
        # Only the tactical pool follows the current causal NEW BUY filter.
        removed = False
        for order in list(decision['orders']):
            if order['side'] != 'BUY' or order['sleeves'] == [200]:
                continue
            factor, diagnostic = evaluate(self.features, views[30], self.venue.now_ms)
            quote = resize(decision, order, D(order['quoteOrderQty']) * factor)
            if quote == 0:
                removed = True
                self.filters['blocked'] += 1
            if diagnostic['blocked_reason']:
                self.filters['missing'] += 1
            self.journal.append(dict(event='tactical-crowding', **diagnostic,
                                     resulting_quote=str(quote)))
        if removed:
            # Remove cancelled entry protection while preserving held coins and
            # keeping core protection ownership separate from the tactical pool.
            tactical = {w: views[w] for w in alpha.TACTICAL}
            decision['protections'] = alpha._merge_protections(
                {w: decision['sleeves'][str(w)] for w in tactical}, tactical, snapshot, views[30]) + [
                    p for p in decision['protections'] if p['sleeves'] == [200]]
        decision['order'] = decision['orders'][0] if decision['orders'] else None
        return decision


def measure(bars, starts, fx, features):
    old = complete.Policy, complete.configured
    policies = []
    def create(*args):
        policy = Policy(*args)
        policies.append(policy)
        return policy
    try:
        complete.Policy, complete.configured = create, alpha.configured
        row = complete.measure('core-tactical', 'base', bars, starts, fx, features)
    finally:
        complete.Policy, complete.configured = old
    row.update(research_identity=policies[0].identity,
               opportunity_ledger=policies[0].journal)
    owners = alpha.execution.allocation_owners(
        (json.loads(r[1]), json.loads(r[3])) for r in row['allocations'])
    fills = [dict(f, **{k: D(f[k]) for k in ('qty', 'quote', 'price', 'commission')})
             for f in row['fills']]
    row['subpools'] = alpha.serial(alpha.subpools(
        {'cash': D(10000) / fx(complete.START_MS) * D('.999'), 'btc': '0'},
        fills, owners, D('.20'),
        {'usdt_free': row['cash_usdt'], 'usdt_locked': '0', 'btc': row['btc']}))
    return row
