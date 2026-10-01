"""P4 preview -> simulated fill -> existing sleeve attribution -> durable stop."""
import argparse
import io
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from research.rebuild import source_identity
from spotquant.follow import apply_day, normalize_trade
from spotquant.model import DAY, ORIGIN, SLEEVES, Model
from spotquant.offline import Lifecycle, OfflineVenue
from spotquant.preview import portfolio
from spotquant.session import _view
from spotquant.state import State
from spotquant.types import serial


def replay():
    # Synthetic contiguous bars arm the real Model; this tests execution only.
    models = {window: Model(window) for window in SLEEVES}
    signal = ORIGIN + 402 * DAY
    prices = [D(100)] * 400 + [D(98), D(101), D(102), D(103)]
    history = [(ORIGIN + day * DAY, price, price, price) for day, price in enumerate(prices)]
    for model in models.values():
        for bar in history[:-1]:
            model.update(*bar)
    venue = OfflineVenue(price='103')
    venue.lose_ack, venue.fraction = True, D('.5')
    snapshot = {'btc': D(0), 'usdt_free': venue.cash, 'usdt_locked': D(0), 'open_orders': 0}
    preview = portfolio(models, {window: D(0) for window in SLEEVES}, snapshot,
                        entries_enabled=True, capital_limit=None)
    with tempfile.TemporaryDirectory() as directory:
        with State(directory, 'offline:BTCUSDT:spot:1') as state:
            order = Lifecycle(state, venue).submit(preview['orders'][0], signal)
        # Reopen the actual SQLite state before repeating the same order identity.
        with State(directory, 'offline:BTCUSDT:spot:1') as state:
            lifecycle = Lifecycle(state, venue)
            lifecycle.submit(preview['orders'][0], signal)
            spent = D(order['quote'])
            gross = spent / venue.price
            trade = normalize_trade({'id': 1, 'orderId': 1, 'time': signal + DAY + 1,
                                     'qty': str(gross), 'quoteQty': str(spent), 'price': str(venue.price),
                                     'commission': str(gross - venue.btc), 'commissionAsset': 'BTC', 'isBuyer': True})
            positions, _, _, _ = apply_day(
                models, {window: None for window in SLEEVES},
                {window: {'signal_ms': signal, 'repair': False} for window in SLEEVES},
                set(), signal + DAY, [trade], lambda: history)
            views, owned = {}, {}
            for window in SLEEVES:
                views[window], owned[window] = _view(models[window], positions[window])
            snapshot.update(btc=venue.btc, usdt_free=venue.cash)
            hold = portfolio(views, owned, snapshot, entries_enabled=True, capital_limit=None)
            stop = lifecycle.submit(hold['protections'][0], signal + DAY)
        venue.trigger('70')  # Process is closed. Only simulated venue protection acts.
        with State(directory, 'offline:BTCUSDT:spot:1') as state:
            Lifecycle(state, venue)
            intents = [{'id': row[0], 'status': row[1], 'result': json.loads(row[2])}
                       for row in state.db.execute('SELECT id,status,result FROM intents ORDER BY updated')]
        return serial({'synthetic': True, 'entry_preview': preview, 'positions': positions,
                       'hold_preview': hold, 'stop': stop, 'venue_events': venue.events,
                       'submissions': venue.sent, 'durable_intents': intents, 'balances': venue.balances(),
                       'limitations': ['terminal partial fill is an IOC-like offline assumption',
                                       'stop locking/amendment and native stop triggering are unverified',
                                       'the daily-open economic meter is not this replay'],
                       'native_qualification': 'NOT_QUALIFIED'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    source = source_identity()
    if args.out.exists() or source['dirty']:
        parser.error('commit source and choose a new evidence file')
    output = io.StringIO()
    tests = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name)
                               for name in ('tests.test_offline', 'tests.test_p4_execution'))
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(tests)
    report = {'source': source, 'offline_passed': result.wasSuccessful() and not result.skipped,
              'tests_run': result.testsRun, 'test_log': output.getvalue(), 'replay': session_replay(),
              'native_execution_verified': False}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'offline_passed': report['offline_passed'], 'tests_run': result.testsRun,
                      'submissions': len(report['replay']['submissions'])}))
    return 0 if report['offline_passed'] else 2


def session_replay():
    from spotquant.config import Config
    from spotquant.offline import P4Venue
    from spotquant.session import run
    prices = [D(100)] * 400 + [D(98)]
    venue = P4Venue([(ORIGIN + i * DAY, p, p, p) for i, p in enumerate(prices)])
    reports = []
    with tempfile.TemporaryDirectory() as directory:
        config = Config('1', directory, 1, 1, 'demo', '1000')
        def invoke():
            reports.append(run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait))
            if reports[-1]['errors']:
                raise ValueError('P4 session replay did not complete')
        invoke()
        for close, high in (('101', '101'), ('102', '102'), ('103', '110')):
            price = D(close)
            venue.bars.append((venue.bars[-1][0] + DAY, D(high), price, price))
            venue.now_ms = venue.bars[-1][0] + DAY
            venue.price = price
            if close == '102':
                venue.fraction, venue.lose_ack = D('.5'), True
            invoke()
        venue.now_ms += 2000
        venue.trigger('70')
        invoke()
        with State(directory, config.scope) as state:
            from spotquant.execution import Lifecycle
            orders = Lifecycle(state, venue, config).rows()
            positions = state.get('positions')
    return serial({'synthetic': True, 'entrypoint': 'spotquant.session.run/cycle → spotquant.execution.Lifecycle',
                   'sessions': reports, 'durable_allocations': orders, 'fills': venue.fills,
                   'positions': positions, 'cash': venue.cash, 'btc': venue.btc,
                   'limitations': ['terminal partial market fill and lock semantics are offline assumptions',
                                   'cancel/replacement gaps still require native Demo measurement'],
                   'native_execution_verified': False})


if __name__ == '__main__':
    raise SystemExit(main())
