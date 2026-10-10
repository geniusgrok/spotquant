"""Notifier, drawdown halt, and kill-switch. No network and no real mail."""
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from spotquant.config import Config
from spotquant.notify import (
    ALERT_PREFIX, HEARTBEAT_PREFIX, collect_alerts, deliver, heartbeat_message,
    missed_run_alert, remember_sent, smtp_settings, subject_for, unsent,
)
from spotquant.ops import (
    account_equity, backup_database, buy_halt_reason, check_missed_run, execute_ops,
    halt_path, kill_switch, note_equity,
)
from spotquant.state import State
from spotquant.types import Blocked


def _smtp_env(port='465', to='a@example.com, b@example.com'):
    return {
        'SPOTQUANT_SMTP_HOST': 'smtp.qq.com',
        'SPOTQUANT_SMTP_PORT': port,
        'SPOTQUANT_SMTP_USER': 'bot@example.com',
        'SPOTQUANT_SMTP_PASSWORD': 'app-password',
        'SPOTQUANT_SMTP_FROM': 'bot@example.com',
        'SPOTQUANT_SMTP_TO': to,
    }


class FakeMail:
    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.tls = False
        self.user = None
        self.message = None
        self.context = context

    def ehlo(self):
        return None

    def starttls(self, context=None):
        self.tls = True
        self.context = context

    def login(self, user, password):
        self.user = (user, password)

    def send_message(self, message):
        self.message = message

    def quit(self):
        return None


class NotifyTests(unittest.TestCase):
    def test_each_trigger_has_a_chinese_alert_subject(self):
        report = {
            'status': 'unknown',
            'manual_takeover': True,
            'protection_failure': {'reason': 'rejected', 'exchange_msg': 'Filter failure: PERCENT_PRICE_BY_SIDE'},
            'report_persistence_failed': True,
            'buy_halt': 'halt file is present; new buys are stopped',
            'model_preview': {'protections': [{'placeable': False, 'unplaceable_reason': 'band'}]},
            'environment': 'demo',
        }
        alerts = collect_alerts(report, exit_code=2)
        keys = {item['key'] for item in alerts}
        self.assertEqual(keys, {
            'status-unknown', 'manual-takeover', 'protection-failure',
            'report-persistence-failed', 'nonzero-exit', 'stop-unplaceable', 'buy-halt',
        })
        for item in alerts:
            self.assertTrue(subject_for(item).startswith(ALERT_PREFIX))
        beat = heartbeat_message({
            'environment': 'demo', 'status': 'demo_execution',
            'equity_mark': {'last': '10', 'peak': '12', 'drawdown': '0.1'},
            'risk_state': {'btc': '0.1'},
            'execution_evidence': {'stop_clamp': {'placed': '80.08', 'clamped': True}},
            'stop_price_percent_band': True,
        })
        self.assertTrue(subject_for(beat).startswith(HEARTBEAT_PREFIX))
        self.assertIn('80.08', beat['detail'])
        self.assertIn('True', beat['detail'])

    def test_smtps_and_starttls_and_port_25_is_refused(self):
        with self.assertRaisesRegex(Blocked, 'port 25'):
            smtp_settings(dict(_smtp_env(), SPOTQUANT_SMTP_PORT='25'))
        sent = []

        def ssl_factory(*args, **kwargs):
            client = FakeMail(*args, **kwargs)
            sent.append(client)
            return client

        def plain_factory(*args, **kwargs):
            raise AssertionError('465 must not open a clear SMTP connection')

        deliver('主题', '正文没有口令', env=_smtp_env(), smtp_ssl=ssl_factory, smtp_plain=plain_factory)
        self.assertEqual(sent[0].host, 'smtp.qq.com')
        self.assertEqual(sent[0].port, 465)
        self.assertFalse(sent[0].tls)
        self.assertEqual(sent[0].user, ('bot@example.com', 'app-password'))
        self.assertNotIn('app-password', sent[0].message.get_content())
        self.assertEqual(sent[0].message['To'], 'a@example.com, b@example.com')

        def plain(*args, **kwargs):
            client = FakeMail(*args, **kwargs)
            sent.append(client)
            return client

        deliver('主题', '正文', env=_smtp_env('587', 'only@example.com'), smtp_ssl=ssl_factory, smtp_plain=plain)
        self.assertTrue(sent[-1].tls)
        self.assertEqual(sent[-1].port, 587)

    def test_repeated_alerts_are_deduped_and_a_missed_run_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'alert-dedupe.json'
            item = {'key': 'manual-takeover', 'kind': 'alert', 'title': '人工接管', 'detail': 'x'}
            self.assertEqual(unsent(path, [item], 1000, 3600), [item])
            remember_sent(path, [item], 1000)
            self.assertEqual(unsent(path, [item], 2000, 3600), [])
            self.assertEqual(unsent(path, [item], 5000, 3600), [item])
            self.assertIsNone(missed_run_alert({'at': 1000}, 1000 + 3600, 26 * 3600))
            late = missed_run_alert({'at': 1000}, 1000 + 27 * 3600, 26 * 3600)
            self.assertEqual(late['key'], 'missed-run')
            self.assertTrue(subject_for(late).startswith(ALERT_PREFIX))
            env = _smtp_env()
            calls = []

            def ssl_factory(*args, **kwargs):
                client = FakeMail(*args, **kwargs)
                calls.append(client)
                return client

            first = check_missed_run(Path(directory), now=1000, env=env, smtp_ssl=ssl_factory,
                                     smtp_plain=ssl_factory)
            self.assertEqual(len(first['sent']), 1)
            second = check_missed_run(Path(directory), now=2000, env=env, smtp_ssl=ssl_factory,
                                      smtp_plain=ssl_factory)
            self.assertEqual(second['sent'], [])
            self.assertEqual(len(calls), 1)


class BreakerTests(unittest.TestCase):
    def test_drawdown_and_halt_file_stop_new_buys(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, environment='demo', capital_limit_usdt='100',
                            max_drawdown_halt_pct='0.25')
            with State(directory, config.scope) as state:
                high = {'usdt_free': '100', 'usdt_locked': '0', 'btc': '0', 'last_price': '1'}
                self.assertIsNone(note_equity(state, high, config)['buy_halt'])
                low = {'usdt_free': '70', 'usdt_locked': '0', 'btc': '0', 'last_price': '1'}
                halted = note_equity(state, low, config)
                self.assertGreaterEqual(D(halted['drawdown']), D('0.25'))
                self.assertIn('max_drawdown_halt_pct', halted['buy_halt'])
                self.assertEqual(D(state.get('equity_mark')['peak']), D('100'))
                quiet = Config('1', directory, environment='demo', capital_limit_usdt='100')
                state.set('equity_mark', {'initial': '100', 'peak': '100', 'last': '90', 'drawdown': '0.1'})
                self.assertIsNone(buy_halt_reason(state, quiet, {'usdt_free': '90', 'btc': '0', 'last_price': '1'}))
                halt_path(state).write_text('', encoding='utf-8')
                self.assertIn('halt file', buy_halt_reason(state, quiet, high))

    def test_backup_rotates(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'intents.sqlite'
            conn = sqlite3.connect(db)
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('INSERT INTO meta VALUES ("identity", "demo")')
            conn.commit()
            conn.close()
            dest = Path(directory) / 'backups'
            first = backup_database(db, dest, keep=2, now=1_700_000_000)
            copied = sqlite3.connect(first)
            self.assertEqual(copied.execute('SELECT value FROM meta').fetchone()[0], 'demo')
            copied.close()
            backup_database(db, dest, keep=2, now=1_700_000_100)
            backup_database(db, dest, keep=2, now=1_700_000_200)
            self.assertEqual(len(list(dest.glob('intents-*.sqlite'))), 2)


class KillSwitchTests(unittest.TestCase):
    def test_confirm_cancels_only_our_orders_and_sells_the_recorded_quantity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, environment='demo', capital_limit_usdt='100')
            calls = []

            class Venue:
                def snapshot(self, uid):
                    calls.append(('snapshot', uid))
                    return {
                        'btc': D('0.5'), 'btc_free': D('0.5'), 'last_price': D('100'),
                        'avg_price': D('100'), 'min_notional': D('5'),
                        'orders': [
                            {'client_id': 'sq-ours', 'order_id': 7, 'type': 'STOP_LOSS'},
                            {'client_id': 'sq-foreign', 'order_id': 8, 'type': 'STOP_LOSS'},
                        ],
                    }

                def cancel(self, identity, *, order_id, cancel_id):
                    calls.append(('cancel', identity, order_id, cancel_id))

                def submit(self, identity, payload):
                    calls.append(('submit', identity, payload))
                    return {'orderId': 9}

            with State(directory, config.scope) as state:
                state.set('positions', {'40': {'qty': '0.1', 'dust': False}})
                state.db.execute(
                    'INSERT INTO intents VALUES (?,?,?,?,?,?)',
                    ('sq-ours', 'p4', '{}', 'resting', '{}', 1))
                state.db.commit()
                with self.assertRaisesRegex(Blocked, '--confirm'):
                    kill_switch(state, Venue(), config, confirm=False)
                self.assertEqual(calls, [])
                result = kill_switch(state, Venue(), config, confirm=True)
            self.assertEqual(result['environment'], 'demo')
            self.assertEqual(result['cancelled'], ['sq-ours'])
            self.assertEqual(result['sold'], '0.1')
            self.assertEqual(D(result['unexplained_btc']), D('0.4'))
            submits = [item for item in calls if item[0] == 'submit']
            self.assertEqual(submits[0][2]['quantity'], '0.1')
            self.assertEqual(submits[0][2]['side'], 'SELL')

    def test_live_ops_requires_the_enable_file_and_a_busy_lock_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, environment='live', capital_limit_usdt='100')
            with self.assertRaisesRegex(Blocked, 'not explicitly enabled'):
                execute_ops(config, object(), expect_environment='live', run_session=lambda *_: {},
                            env={'SPOTQUANT_LIVE_ENABLE_FILE': str(Path(directory) / 'missing')})
            flag = Path(directory) / 'LIVE_ENABLED'
            flag.write_text('', encoding='utf-8')

            def locked(config, venue):
                raise Blocked('state unavailable or another run holds the execution lock')

            skipped = execute_ops(config, object(), expect_environment='live', run_session=locked,
                                  env={'SPOTQUANT_LIVE_ENABLE_FILE': str(flag)})
            self.assertTrue(skipped['skipped'])
            self.assertEqual(skipped['status'], 'pass')


class OpsRunNotifyTests(unittest.TestCase):
    def test_a_finished_session_sends_a_heartbeat_once_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, environment='demo', capital_limit_usdt='100')
            calls = []

            def ssl_factory(*args, **kwargs):
                client = FakeMail(*args, **kwargs)
                calls.append(client)
                return client

            def session(config, venue):
                return {'status': 'demo_execution', 'environment': 'demo', 'equity_mark': {'last': '10', 'peak': '10', 'drawdown': '0'},
                        'risk_state': {'btc': '0'}, 'stop_price_percent_band': False}

            env = _smtp_env()
            first = execute_ops(config, object(), expect_environment='demo', run_session=session,
                                env=env, smtp_ssl=ssl_factory, smtp_plain=ssl_factory, now=1_700_000_000)
            self.assertEqual(len(first['notifications']['sent']), 1)
            self.assertTrue(first['notifications']['sent'][0]['subject'].startswith(HEARTBEAT_PREFIX))
            second = execute_ops(config, object(), expect_environment='demo', run_session=session,
                                 env=env, smtp_ssl=ssl_factory, smtp_plain=ssl_factory, now=1_700_000_100)
            self.assertEqual(second['notifications']['sent'], [])
            self.assertEqual(len(calls), 1)
            stored = json.loads((Path(directory) / 'heartbeat.json').read_text())
            self.assertEqual(stored['status'], 'demo_execution')
            self.assertIsNone(account_equity({}))
