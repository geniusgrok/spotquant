"""One registered 2018/2019 and common-window screen; no session replay."""
import bisect
import argparse
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT = Path('/workspace/btc-history-2018-2019-20261005')
COIN = Path('/workspace/coinquant')
SPOT = Path(__file__).resolve().parents[1]
DAY = 86400000


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def serial(value):
    if isinstance(value, D):
        return str(value)
    if isinstance(value, dict):
        return {str(k):serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    return value


def run(kind, mode, bars, signals, funding, begin, end, Wallet, stress=False, short_only=False):
    wallet = Wallet(10000, kind == 'coin', stress)
    quantities, stops, peaks = {}, {}, {}
    previous_direction, epoch, consumed = 0, 0, None
    campaigns = short_campaigns = 0
    last_close = None
    daily, actors = [], []
    times = [r[0] for r in funding]
    for t, o, h, l, c in bars:
        if not begin <= t < end:
            continue
        last_close = c
        # Coarse next-open quote, with a fixed modeled 60s publication delay.
        # The first minute price is not observed; actual accounts resolve it.
        feature = signals.at(t+60000) if signals else None
        wallet.mark(t, o)
        blocked = set()
        if kind == 'coin':
            direction = feature['direction'] if feature else 0
            if short_only and direction > 0:
                direction = 0
            if direction != previous_direction:
                epoch += 1
                previous_direction = direction
            if wallet.q and wallet.stop is not None and (o <= wallet.stop if wallet.q > 0 else o >= wallet.stop):
                wallet.resize(D(0), o)
                consumed = epoch
            fraction = min(D(1), feature['fraction']) if begin < 1577836800000 else feature['fraction']
            desired = direction*max(D(0), wallet.equity(o))*fraction/o
            trail = feature['trail']
            desired = (1 if desired > 0 else -1)*min(abs(desired), max(D(0), wallet.cash)/(o*(trail+D('.12')))) if desired else D(0)
            if wallet.q*desired < 0:
                wallet.resize(D(0), o)
            if not wallet.q and desired and consumed == epoch:
                desired = D(0)
            if abs(desired-wallet.q)*o >= max(D(5), max(D(0), wallet.equity(o))*D('.05')) or not desired:
                fresh = not wallet.q and bool(desired)
                wallet.resize(desired, o)
                if fresh:
                    campaigns += 1
                    short_campaigns += int(desired < 0)
                    wallet.extreme = o
                    wallet.stop = o*(1-trail if desired > 0 else 1+trail)
            if wallet.q and begin >= 1577836800000:
                for settlement, available, rate in funding[bisect.bisect_left(times, t):bisect.bisect_left(times, t+DAY)]:
                    cost = wallet.q*o*rate
                    wallet.cash -= cost
                    wallet.funding += cost
            if wallet.q and wallet.stop is not None:
                breached = l <= wallet.stop if wallet.q > 0 else h >= wallet.stop
                if breached:
                    stop = min(o, wallet.stop) if wallet.q > 0 else max(o, wallet.stop)
                    wallet.mark(t+1, stop)
                    wallet.resize(D(0), stop)
                    consumed = epoch
                else:
                    wallet.mark(t+1, h if wallet.q > 0 else l)
                    wallet.mark(t+2, l if wallet.q > 0 else h)
                    wallet.extreme = max(wallet.extreme, h) if wallet.q > 0 else min(wallet.extreme, l)
                    proposed = wallet.extreme*(1-trail if wallet.q > 0 else 1+trail)
                    wallet.stop = max(wallet.stop, proposed) if wallet.q > 0 else min(wallet.stop, proposed)
        else:
            fractions = ({30:D('.25'), 40:D(0), 50:D(0)} if mode == 'constant25' else
                         {30:D('.90'), 40:D(0), 50:D(0)} if mode == 'constant90' else
                         {30:D(0), 40:feature['components'][40], 50:D(0)} if mode == 'tactical' else feature['components'])
            trail = feature['trail'] if feature else D('.30')
            for w in list(quantities):
                if quantities[w] and o <= stops[w]:
                    wallet.resize(wallet.q-quantities[w], o)
                    quantities[w] = D(0)
                    blocked.add(w)
            capital = max(D(0), wallet.equity(o))
            targets = {w:capital*fraction/o for w, fraction in fractions.items()}
            order = sorted(targets, key=lambda w:targets[w]-quantities.get(w, D(0)))
            for w in order:
                old = quantities.get(w, D(0))
                target = targets[w]
                delta = target-old
                if w in blocked or abs(delta)*o < max(D(5), capital*D('.05')) and target:
                    continue
                if delta > 0:
                    delta = min(delta, max(D(0), wallet.cash)/(o*(1+wallet.slip)*(1+wallet.fee)))
                wallet.resize(wallet.q+delta, o)
                quantities[w] = old+delta
                if not old and quantities[w]:
                    peaks[w], stops[w] = o, o*(1-trail)
                    campaigns += 1
                elif not quantities[w]:
                    stops.pop(w, None)
                    peaks.pop(w, None)
            wallet.mark(t+1, h)
            for w in sorted(stops, key=lambda w:stops[w], reverse=True):
                if quantities[w] and l <= stops[w]:
                    wallet.mark(t+2, stops[w])
                    wallet.resize(wallet.q-quantities[w], min(o, stops[w]))
                    quantities[w] = D(0)
                elif quantities[w]:
                    peaks[w] = max(peaks[w], h)
                    stops[w] = max(stops[w], peaks[w]*(1-trail))
            wallet.mark(t+3, l)
            if abs(sum(quantities.values(), D(0))-wallet.q) > D('1e-18'):
                raise ValueError('coarse component fill ownership imbalance')
            if wallet.cash < -D('1e-10'):
                raise ValueError('unfunded shared spot pool')
        wallet.mark(t+DAY, c)
        daily.append((t+DAY, wallet.equity(c)))
        if wallet.closed:
            break
    if last_close is None:
        raise ValueError('registered period has no complete data')
    terminal_position = wallet.q
    wallet.resize(D(0), last_close)
    return dict(net_return=wallet.cash/wallet.initial-1, final_wealth=wallet.cash,
                mdd_path_proxy=wallet.mdd, fees=wallet.fees, funding_paid=wallet.funding,
                fills=wallet.fills, campaigns=campaigns, short_campaigns=short_campaigns,
                terminal_price=last_close, terminal_position_before_close=terminal_position,
                insolvent=wallet.closed, daily=daily,
                interpretation='independent daily quote wallet proxy;2018/2019 Coin unlevered direction study without contract funding/liquidation;2020+ settled rates approximate daily-open notional; OHLC path and publication-minute fills not native')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover-terminal', action='store_true')
    args = parser.parse_args()
    started = time.monotonic()
    if (ROOT/'screen.json').exists() and not args.recover_terminal:
        raise ValueError('retain original fixed results')
    raw = (ROOT/'days.json').read_bytes()
    receipt = json.loads((ROOT/'data-receipt.json').read_text())
    if hashlib.sha256(raw).hexdigest() != receipt['parsed_sha256']:
        raise ValueError('qualified input changed')
    packet = json.loads(raw)
    bars = [(int(t), *[D(row[k]) for k in ('open','high','low','close')]) for t, row in sorted(packet['bars'].items(), key=lambda item:int(item[0]))]
    sys.path.insert(0, str(COIN))
    meter = module(COIN/'research/core_screen.py', 'history_wallet_meter')
    coin = module(COIN/'research/history_core.py', 'signed_history_core')
    spot = module(SPOT/'research/history_core.py', 'spot_history_core')
    from research.edge_features import FeatureBook
    funding_path = COIN/'evidence/btc-edge-20261003/task-artifacts/edge-features-v1.json'
    book = FeatureBook(funding_path, 'bf920626cc13653b8bbeafd171650cc1a76888e15aad0c20bbd1061fb41da512')
    funding = [(a,b,v) for a,b,v,cause in book.records['funding'] if cause is None]
    spec_raw = (ROOT/'spec.json').read_bytes()
    spec = json.loads(spec_raw)
    eras = [(meter.stamp(a), meter.stamp(b)) for a,b in spec['screen']['eras']]
    signals = {kind+':'+mode:mod.Signals(bars, mode, receipt['parsed_sha256']) for kind,mod in [('coin',coin),('spot',spot)] for mode in mod.MODES}
    results = {}
    original = json.loads((ROOT/'screen-original.json').read_text()) if args.recover_terminal else None
    reused, recovered = [], []

    def measure(kind, mode, signal, a, b, stress=False, short_only=False, index=0):
        label = 'new_short_direction_only' if short_only else 'stress' if stress else 'base'
        key = kind+':'+mode
        if original is not None:
            prior = original['results'][key][label][index]
            if b == eras[-1][1] or D(prior['final_wealth']) == D(prior['daily'][-1][1]):
                reused.append(dict(case=key, label=label, era_index=index,
                                   row_sha256=hashlib.sha256(json.dumps(prior,sort_keys=True).encode()).hexdigest()))
                def decimals(value):
                    if isinstance(value, dict):return {k:decimals(v) for k,v in value.items()}
                    if isinstance(value, list):return [decimals(v) for v in value]
                    if isinstance(value,str):
                        try:return D(value)
                        except Exception:return value
                    return value
                return decimals(prior)
            recovered.append(dict(case=key,label=label,era_index=index))
        return run(kind,mode,bars,signal,[] if short_only else funding,a,b,meter.Wallet,stress,short_only)
    for kind, modes in [('coin',coin.MODES), ('spot',(*spot.MODES,'constant25','constant90','tactical'))]:
        for mode in modes:
            signal = signals.get(kind+':'+mode, signals['spot:base'])
            rows = [measure(kind,mode,signal,a,b,index=i) for i,(a,b) in enumerate(eras)]
            stress = [measure(kind,mode,signal,a,b,True,index=i) for i,(a,b) in enumerate(eras)]
            results[kind+':'+mode] = dict(base=rows, stress=stress)
            if kind == 'coin':
                results[kind+':'+mode]['new_short_direction_only'] = [measure(kind,mode,signal,a,b,short_only=True,index=i) for i,(a,b) in enumerate(eras[:2])]
    for kind, modes in [('coin',coin.MODES), ('spot',spot.MODES)]:
        for mode in modes:
            result = results[kind+':'+mode]
            rows, stress = result['base'], result['stress']
            if kind == 'coin':
                shorts = result['new_short_direction_only']
                gates = dict(new_short_combined_net=(1+shorts[0]['net_return'])*(1+shorts[1]['net_return'])>1,
                             short_support_each_new_year=all(r['short_campaigns']>=3 for r in shorts),
                             new_period_stress_net=(1+stress[0]['net_return'])*(1+stress[1]['net_return'])>1,
                             common_positive_two_eras=sum(r['net_return']>0 for r in rows[2:])>=2,
                             common_stress_positive_two_eras=sum(r['net_return']>0 for r in stress[2:])>=2,
                             solvent=all(not r['insolvent'] for r in rows+stress))
            else:
                controls = results['spot:constant90']['base']
                control25 = results['spot:constant25']['base']
                benchmark = {t+DAY:float(c) for t,o,h,l,c in bars}
                risk = module(COIN/'research/core_report.py','saved_history_risk').risk
                captures = [risk({t:float(v) for t,v in r['daily']},benchmark) for r in [rows[1],controls[1]]]
                up = captures[0].get('up_capture')
                control_up = captures[1].get('up_capture')
                gates = dict(bear_mdd=rows[0]['mdd_path_proxy']<=controls[0]['mdd_path_proxy']*D('.8'),
                             bear_wealth=rows[0]['final_wealth']>=controls[0]['final_wealth'],
                             recovery_net=rows[1]['net_return']>0,
                             recovery_up_capture=up is not None and control_up is not None and up>=.5*control_up,
                             common_positive_each_era=all(r['net_return']>0 for r in rows[2:]),
                             common_stress_positive_each_era=all(r['net_return']>0 for r in stress[2:]),
                             common_beats25_two_eras=sum(r['final_wealth']>=c['final_wealth'] for r,c in zip(rows[2:],control25[2:]))>=2,
                             solvent=all(not r['insolvent'] for r in rows+stress))
                result['recovery_capture'] = captures
            result['gates'] = gates
            result['status'] = 'COARSE_SCREEN_ENTRANT' if all(gates.values()) else 'REJECT_FIXED_CORE' if gates.get('short_support_each_new_year',True) else 'SUPPORT_PENDING'
            result['common_compound_wealth_ratio'] = (1+rows[2]['net_return'])*(1+rows[3]['net_return'])*(1+rows[4]['net_return'])
    selected = {}
    for kind, modes in [('coin',coin.MODES), ('spot',spot.MODES)]:
        entrants = [(results[kind+':'+mode]['common_compound_wealth_ratio'],mode) for mode in modes if results[kind+':'+mode]['status']=='COARSE_SCREEN_ENTRANT']
        selected[kind] = max(entrants)[1] if entrants else None
    output = dict(format='btc-history-core-screen-v1', results=results, selected=selected,
                  qualified_daily_sha256=receipt['parsed_sha256'], spec_sha256=hashlib.sha256(spec_raw).hexdigest(),
                  producer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  coin_core_sha256=hashlib.sha256((COIN/'research/history_core.py').read_bytes()).hexdigest(),
                  spot_core_sha256=hashlib.sha256((SPOT/'research/history_core.py').read_bytes()).hexdigest(),
                  original_funding_sha256=book.sha256, elapsed_seconds=time.monotonic()-started,
                  new_actual_accounts=0,new_actual_sessions=0,new795=0, economic_adoption=False,
                  limits='Repeated development common history; new2018 and cached2019 not untouched future OOS; all cases independent coarse wallets, not manual-session accounts or prospective alpha.')
    output['terminal_recovery'] = dict(original_preserved=bool(original), recovered=recovered,
                                      reused=reused, original_sha256=hashlib.sha256((ROOT/'screen-original.json').read_bytes()).hexdigest() if original else None)
    (ROOT/'screen.json').write_text(json.dumps(serial(output),indent=2)+'\n')
    print(json.dumps(serial(dict(selected=selected,results={k:{'status':v.get('status'),'gates':v.get('gates'),'returns':[r['net_return'] for r in v['base']],'mdd':[r['mdd_path_proxy'] for r in v['base']]} for k,v in results.items()},elapsed_seconds=output['elapsed_seconds'])),indent=2))


if __name__ == '__main__':
    main()
