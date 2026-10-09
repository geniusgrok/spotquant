"""A bounded session previews and does not record an order intent."""
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from spotquant.config import Config
from spotquant.model import DAY, ORIGIN, Model
from spotquant.session import KNOWN_OLD_RULE, cycle, run
from spotquant.state import State, client_id
from spotquant.types import Blocked, Unknown


class Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return 0 if self.n < 6 else 10


class Venue:
    def crowding_features(self):
        from venue_fixture import KnownFeatures
        return KnownFeatures()

    def __init__(self, bars):
        self.bars = list(bars)
        self.environment = 'live'
        self.capital_limit = None
        self.orders_sent = 0
        self.trade_rows = []

    def clock(self):
        # The observation clock and completed candles share one historical date.
        return (self.bars[-1][0] + DAY + 60_000) / 1000

    def snapshot(self, uid):
        return {
            'account_uid': uid,
            'btc': D(0),
            'usdt_free': D('1000'),
            'usdt_locked': D(0),
            'open_orders': 0,
            'environment': 'live', 'avg_price': self.bars[-1][-1],
        }

    def completed_daily(self, after):
        return [(open_ms, close, high, low, close) for open_ms, high, low, close in self.bars
                if after is None or open_ms > after]

    def daily_open(self, open_ms):
        return open_ms, getattr(self, 'open_price', self.bars[-1][3])

    def trades(self, since):
        return [row for row in self.trade_rows if row['time'] >= since]

    def submit(self, *args, **kwargs):
        self.orders_sent += 1
        raise AssertionError('session must not place orders')


def bars(count, close):
    return [(ORIGIN + index * DAY, D(close), D(close), D(close)) for index in range(count)]


class SessionTests(unittest.TestCase):
    def test_cold_start_does_not_adopt_a_long_shadow_until_a_fresh_cross(self):
        venue = Venue(bars(400, 100))
        for price in (110, 111, 112):
            venue.bars.append((venue.bars[-1][0] + DAY, D(price), D(price), D(price)))
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            first = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(first['model_preview']['action'], 'flat')
            with State(directory, config.scope) as state:
                self.assertTrue(state.get('models')['40']['body']['shadow_in'])
            venue.bars.append((venue.bars[-1][0] + DAY, D(113), D(113), D(113)))
            for _ in range(2):
                held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
                self.assertEqual(held['model_preview']['action'], 'flat')
                self.assertEqual(held['pending_intents'], 0)
            for price in (80, 120):
                venue.bars.append((venue.bars[-1][0] + DAY, D(price), D(price), D(price)))
                held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(held['model_preview']['action'], 'enter')
            self.assertEqual(venue.orders_sent, 0)

    def test_cold_start_then_one_bullish_close_previews_entry(self):
        venue = Venue(bars(252, 100))
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            first = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(first['status'], 'read_only')
            self.assertEqual(first['model_preview']['action'], 'flat')
            self.assertEqual(first['write_attempted'], False)
            self.assertEqual(first['pending_intents'], 0)
            self.assertGreaterEqual(first['cycles'], 1)
            venue.bars.append((ORIGIN + 252 * DAY, D(200), D(180), D(200)))
            second = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(second['model_preview']['action'], 'enter')
            venue.bars.append((ORIGIN + 253 * DAY, D(210), D(190), D(210)))
            third = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(third['model_preview']['action'], 'enter')
        self.assertEqual(third['model_preview']['order']['side'], 'BUY')
        self.assertEqual(third['model_preview']['order']['sleeves'], [40])
        self.assertEqual(third['model_preview']['order']['quoteOrderQty'], '1000.00')
        self.assertEqual(venue.orders_sent, 0)

    def test_a_followed_buy_previews_the_fill_stop_and_a_failed_cycle_drops_the_old_view(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._held_venue(directory)
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(held['status'], 'read_only')
            self.assertTrue(held['followed_position'])
            self.assertEqual(held['followed_sleeves'], [40])
            self.assertEqual(held['model_preview']['action'], 'hold')
            # The 28% trail uses the 111 fill as the peak, not the pre-fill wick.
            self.assertEqual(held['model_preview']['protections'][0]['stopPrice'], '79.92')
            self.assertEqual(held['model_preview']['protections'][0]['sleeves'], [40])
            # The current daily open has now joined the shadow book.
            self.assertIn('stop 28%', held['model_preview']['sleeves']['40']['reason'])
            observed = venue.snapshot
            venue.snapshot = lambda uid: dict(observed(uid), btc=D(1))
            external = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(external['status'], 'unknown')
            self.assertNotIn('model_preview', external)
            venue.snapshot = lambda uid: (_ for _ in ()).throw(Unknown('feed broke'))
            failed = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(failed['status'], 'unknown')
        self.assertNotIn('model_bull', failed)
        self.assertNotIn('model_preview', failed)
        self.assertNotIn('followed_position', failed)
        self.assertNotIn('followed_sleeves', failed)
        self.assertFalse(failed['recorded_limits']['adverse_loss_capped'])

    def _held_venue(self, directory):
        venue = Venue(bars(252, 100))
        config = Config('10001', directory, session_seconds=2, poll_seconds=1)
        run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        venue.bars.append((ORIGIN + 252 * DAY, D(110), D(100), D(110)))
        run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        venue.bars.append((ORIGIN + 253 * DAY, D(111), D(100), D(111)))
        run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        venue.snapshot = lambda uid: {
            'account_uid': uid, 'btc': D('0.6'), 'usdt_free': D('0'),
            'usdt_locked': D(0), 'open_orders': 0, 'environment': 'live', 'avg_price': venue.bars[-1][-1],
        }
        venue.trade_rows = [{
            'id': 1, 'time': ORIGIN + 254 * DAY + 60_000, 'qty': D('0.6'), 'quote': D('66.6'),
            'buyer': True, 'order_id': 1, 'commission': D('0'), 'commission_asset': 'BNB',
        }]
        held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertTrue(held['followed_position'])
        return config, venue

    def test_a_failed_preview_still_keeps_the_positions_in_step_with_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._held_venue(directory)
            healthy = venue.snapshot
            venue.snapshot = lambda uid: (_ for _ in ()).throw(Unknown('feed broke'))
            venue.bars.append((ORIGIN + 254 * DAY, D(111), D(111), D(111)))
            venue.bars.append((ORIGIN + 255 * DAY, D(150), D(111), D(150)))
            failed = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            self.assertEqual(failed['status'], 'unknown')
            venue.snapshot = healthy
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(held['status'], 'read_only')
        # The 150 peak is caught up after the failed cycle; the stop is 28% under it.
        self.assertEqual(held['model_preview']['protections'][0]['stopPrice'], '108.00')
        self.assertEqual(held['model_preview']['protections'][0]['sleeves'], [40])

    def test_an_old_checkpoint_is_rejected_even_when_flat(self):
        with tempfile.TemporaryDirectory() as directory:
            venue = Venue(bars(252, 100))
            config = Config('10001', directory, session_seconds=2, poll_seconds=1)
            run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
            from spotquant.state import State
            with State(config.state_dir, config.scope) as state:
                state.set('rule', 'older')
            held = run(config, venue, monotonic=Clock(), wait=lambda _seconds: None)
        self.assertEqual(held['status'], 'blocked')
        self.assertIn('another rule', held['reason'])
        self.assertNotIn('model_preview', held)


class LegacyRecoveryTests(unittest.TestCase):
    def test_actual_main_three_sleeve_v5_is_blocked_before_native_readback_and_keeps_state(self):
        from spotquant.crowding import RULE
        from venue_fixture import TestVenue
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'main-v5-three-sleeve-checkpoint.json').read_text())
        self.assertEqual(fixture['source_sha'], '314c57c5f8eb3da1eb4bca1202115cb6847aac13')
        self.assertEqual(set(fixture['models']), {'30', '40', '50'})
        parameters = dict(version=5, trail='0.28', confirm=2, crash='0.50', high_window=252,
                          fresh=True, extend='0.61', cap_drop='0.11', cap_bounce='0.07',
                          cap_depth='0.50', cap_hand='0.11', cap_window=400, adverse_stop='0.04')
        for key, checkpoint in fixture['models'].items():
            self.assertEqual(checkpoint, self.rehash(checkpoint['body']))
            self.assertEqual(checkpoint['body']['sma_window'], int(key))
            self.assertEqual({field: checkpoint['body'][field] for field in parameters}, parameters)
        last = fixture['models']['40']['body']['last']
        position = dict(qty='0.3', entry_fill='102', first_ms=last + DAY + 60_000,
                        entry_open_ms=last + DAY, peak='102', repair=False,
                        repair_peak=None, adverse=False, through=None, protection='resting')
        order = dict(symbol='BTCUSDT', side='SELL', type='STOP_LOSS', quantity='0.9', stopPrice='73.44')
        payload = dict(order=order, sleeves=[30, 40, 50], weights={key: '0.3' for key in fixture['models']},
                       signal_ms=last, repair={key: False for key in fixture['models']})
        venue = TestVenue(bars(254, 100))
        venue.now_ms += 120_000
        venue.btc = D('0.9')
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            operation = 'SELL-STOP_LOSS-30,40,50-' + hashlib.sha256(json.dumps(order, sort_keys=True).encode()).hexdigest()[:16]
            identity = client_id(config.scope, last, operation)
            venue.submit(identity, order)
            venue.query = Mock(side_effect=AssertionError('main v5 must not query'))
            venue.submit = Mock(side_effect=AssertionError('main v5 must not submit'))
            venue.cancel = Mock(side_effect=AssertionError('main v5 must not cancel'))
            with State(directory, config.scope) as state:
                state.set_many(dict(rule=RULE, models=fixture['models'],
                                    positions={key: position for key in fixture['models']},
                                    follows={key: None for key in fixture['models']}, entries_after=last))
                with state.db:
                    state.db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?)',
                                     (identity, 'p4', json.dumps(payload), 'unknown', '{}', 0))
                before_meta = list(state.db.execute('SELECT key,value FROM meta ORDER BY key'))
                before_intents = list(state.db.execute('SELECT * FROM intents ORDER BY id'))
                with self.assertRaisesRegex(Blocked, 'sleeve checkpoint identity mismatch'):
                    cycle(venue, state, config, execute=True)
                self.assertEqual(venue.query.call_count, 0)
                self.assertEqual(venue.submit.call_count, 0)
                self.assertEqual(venue.cancel.call_count, 0)
                self.assertEqual(list(state.db.execute('SELECT key,value FROM meta ORDER BY key')), before_meta)
                self.assertEqual(list(state.db.execute('SELECT * FROM intents ORDER BY id')), before_intents)

    def checkpoint(self, version):
        model = Model()
        for bar in bars(400, 100):
            model.update(*bar)
        body = model.checkpoint()['body']
        body['version'] = version
        if version < 7:
            body.pop('shadow_open_ms', None)
            body.pop('shadow_blocked', None)
        if version == 5:
            for key in ('touch', 'shadow_in', 'shadow_repair', 'shadow_adverse', 'shadow_entry'):
                body.pop(key)
        return self.rehash(body)

    @staticmethod
    def rehash(body):
        return {'body': body, 'sha256': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}

    def seed(self, directory, version, rule=None):
        from spotquant.crowding import RULE
        from spotquant.execution import Lifecycle
        from venue_fixture import TestVenue
        config = Config('1', directory, 1, 1, 'demo', '1000')
        venue = TestVenue(bars(400, 100))
        venue.now_ms += 120_000
        venue.btc = D(1)
        checkpoint = self.checkpoint(version)
        last = checkpoint['body']['last']
        position = dict(qty='1', entry_fill='100', first_ms=last + DAY + 60_000,
                        entry_open_ms=last + DAY, peak='100', repair=False,
                        repair_peak=None, adverse=False, through=None, protection='resting')
        with State(directory, config.scope) as state:
            state.set_many(dict(rule=RULE if rule is None else rule, models={'40': checkpoint}, positions={'40': position},
                                follows={'40': None}, entries_after=last))
            lifecycle = Lifecycle(state, venue, config)
            stop = lifecycle.prepare(dict(symbol='BTCUSDT', side='SELL', type='STOP_LOSS',
                                          quantity='1', stopPrice='72', sleeves=[40]),
                                     last, state.get('positions'), state.get('follows'))
            venue.submit(stop, next(row[1]['order'] for row in lifecycle.rows() if row[0] == stop))
            payload = next(row[1] for row in lifecycle.rows() if row[0] == stop)
            payload.pop('rearm', None)
            payload.pop('position_first_ms', None)
            lifecycle.save(stop, payload, 'unknown', {})
            buy = lifecycle.prepare(dict(symbol='BTCUSDT', side='BUY', type='MARKET',
                                         quoteOrderQty='100', sleeves=[40]),
                                    last, state.get('positions'), state.get('follows'))
            payload = next(row[1] for row in lifecycle.rows() if row[0] == buy)
            payload.pop('rearm', None)
            lifecycle.save(buy, payload, 'prepared', {})
        queried = []
        query = venue.query
        def readback(identity):
            queried.append(identity)
            return query(identity)
        venue.query = readback
        venue.submit = Mock(side_effect=AssertionError('recovery must not submit'))
        venue.cancel = Mock(side_effect=AssertionError('recovery must not cancel'))
        return config, venue, stop, buy, queried

    def test_known_legacy_only_reads_original_order_and_keeps_protection_and_preparation(self):
        for rule, version in ((None, 5), (None, 6), (KNOWN_OLD_RULE, 5),
                              (KNOWN_OLD_RULE, 6), (KNOWN_OLD_RULE, 7)):
            with (self.subTest(rule=rule, version=version), tempfile.TemporaryDirectory() as directory,
                  patch('spotquant.session._cycle', side_effect=AssertionError('recovery must not run strategy')) as strategy):
                config, venue, stop, buy, queried = self.seed(directory, version, rule)
                with State(directory, config.scope) as state:
                    before = list(state.db.execute('SELECT key,value FROM meta ORDER BY key'))
                    prepared = state.db.execute('SELECT * FROM intents WHERE id=?', (buy,)).fetchone()
                    stop_payload = state.db.execute('SELECT payload FROM intents WHERE id=?', (stop,)).fetchone()
                    sent = list(venue.sent)
                    with self.assertRaises(Blocked):
                        cycle(venue, state, config)
                    self.assertEqual(queried, [])
                    with self.assertRaisesRegex(Blocked, 'readback only'):
                        cycle(venue, state, config, execute=True)
                    self.assertEqual(queried, [stop])
                    self.assertEqual(venue.sent, sent)
                    self.assertEqual(venue.orders[stop]['status'], 'NEW')
                    self.assertEqual(dict(state.db.execute('SELECT id,status FROM intents')),
                                     {stop: 'resting', buy: 'prepared'})
                    self.assertEqual(list(state.db.execute('SELECT key,value FROM meta ORDER BY key')), before)
                    self.assertEqual(state.db.execute('SELECT * FROM intents WHERE id=?', (buy,)).fetchone(), prepared)
                    self.assertEqual(state.db.execute('SELECT payload FROM intents WHERE id=?', (stop,)).fetchone(), stop_payload)
                report = run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)
                self.assertTrue(report['recovery_only'])
                self.assertTrue(report['manual_takeover'])
                self.assertTrue(report['risk_state']['manual_takeover'])
                self.assertEqual(report['risk_state']['covered_btc'], D(1))
                self.assertFalse(report['write_attempted'])
                self.assertNotIn('model_preview', report)
                self.assertEqual(venue.submit.call_count, 0)
                self.assertEqual(venue.cancel.call_count, 0)
                self.assertEqual(strategy.call_count, 0)

    def test_damaged_legacy_and_foreign_rule_never_query_native_orders(self):
        cases = ('bad_hash', 'missing_field', 'wrong_parameter', 'wrong_sleeve', 'unsupported_version', 'foreign_rule',
                 'wrong_identity', 'invalid_stop_rearm', 'missing_position', 'missing_stop_position_key',
                 'invalid_stop_position_time', 'market_position_map')
        for version, case in ((version, case) for version in (6, 7) for case in cases):
            with self.subTest(version=version, case=case), tempfile.TemporaryDirectory() as directory:
                config, venue, stop, buy, queried = self.seed(directory, version, KNOWN_OLD_RULE if version == 7 else None)
                with State(directory, config.scope) as state:
                    saved = state.get('models')
                    if case == 'bad_hash':
                        saved['40']['sha256'] = 'bad'
                    elif case == 'missing_field':
                        saved['40']['body'].pop('shadow_entry')
                        saved['40'] = self.rehash(saved['40']['body'])
                    elif case == 'wrong_parameter':
                        saved['40']['body']['trail'] = '0.01'
                        saved['40'] = self.rehash(saved['40']['body'])
                    elif case == 'wrong_sleeve':
                        saved['40']['body']['sma_window'] = 30
                        saved['40'] = self.rehash(saved['40']['body'])
                    elif case == 'unsupported_version':
                        saved['40']['body']['version'] = 4
                        saved['40'] = self.rehash(saved['40']['body'])
                    elif case == 'foreign_rule':
                        state.set('rule', 'another')
                    elif case == 'wrong_identity':
                        with state.db:
                            state.db.execute('UPDATE intents SET id=? WHERE id=?', ('foreign-id', stop))
                    elif case == 'invalid_stop_rearm':
                        payload = json.loads(state.db.execute('SELECT payload FROM intents WHERE id=?', (stop,)).fetchone()[0])
                        payload['rearm'] = {'40': True}
                        with state.db:
                            state.db.execute('UPDATE intents SET payload=? WHERE id=?', (json.dumps(payload), stop))
                    elif case == 'missing_position':
                        state.set('positions', {})
                    elif case in ('missing_stop_position_key', 'invalid_stop_position_time', 'market_position_map'):
                        identity = buy if case == 'market_position_map' else stop
                        payload = json.loads(state.db.execute('SELECT payload FROM intents WHERE id=?', (identity,)).fetchone()[0])
                        payload['position_first_ms'] = ({} if case == 'missing_stop_position_key'
                                                        else {'40': True if case == 'invalid_stop_position_time' else ORIGIN})
                        with state.db:
                            state.db.execute('UPDATE intents SET payload=? WHERE id=?', (json.dumps(payload), identity))
                    state.set('models', saved)
                    before_meta = list(state.db.execute('SELECT key,value FROM meta ORDER BY key'))
                    before_intents = list(state.db.execute('SELECT * FROM intents ORDER BY id'))
                    sent = list(venue.sent)
                    with self.assertRaises(Blocked):
                        cycle(venue, state, config, execute=True)
                    self.assertEqual(queried, [])
                    self.assertEqual(venue.sent, sent)
                    self.assertEqual(venue.submit.call_count, 0)
                    self.assertEqual(venue.cancel.call_count, 0)
                    self.assertEqual(list(state.db.execute('SELECT key,value FROM meta ORDER BY key')), before_meta)
                    self.assertEqual(list(state.db.execute('SELECT * FROM intents ORDER BY id')), before_intents)

    def test_legacy_unknown_readback_never_retries_or_dispatches_prepared_buy(self):
        for rule, version in ((None, 6), (KNOWN_OLD_RULE, 7)):
            with self.subTest(rule=rule, version=version), tempfile.TemporaryDirectory() as directory:
                config, venue, stop, buy, queried = self.seed(directory, version, rule)
                venue.orders.pop(stop)
                with State(directory, config.scope) as state:
                    before_meta = list(state.db.execute('SELECT key,value FROM meta ORDER BY key'))
                    before_intents = list(state.db.execute('SELECT * FROM intents ORDER BY id'))
                    sent = list(venue.sent)
                    with self.assertRaisesRegex(Unknown, 'never resubmitted'):
                        cycle(venue, state, config, execute=True)
                    self.assertEqual(queried, [stop])
                    self.assertEqual(venue.sent, sent)
                    self.assertEqual(venue.submit.call_count, 0)
                    self.assertEqual(venue.cancel.call_count, 0)
                    self.assertEqual(list(state.db.execute('SELECT key,value FROM meta ORDER BY key')), before_meta)
                    self.assertEqual(list(state.db.execute('SELECT * FROM intents ORDER BY id')), before_intents)


if __name__ == '__main__':
    unittest.main()
