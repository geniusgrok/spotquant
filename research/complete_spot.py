"""All registered spot improvement candidates through actual finite sessions."""
import argparse
import bisect
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import tempfile

from research.market import load_daily, file_digest
from research.rebuild import END_MS, START_MS, source_identity
from research.session_account import HistoricalVenue, audit
from research.restore_check import check
from spotquant import session
from spotquant.config import Config
from spotquant.model import DAY, SLEEVES
from spotquant.preview import BASE_STEP, QUOTE_STEP, MIN_NOTIONAL, portfolio
from spotquant.state import State
from spotquant.types import floor_step, serial

CANDIDATES = ('default', 'consensus', 'downside', 'funding', 'basis')
SCENARIOS = ('base', 'fee150', 'slip2', 'outage')


class PriorFX:
    def __init__(self, path):
        rates = json.loads(Path(path).read_text())['rates']
        self.days = sorted(rates)
        self.rates = [D(str(rates[k]['CNY'])) for k in self.days]

    def __call__(self, stamp):
        from datetime import datetime, timezone
        day = datetime.fromtimestamp(stamp / 1000, timezone.utc).date().isoformat()
        i = bisect.bisect_left(self.days, day) - 1
        return self.rates[i] if i >= 0 else D('6.9615')


class Policy:
    def __init__(self, candidate, venue, features):
        self.candidate, self.venue, self.features = candidate, venue, features
        self.state = None
        self.filters = {'blocked': 0, 'missing': 0}

    def feature(self, name):
        lag = 8 * 3600000 if name == 'funding' else 0
        rows = self.features[name]
        i = bisect.bisect_right([r[0] for r in rows], self.venue.now_ms - lag) - 1
        if (i < 0 or self.venue.now_ms - lag - rows[i][0] > (8 * 3600000 if lag else DAY)
                or (name == 'basis' and rows[i][0] < self.venue.now_ms // DAY * DAY)):
            self.filters['missing'] += 1
            return None
        return D(rows[i][1])

    def __call__(self, views, owned, snapshot, **kwargs):
        decision = portfolio(views, owned, snapshot, **kwargs)
        buys = [o for o in decision['orders'] if o['side'] == 'BUY']
        if self.candidate in ('funding', 'basis') and buys:
            value = self.feature(self.candidate)
            limit = D('.0003') if self.candidate == 'funding' else D('.01')
            if value is None or value > limit:
                self.filters['blocked'] += 1
                decision['orders'] = [o for o in decision['orders'] if o['side'] != 'BUY']
                for o in buys:
                    for w in o['sleeves']:
                        decision['sleeves'][str(w)].update(action='flat', order=None)
        voters = sum(bool(views[w].bull) and decision['sleeves'][str(w)]['action'] in ('enter', 'hold')
                     for w in views)
        if self.candidate == 'consensus' and buys and voters >= 2:
            budget = D(snapshot['usdt_free']) * D('.90')
            if kwargs['capital_limit'] is not None:
                budget = min(budget, max(D(0), kwargs['capital_limit'] - D(snapshot['btc']) * D(snapshot['avg_price'])))
            budget = floor_step(budget, QUOTE_STEP)
            if budget >= MIN_NOTIONAL:
                buys[0]['quoteOrderQty'] = str(max(D(buys[0]['quoteOrderQty']), budget))
        if self.candidate == 'downside':
            decision = self.downside(decision, views, owned, snapshot)
        decision['order'] = decision['orders'][0] if decision['orders'] else None
        return decision

    def downside(self, decision, views, owned, snapshot):
        ref = views[40]
        bar = ref.last
        memo = self.state.get('research_downside') or {'phase': 'normal'}
        held = [w for w in SLEEVES if D(owned[w]) > BASE_STEP]
        if memo['phase'] != 'normal':
            decision['orders'] = [o for o in decision['orders'] if o['side'] != 'BUY']
        if any(o['side'] == 'SELL' for o in decision['orders']):
            return decision
        if not held:
            if memo['phase'] == 'normal' or (ref.bull and ref.streak >= 2):
                self.state.set('research_downside', {'phase': 'normal'})
            return decision
        if memo['phase'] == 'reducing' and D(snapshot['btc']) <= D(memo['remaining']) + BASE_STEP * 4:
            memo['phase'] = 'reduced'
        if memo['phase'] == 'restoring' and D(snapshot['btc']) > D(memo['before']) + BASE_STEP:
            memo = {'phase': 'normal', 'last_restore': bar}
        closes = list(ref.closes)
        adverse = len(closes) >= 6 and ref.sma is not None and ref.close < ref.sma and ref.close < closes[-6]
        if memo['phase'] == 'normal' and adverse and memo.get('last_restore') != bar:
            quantity = floor_step(sum((D(owned[w]) for w in held), D(0)) / 2, BASE_STEP)
            if quantity * self.venue.price >= MIN_NOTIONAL:
                memo = {'phase': 'reducing', 'remaining': str(D(snapshot['btc']) - quantity),
                        'quantity': str(quantity), 'group': held, 'signal': bar}
        if memo['phase'] == 'reducing':
            # Stable signal identity prevents repeat reductions within this episode.
            if bar == memo['signal']:
                decision['orders'] = [dict(symbol='BTCUSDT', side='SELL', type='MARKET',
                                           quantity=memo['quantity'], sleeves=memo['group'])]
                for w in memo['group']:
                    decision['sleeves'][str(w)]['action'] = 'exit'
        if memo['phase'] == 'reduced' and ref.bull and ref.streak >= 2:
            budget = floor_step(D(snapshot['usdt_free']), QUOTE_STEP)
            if budget >= MIN_NOTIONAL:
                memo.update(phase='restoring', before=str(snapshot['btc']), quote=str(budget),
                            group=held, signal=bar)
        if memo['phase'] == 'restoring' and bar == memo['signal']:
            decision['orders'] = [dict(symbol='BTCUSDT', side='BUY', type='MARKET',
                                       quoteOrderQty=memo['quote'], sleeves=memo['group'])]
        self.state.set('research_downside', memo)
        return decision


@contextmanager
def configured(policy):
    original_portfolio, original_state = session.portfolio, session.State
    class ResearchState(State):
        def __enter__(self):
            result = super().__enter__()
            policy.state = result
            return result
    session.portfolio, session.State = policy, ResearchState
    try:
        yield
    finally:
        session.portfolio, session.State = original_portfolio, original_state


def measure(candidate, scenario, bars, starts, fx, features, *, limit=None, initial_cny=D(10000)):
    starts = starts[:limit]
    wallet = initial_cny / fx(START_MS) * D('.999')
    venue = HistoricalVenue(bars, START_MS, wallet, fx,
                            fee=D('.0015') if scenario == 'fee150' else D('.001'),
                            slip=D('.001') if scenario == 'slip2' else D('.0005'),
                            stop_slip=D('.002') if scenario == 'slip2' else D('.001'))
    policy = Policy(candidate, venue, features)
    reports, failure = [], None
    with tempfile.TemporaryDirectory(prefix='spot-complete-') as directory, configured(policy):
        config = Config('1', directory, 300, 5, 'demo', '5000000')
        for i, start in enumerate(starts):
            if scenario == 'outage' and 1583020800000 <= start < 1584835200000:
                continue
            venue.advance(start)
            report = session.run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
            archive_proof = check(report['session_archive']['report']) if 'session_archive' in report else None
            reports.append({'start_ms': start, 'status': report['status'], 'cycles': report['cycles'],
                            'errors': report['errors'], 'pending_intents': report['pending_intents'],
                            'elapsed_seconds': report['elapsed_seconds'],
                            'ended_ms': venue.now_ms,
                            'archive_verified': bool(archive_proof and archive_proof['integrity_verified']),
                            'archive_backup_sha256': archive_proof['backup_sha256'] if archive_proof else None})
            reports[-1]['execution_unresolved'] = bool(report['pending_intents']) or (
                report['status'] not in ('offline_execution', 'demo_execution')
                and not (report['errors'] and all('session deadline' in e['reason'] for e in report['errors'])))
            if i % 100 == 0:
                print(json.dumps({'candidate': candidate, 'scenario': scenario, 'session': i}), flush=True)
        end = starts[-1] + 420000 if limit else END_MS
        venue.advance(end)
        with State(directory, config.scope) as state:
            pending = state.pending()
            positions = state.get('positions')
            policy_pending = (state.get('research_downside') or {}).get('phase') in ('reducing', 'restoring')
            allocations = list(state.db.execute('SELECT id,payload,status,result FROM intents ORDER BY updated'))
    money = audit(venue)
    value = (venue.cash + venue.btc * venue.price) * fx(end) * D('.999')
    unresolved = sum(r['execution_unresolved'] for r in reports)
    complete = (limit is None and money['passed'] and not pending and not policy_pending and not unresolved
                and all(r['archive_verified'] for r in reports))
    return serial({'candidate': candidate, 'scenario': scenario, 'complete': complete,
                   'initial_cny': initial_cny, 'final_cny': value,
                   'final_usdt': venue.cash + venue.btc * venue.price,
                   'cagr': (float(value / initial_cny) ** (1 / ((end - START_MS) / (365.25 * DAY))) - 1)
                           if limit is None else None,
                   'mdd': venue.mdd, 'audit': money, 'cash_usdt': venue.cash, 'btc': venue.btc,
                   'fills': venue.fills, 'daily': venue.daily, 'sessions': reports,
                   'client_events': venue.client_events,
                   'session_error_count': sum(bool(r['errors']) for r in reports),
                   'execution_unresolved_sessions': unresolved, 'policy_pending': policy_pending,
                   'filters': policy.filters, 'positions': positions, 'allocations': allocations,
                   'pending_intents': pending, 'native_execution_verified': False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--crowding', type=Path, default=Path('/tmp/btc-complete-inputs/crowding-complete.json'))
    parser.add_argument('--candidate', choices=CANDIDATES)
    parser.add_argument('--scenario', choices=SCENARIOS)
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1,
                        help='isolated research processes; each account retains its own finite sessions')
    args = parser.parse_args(argv)
    source = source_identity()
    if source['dirty'] or args.out.exists() or (args.limit and not str(args.out.resolve()).startswith('/tmp/')):
        parser.error('commit source; choose a new output; partial accounts only in /tmp')
    market_root = Path('/tmp/spotquant-market/klines')
    bars = load_daily(market_root, END_MS, require_through=END_MS)
    schedule_path = Path('../coinquant/research/session_schedule.json').resolve()
    starts = json.loads(schedule_path.read_text())['primary']['starts_ms']
    fx_path = Path('../starquant/data/usdcny_frankfurter.json').resolve()
    features = json.loads(args.crowding.read_text())
    results = {}
    jobs = [(candidate, scenario)
            for candidate in ([args.candidate] if args.candidate else CANDIDATES)
            for scenario in ([args.scenario] if args.scenario else SCENARIOS)]
    def record(candidate, scenario, result):
        results[candidate + '-' + scenario] = result
        progress = args.out.with_suffix('.progress.json')
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(json.dumps({'complete': False, 'source': source,
            'completed': list(results), 'results': results}, separators=(',', ':')) + '\n')
    if args.workers == 1:
        for candidate, scenario in jobs:
            record(candidate, scenario, measure(candidate, scenario, bars, starts, PriorFX(fx_path), features, limit=args.limit))
    else:
        # Separate processes isolate Policy's existing process-local hooks. This
        # changes wall-clock scheduling only; never run accounts in shared threads.
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(measure, c, s, bars, starts, PriorFX(fx_path), features, limit=args.limit): (c, s)
                       for c, s in jobs}
            for future in as_completed(pending):
                record(*pending[future], future.result())
    results = {c + '-' + s: results[c + '-' + s] for c, s in jobs}
    report = {'source': source, 'market_sha256': file_digest(market_root),
              'schedule_sha256': hashlib.sha256(schedule_path.read_bytes()).hexdigest(),
              'fx_sha256': hashlib.sha256(fx_path.read_bytes()).hexdigest(),
              'crowding_sha256': hashlib.sha256(args.crowding.read_bytes()).hexdigest(),
              'results': results, 'native_execution_verified': False,
              'limitations': ['daily OHLC linear high-before-low proxy; no observed intraday tape or queue',
                              'actual bounded session/Lifecycle, fixed published research cap USDT5m',
                              'first protection and cancel-replace are non-atomic',
                              'all previously studied history; no prospective alpha proof']}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        json.dump(report, stream, separators=(',', ':'), allow_nan=False)
        stream.write('\n')
    print(json.dumps({'complete_accounts': sum(r['complete'] for r in results.values()), 'accounts': len(results)}))


if __name__ == '__main__':
    main()
