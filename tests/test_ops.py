"""Notifier, drawdown halt, and kill-switch. No network and no real mail."""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from spotquant.config import Config
from spotquant.notify import (
    ALERT_PREFIX, HEARTBEAT_PREFIX, collect_alerts, deliver, heartbeat_message,
    missed_run_alert, remember_sent, smtp_settings, subject_for, unsent,
)
from spotquant.ops import (
    HEARTBEAT_MAX_AGE_SECONDS, account_equity, backup_database, buy_halt_reason,
    check_missed_run, execute_ops, halt_path, kill_switch, note_equity,
)
from spotquant.state import State
from spotquant.types import Blocked, floor_step


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

    def test_demo_and_live_sessions_start_at_0045_utc(self):
        root = Path(__file__).resolve().parents[1]
        session = (root / 'deploy/systemd/spotquant-session@.timer').read_text(encoding='utf-8')
        self.assertIn('OnCalendar=*-*-* 00:45:00 UTC', session)
        self.assertNotIn('00:05:00', session)
        self.assertIn('spotquant-session@%i.service', session)
        backup = (root / 'deploy/systemd/spotquant-backup@.timer').read_text(encoding='utf-8')
        self.assertIn('OnCalendar=*-*-* 01:05:00 UTC', backup)
        # 26 hours after 00:45 still includes the next day's start, then the
        # hourly check alerts once the clock passes 02:45 UTC.
        self.assertEqual(HEARTBEAT_MAX_AGE_SECONDS, 26 * 3600)
        self.assertIsNone(missed_run_alert({'at': 0}, 26 * 3600, HEARTBEAT_MAX_AGE_SECONDS))
        self.assertEqual(missed_run_alert({'at': 0}, 26 * 3600 + 1, HEARTBEAT_MAX_AGE_SECONDS)['key'],
                         'missed-run')
        for name in ('spotquant-session@.service', 'spotquant-watch@.service',
                     'spotquant-backup@.service', 'spotquant-failed@.service'):
            unit = (root / 'deploy/systemd' / name).read_text(encoding='utf-8')
            self.assertIn('UMask=0077', unit)
            self.assertIn('/usr/local/bin/sq ', unit)
        for name in ('spotquant-session@live.service', 'spotquant-watch@live.service',
                     'spotquant-backup@live.service'):
            dropin = (root / 'deploy/systemd' / f'{name}.d' / 'enable.conf').read_text(encoding='utf-8')
            self.assertIn('ConditionPathExists=/etc/spotquant/LIVE_ENABLED', dropin)


class KillSwitchTests(unittest.TestCase):
    def _open(self, directory):
        from test_execution import ExecutionTests
        return ExecutionTests().entered(directory)

    def test_confirm_is_required_and_unrecorded_btc_is_not_sold(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            sent = len(venue.sent)
            with State(directory, config.scope) as state:
                with self.assertRaisesRegex(Blocked, '--confirm'):
                    kill_switch(state, venue, config, confirm=False)
            self.assertEqual(len(venue.sent), sent)
            self.assertFalse((Path(directory) / 'HALT').is_file())
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, environment='demo', capital_limit_usdt='100')

            class Venue:
                execution_authorized = True

                def snapshot(self, uid):
                    return {
                        'btc': D('0.5'), 'btc_free': D('0.5'), 'last_price': D('100'),
                        'avg_price': D('100'), 'min_notional': D('5'), 'orders': [],
                    }

            with State(directory, config.scope) as state:
                with self.assertRaisesRegex(Blocked, 'will not adopt'):
                    kill_switch(state, Venue(), config, confirm=True, env={})
            self.assertTrue((Path(directory) / 'HALT').is_file())

    def test_full_fill_is_confirmed_and_the_next_session_continues(self):
        from test_execution import run_day
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            owned = venue.btc
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
                position = (state.get('positions') or {}).get('40')
            sell_fills = [row for row in venue.fills if not row['buyer']]
            self.assertEqual(result['status'], 'pass', result)
            self.assertFalse(result['manual_takeover'])
            self.assertEqual(D(result['sold']), sum((row['qty'] for row in sell_fills), D(0)))
            self.assertLess(D(result['sold']), owned)
            self.assertGreater(D(result['residual_btc']), 0)
            self.assertTrue(result['dust'])
            self.assertLess(venue.btc * venue.price, D('5'))
            self.assertLess(D(result['residual_btc']) * venue.price, D('5'))
            with State(directory, config.scope) as state:
                payload = json.loads(state.db.execute(
                    "SELECT payload FROM intents WHERE id=?", (result['cancelled'][0],)).fetchone()[0])
                self.assertTrue(str(payload.get('cancel_id', '')).startswith('sq-'))
            sent = len(venue.sent)
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [], report)
            self.assertNotIn('external trade', str(report))
            self.assertEqual(len(venue.sent), sent)

    def test_partial_fill_reports_the_confirmed_quantity_and_the_dust(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            venue.fraction = D('0.5')
            requested = floor_step(venue.btc, D('0.00001'))
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(result['status'], 'pass', result)
            self.assertLess(D(result['sold']), requested)
            self.assertGreater(D(result['residual_btc']), 0)
            self.assertTrue(result['dust'])
            self.assertNotEqual(result['sold'], format(requested, 'f'))
            self.assertLess(venue.btc * venue.price, D('5'))

    def test_a_rejected_sell_does_not_report_a_fill_and_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            original = venue.submit
            btc = venue.btc

            def submit(identity, payload, preflight=None):
                if payload.get('side') == 'SELL' and payload.get('type') == 'MARKET':
                    raise Blocked('native filter rejected market sell')
                return original(identity, payload, preflight=preflight)

            venue.submit = submit
            calls = []

            def smtp(*args, **kwargs):
                client = FakeMail(*args, **kwargs)
                calls.append(client)
                return client

            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env=_smtp_env(), smtp_ssl=smtp)
            self.assertNotEqual(result['status'], 'pass')
            self.assertEqual(result['sold'], '0')
            self.assertEqual(venue.btc, btc)
            self.assertGreater(D(result['residual_btc']), 1)
            self.assertTrue(any(row['status'] == 'NEW' and row['type'] == 'STOP_LOSS'
                                for row in venue.orders.values()))
            self.assertTrue(calls)
            self.assertTrue(calls[0].message['Subject'].startswith(ALERT_PREFIX))

    def test_lost_acknowledgement_is_recovered_without_a_second_sell(self):
        from spotquant.types import Unknown as OrderUnknown
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            venue.lose_ack = True
            queries = {'n': 0}
            original = venue.query

            def query(identity):
                row = original(identity)
                if (queries['n'] == 0 and isinstance(row, dict) and row.get('type') == 'MARKET'
                        and row.get('side') == 'SELL'):
                    queries['n'] += 1
                    raise OrderUnknown('order query unavailable')
                return row

            venue.query = query
            with State(directory, config.scope) as state:
                first = kill_switch(state, venue, config, confirm=True, env={})
            sells = [row for row in venue.orders.values() if row.get('side') == 'SELL' and row.get('type') == 'MARKET']
            self.assertEqual(first['status'], 'unknown', first)
            self.assertNotEqual(first.get('sold'), format(floor_step(D(first['residual_btc']), D('0.00001')), 'f'))
            self.assertGreater(D(first['residual_btc']), 1)
            self.assertEqual(len(sells), 1)
            sent = len(venue.sent)
            with State(directory, config.scope) as state:
                second = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(second['status'], 'pass', second)
            self.assertEqual(len(venue.sent), sent)
            self.assertEqual(len([row for row in venue.orders.values()
                                  if row.get('side') == 'SELL' and row.get('type') == 'MARKET']), 1)
            self.assertEqual(D(second['sold']), D(sells[0]['executedQty']))

    def test_crash_after_cancel_resumes_the_same_sell(self):
        from test_execution import SimulatedCrash
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            original = venue.cancel

            def crash(identity, **kwargs):
                original(identity, **kwargs)
                raise SimulatedCrash

            venue.cancel = crash
            with State(directory, config.scope) as state:
                with self.assertRaises(SimulatedCrash):
                    kill_switch(state, venue, config, confirm=True, env={})
                payload = json.loads(state.db.execute(
                    "SELECT payload FROM intents WHERE status='canceling'").fetchone()[0])
                prepared = state.db.execute(
                    "SELECT id FROM intents WHERE status='prepared'").fetchone()
            self.assertTrue(str(payload.get('cancel_id', '')).startswith('sq-'))
            self.assertIsNotNone(prepared)
            venue.cancel = original
            sent = len(venue.sent)
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(result['status'], 'pass', result)
            self.assertEqual(len([row for row in venue.orders.values()
                                  if row.get('type') == 'MARKET' and row.get('side') == 'SELL']), 1)
            self.assertEqual(len(venue.sent), sent + 1)

    def test_crash_after_the_fill_and_before_the_ledger_does_not_sell_again(self):
        from test_execution import SimulatedCrash, run_day
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            with State(directory, config.scope) as state:
                original = state.set_many

                def set_many(values):
                    if 'positions' in values:
                        for payload, result in state.db.execute(
                                "SELECT payload,result FROM intents WHERE status='settled'"):
                            order = (json.loads(payload).get('order') or {})
                            saved = json.loads(result)
                            if (order.get('side') == 'SELL' and order.get('type') == 'MARKET'
                                    and saved.get('orderId')):
                                raise SimulatedCrash
                    return original(values)

                state.set_many = set_many
                with self.assertRaises(SimulatedCrash):
                    kill_switch(state, venue, config, confirm=True, env={})
                position = D(state.get('positions')['40']['qty'])
            self.assertGreater(position, 1)
            sent = len(venue.sent)
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(result['status'], 'pass', result)
            self.assertEqual(len(venue.sent), sent)
            self.assertGreater(D(result['sold']), 0)
            self.assertNotEqual(D(result['residual_btc']), position)
            report = run_day(config, venue)
            self.assertEqual(report['errors'], [], report)
            self.assertNotIn('external trade', str(report))

    def test_a_foreign_open_order_is_left_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            venue.orders['sq-foreign'] = {
                'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS', 'status': 'NEW',
                'quantity': '0.1', 'executedQty': '0', 'stopPrice': '1',
                'clientOrderId': 'sq-foreign', 'orderId': 99, 'id': 'sq-foreign',
            }
            sent = len(venue.sent)
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertNotEqual(result['status'], 'pass')
            self.assertEqual(len(venue.sent), sent)
            self.assertEqual(venue.orders['sq-foreign']['status'], 'NEW')
            self.assertTrue(any(row['status'] == 'NEW' and row['type'] == 'STOP_LOSS'
                                and row['clientOrderId'] != 'sq-foreign'
                                for row in venue.orders.values()))

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


class SourceShaWrapperTests(unittest.TestCase):
    def test_sq_prefers_the_environment_then_git_then_the_recorded_file(self):
        wrapper = Path(__file__).resolve().parents[1] / 'deploy' / 'sq'
        install = wrapper.parent.joinpath('install.sh').read_text(encoding='utf-8')
        self.assertIn('install -m 755 "$ROOT/deploy/sq" /usr/local/bin/sq', install)
        self.assertIn('/etc/spotquant/source_sha', install)
        for unit in ('spotquant-session@live.service', 'spotquant-watch@live.service',
                     'spotquant-backup@live.service'):
            self.assertIn(unit, install)
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / 'tree'
            checkout.mkdir()
            (checkout / 'marker').write_text('x', encoding='utf-8')
            sha_file = Path(directory) / 'source_sha'
            stub = Path(directory) / 'python-stub'
            stub.write_text('#!/bin/sh\nprintf %s "$SPOTQUANT_SOURCE_SHA"\n', encoding='utf-8')
            stub.chmod(0o755)
            base = {
                'PATH': os.environ.get('PATH', ''),
                'SPOTQUANT_ROOT': str(checkout),
                'SPOTQUANT_SOURCE_SHA_FILE': str(sha_file),
                'SPOTQUANT_PYTHON': str(stub),
                'SPOTQUANT_SOURCE_SHA': '',
            }
            subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
            subprocess.run(['git', '-C', str(checkout), 'add', 'marker'], check=True)
            subprocess.run(
                ['git', '-C', str(checkout), '-c', 'user.email=spotquant@example.com',
                 '-c', 'user.name=spotquant', 'commit', '-q', '-m', 'marker'],
                check=True,
            )
            head = subprocess.run(
                ['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            seen = subprocess.run([str(wrapper), 'status'], check=True, capture_output=True, text=True, env=base)
            self.assertEqual(seen.stdout, head)
            shutil.rmtree(checkout / '.git')
            sha_file.write_text(head + '\n', encoding='utf-8')
            fallback = subprocess.run([str(wrapper), 'status'], check=True, capture_output=True, text=True, env=base)
            self.assertEqual(fallback.stdout, head)
            forced = dict(base, SPOTQUANT_SOURCE_SHA='operator-sha')
            chosen = subprocess.run([str(wrapper), 'status'], check=True, capture_output=True, text=True, env=forced)
            self.assertEqual(chosen.stdout, 'operator-sha')


class KillSwitchReviewTests(unittest.TestCase):
    def _open(self, directory):
        from test_execution import ExecutionTests
        return ExecutionTests().entered(directory)

    def test_a_stop_fill_during_cancel_resizes_the_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            owned = venue.btc
            original = venue.cancel

            def cancel(identity, **kwargs):
                row = venue.query(kwargs['order_id'])
                qty = D(row['quantity']) * D('0.4')
                quote = qty * venue.price
                venue.btc -= qty
                venue.cash += quote * (D(1) - venue.fee)
                row['executedQty'] = format(qty, 'f')
                row['status'] = 'PARTIALLY_FILLED'
                venue._fill(row, qty, quote)
                return original(identity, **kwargs)

            venue.cancel = cancel
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            markets = [row for row in venue.orders.values()
                       if row.get('type') == 'MARKET' and row.get('side') == 'SELL']
            self.assertTrue(markets)
            self.assertTrue(all(D(row['quantity']) < owned * D('0.7') for row in markets))
            self.assertGreaterEqual(venue.btc, 0)
            self.assertLess(abs(D(result['residual_btc']) - venue.btc), D('0.00005'))

    def test_a_full_stop_fill_during_cancel_does_not_keep_the_old_sell(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            sent = list(venue.sent)

            def cancel(identity, **kwargs):
                row = venue.query(kwargs['order_id'])
                qty = D(row['quantity'])
                quote = qty * venue.price
                venue.btc -= qty
                venue.cash += quote * (D(1) - venue.fee)
                row['executedQty'] = format(qty, 'f')
                row['status'] = 'FILLED'
                venue._fill(row, qty, quote)
                return row

            venue.cancel = cancel
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            markets = [row for row in venue.orders.values()
                       if row.get('type') == 'MARKET' and row.get('side') == 'SELL']
            self.assertEqual(markets, [])
            self.assertEqual(venue.sent, sent)
            self.assertEqual(result['status'], 'pass', result)
            self.assertLess(venue.btc * venue.price, D('5'))

    def test_the_twelfth_partial_fill_is_booked_before_the_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            owned = venue.btc
            venue.fraction = D('0.1')
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertLess(abs(D(result['residual_btc']) - venue.btc), D('0.00005'))
            self.assertLess(abs(D(result['sold']) + D(result['residual_btc']) - owned), D('0.001'))
            self.assertEqual(result['status'], 'unknown')
            self.assertTrue(result['manual_takeover'])
            self.assertIn('notifications', result)

    def test_not_sent_restores_protection_instead_of_looking_unknown(self):
        from spotquant.types import NotSent
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            original = venue.submit
            btc = venue.btc

            def submit(identity, payload, preflight=None):
                if payload.get('side') == 'SELL' and payload.get('type') == 'MARKET':
                    raise NotSent('sell quantity fails native lot filters')
                return original(identity, payload, preflight=preflight)

            venue.submit = submit
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertNotEqual(result['status'], 'unknown')
            self.assertNotIn('not confirmed', result.get('reason', ''))
            self.assertEqual(venue.btc, btc)
            self.assertTrue(any(row['status'] == 'NEW' and row['type'] == 'STOP_LOSS'
                                for row in venue.orders.values()))
            self.assertFalse(result['manual_takeover'])

    def test_a_known_lot_filter_does_not_cancel_the_stop_first(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            original = venue.snapshot
            stop_id = next(row['clientOrderId'] for row in venue.orders.values() if row['status'] == 'NEW')

            def snapshot(uid):
                row = original(uid)
                row['market_max_qty'] = D(1)
                return row

            venue.snapshot = snapshot
            sent = len(venue.sent)
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(len(venue.sent), sent)
            self.assertEqual(venue.orders[stop_id]['status'], 'NEW')
            self.assertIn('existing protection was kept', result['reason'])
            self.assertNotEqual(result['status'], 'pass')

    def test_a_short_or_partial_stop_still_requires_takeover_when_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            stop = next(row for row in venue.orders.values() if row['status'] == 'NEW')
            stop['quantity'] = '0.1'
            venue.cash += D(1)
            with State(directory, config.scope) as state:
                encoded = state.db.execute('SELECT payload FROM intents WHERE id=?',
                                           (stop['clientOrderId'],)).fetchone()[0]
                payload = json.loads(encoded)
                payload['order']['quantity'] = '0.1'
                state.db.execute('UPDATE intents SET payload=? WHERE id=?',
                                 (json.dumps(payload), stop['clientOrderId']))
                state.db.commit()
                short = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(short['status'], 'unknown')
            self.assertTrue(short['manual_takeover'])
            self.assertEqual(stop['status'], 'NEW')
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            stop = next(row for row in venue.orders.values() if row['status'] == 'NEW')
            filled = D(stop['quantity']) * D('0.2')
            stop['status'] = 'PARTIALLY_FILLED'
            stop['executedQty'] = format(filled, 'f')
            venue.cash += D(1)
            with State(directory, config.scope) as state:
                partial = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(partial['status'], 'unknown')
            self.assertTrue(partial['manual_takeover'])

    def test_sold_ignores_an_older_cycle_and_an_unread_acknowledgement(self):
        from spotquant.types import Unknown as OrderUnknown
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            original = venue.submit
            original_query = venue.query
            failed = {'n': 0}

            def query(identity):
                row = original_query(identity)
                if (failed['n'] == 0 and isinstance(row, dict) and row.get('side') == 'SELL'
                        and row.get('type') == 'MARKET'):
                    failed['n'] += 1
                    raise OrderUnknown('order query unavailable')
                return row

            def submit(identity, payload, preflight=None):
                row = original(identity, payload, preflight=preflight)
                venue.query = query
                return row

            venue.submit = submit
            with State(directory, config.scope) as state:
                first = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(first['status'], 'unknown')
            self.assertIsNone(first['sold'])
            self.assertEqual(D(first['unconfirmed_executed']), D(next(
                row['executedQty'] for row in venue.orders.values()
                if row.get('side') == 'SELL' and row.get('type') == 'MARKET')))
            sent = len(venue.sent)
            with State(directory, config.scope) as state:
                second = kill_switch(state, venue, config, confirm=True, env={})
                payload = json.dumps({'order': {
                    'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '9',
                }, 'sleeves': [40], 'signal_ms': 1, 'weights': {'40': '9'}, 'repair': {'40': False},
                    'rearm': {'40': False}})
                result = json.dumps({'orderId': 50, 'executedQty': '9', 'status': 'FILLED',
                                     'clientOrderId': 'sq-old-cycle'})
                state.db.execute(
                    'INSERT INTO intents VALUES (?,?,?,?,?,?)',
                    ('sq-old-cycle', 'p4', payload, 'settled', result, 1))
                state.db.commit()
                third = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(len(venue.sent), sent)
            self.assertEqual(third['sold'], second['sold'])
            self.assertNotEqual(D(third['sold']), D(second['sold']) + D(9))

    def test_halt_is_written_before_cancel_and_a_session_does_not_buy(self):
        from test_execution import SimulatedCrash, add_day, run_day, venue_before_entry
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            armed = {}
            original = venue.cancel

            def cancel(identity, **kwargs):
                armed['before'] = (Path(directory) / 'HALT').is_file()
                raise SimulatedCrash

            venue.cancel = cancel
            with State(directory, config.scope) as state:
                with self.assertRaises(SimulatedCrash):
                    kill_switch(state, venue, config, confirm=True, env={})
            self.assertTrue(armed['before'])
            self.assertTrue((Path(directory) / 'HALT').is_file())
            buys = len([row for row in venue.orders.values() if row.get('side') == 'BUY'])
            venue.cancel = original
            run_day(config, venue)
            self.assertTrue((Path(directory) / 'HALT').is_file())
            self.assertEqual(len([row for row in venue.orders.values() if row.get('side') == 'BUY']), buys)
        with tempfile.TemporaryDirectory() as directory:
            config = Config('1', directory, 1, 1, 'demo', '1000')
            venue = venue_before_entry()
            self.assertEqual(run_day(config, venue)['errors'], [])
            add_day(venue, '101')
            (Path(directory) / 'HALT').write_text('', encoding='utf-8')
            sent = len(venue.sent)
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len(venue.sent), sent)
            (Path(directory) / 'HALT').unlink()
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertGreater(len(venue.sent), sent)
            self.assertTrue(any(row.get('side') == 'BUY' for row in venue.orders.values()))

    def test_removing_halt_does_not_buy_the_stale_touch_signal(self):
        from test_execution import SimulatedCrash, add_day, run_day
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            venue.price = D('100.4')
            original = venue.cancel

            def crash(identity, **kwargs):
                original(identity, **kwargs)
                raise SimulatedCrash

            venue.cancel = crash
            with self.assertRaises(SimulatedCrash):
                run_day(config, venue)
            venue.cancel = original
            venue.price = D('102')
            calls = []

            def smtp(*args, **kwargs):
                client = FakeMail(*args, **kwargs)
                calls.append(client)
                return client

            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env=_smtp_env(), smtp_ssl=smtp)
            self.assertTrue(result['halt'])
            self.assertIn('halt file', result['buy_halt'])
            subjects = [str(client.message['Subject']) for client in calls]
            self.assertTrue(any('回撤停买' in subject for subject in subjects))
            self.assertFalse(any('退出码非零' in subject for subject in subjects))
            with State(directory, config.scope) as state:
                for payload, in state.db.execute("SELECT payload FROM intents"):
                    body = json.loads(payload)
                    if (body.get('order') or {}).get('side') == 'SELL' and (body.get('order') or {}).get('type') == 'MARKET':
                        self.assertFalse(any((body.get('rearm') or {}).values()))
            buys = len([row for row in venue.orders.values() if row.get('side') == 'BUY'])
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len([row for row in venue.orders.values() if row.get('side') == 'BUY']), buys)
            (Path(directory) / 'HALT').unlink()
            add_day(venue, '103')
            self.assertEqual(run_day(config, venue)['errors'], [])
            self.assertEqual(len([row for row in venue.orders.values() if row.get('side') == 'BUY']), buys)

    def test_the_twelfth_fill_can_finish_as_dust(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            original = venue.submit
            markets = {'n': 0}

            def submit(identity, payload, preflight=None):
                if payload.get('side') == 'SELL' and payload.get('type') == 'MARKET':
                    markets['n'] += 1
                    venue.fraction = D('1') if markets['n'] >= 12 else D('0.1')
                return original(identity, payload, preflight=preflight)

            venue.submit = submit
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(result['status'], 'pass', result)
            self.assertFalse(result['manual_takeover'])
            self.assertTrue(result['dust'])
            self.assertLess(venue.btc * venue.price, D('5'))
            self.assertLess(abs(D(result['residual_btc']) - venue.btc), D('0.00005'))

    def test_a_touch_exit_keeps_its_confirmed_sold_after_restart(self):
        from test_execution import SimulatedCrash, run_day
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            venue.price = D('100.4')
            original = venue.cancel

            def crash(identity, **kwargs):
                original(identity, **kwargs)
                raise SimulatedCrash

            venue.cancel = crash
            with self.assertRaises(SimulatedCrash):
                run_day(config, venue)
            venue.cancel = original
            venue.price = D('102')
            with State(directory, config.scope) as state:
                payload = json.dumps({
                    'order': {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '1'},
                    'sleeves': [40], 'signal_ms': 1, 'weights': {'40': '1'},
                    'repair': {'40': False}, 'rearm': {'40': True},
                })
                state.db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?)',
                                 ('sq-history', 'p4', payload, 'settled', '{}', 1))
                state.db.commit()
                first = kill_switch(state, venue, config, confirm=True, env={})
                second = kill_switch(state, venue, config, confirm=True, env={})
                kept = json.loads(state.db.execute(
                    'SELECT payload FROM intents WHERE id=?', ('sq-history',)).fetchone()[0])
            self.assertEqual(first['status'], 'pass', first)
            self.assertIsNotNone(first['sold'])
            self.assertEqual(second['sold'], first['sold'])
            self.assertTrue(kept['rearm']['40'])

    def test_documented_env_grep_does_not_prefix_names(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / 'deploy/README.md').read_text(encoding='utf-8')
        self.assertIn("grep -h -v '^#' /etc/spotquant/live.env /etc/spotquant/notify.env", text)
        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory) / 'live.env'
            notify = Path(directory) / 'notify.env'
            live.write_text('SPOTQUANT_BINANCE_DEMO_KEY=one\n', encoding='utf-8')
            notify.write_text('SPOTQUANT_SMTP_HOST=two\n', encoding='utf-8')
            seen = subprocess.run(
                ['sh', '-c', "env $(grep -h -v '^#' live.env notify.env | xargs) "
                 "python3 -c 'import os; print(os.environ[\"SPOTQUANT_BINANCE_DEMO_KEY\"]"
                 "+\"|\"+os.environ[\"SPOTQUANT_SMTP_HOST\"])'"],
                cwd=directory, check=True, capture_output=True, text=True)
            self.assertEqual(seen.stdout.strip(), 'one|two')

    def test_cli_notifies_when_halt_or_backoff_setup_fails(self):
        from spotquant.cli import main
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': '10001', 'state_dir': directory,
                'environment': 'demo', 'capital_limit_usdt': '100',
            }), encoding='utf-8')
            notes = []

            def connect(loaded, execute_orders=False):
                class Venue:
                    execution_authorized = True

                    def bind_state(self, state):
                        raise Blocked('persisted rate limit state is invalid')

                    def snapshot(self, uid):
                        raise AssertionError('snapshot after a bad backoff record')

                return Venue()

            def dispatch(directory, report, **kwargs):
                notes.append(report['reason'])
                return {'sent': [], 'skipped': 0, 'errors': []}

            with patch('spotquant.cli.connect', connect), \
                    patch('spotquant.ops.dispatch_notifications', dispatch):
                code = main(['kill-switch', '--config', str(path), '--authorize-uid', '10001', '--confirm'])
            self.assertEqual(code, 2)
            self.assertEqual(notes, ['persisted rate limit state is invalid'])
            self.assertTrue((Path(directory) / 'HALT').is_file())
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': config.account_uid, 'state_dir': directory,
                'environment': 'demo', 'capital_limit_usdt': '1000',
                'session_seconds': 1, 'poll_seconds': 1,
            }), encoding='utf-8')
            notes = []

            def connect(loaded, execute_orders=False):
                return venue

            def dispatch(directory, report, **kwargs):
                notes.append(report['status'])
                return {'sent': [], 'skipped': 0, 'errors': []}

            with patch('spotquant.cli.connect', connect), \
                    patch('spotquant.ops._arm_halt', side_effect=OSError('halt directory is read only')), \
                    patch('spotquant.ops.dispatch_notifications', dispatch):
                code = main(['kill-switch', '--config', str(path), '--authorize-uid', config.account_uid,
                             '--confirm'])
            self.assertEqual(code, 2)
            self.assertEqual(notes, ['unknown'])

    def test_cli_kill_switch_notifies_and_keeps_a_saved_backoff(self):
        from spotquant.binance import Binance
        from spotquant.cli import main
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, environment='demo', capital_limit_usdt='100')
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': '10001', 'state_dir': directory,
                'environment': 'demo', 'capital_limit_usdt': '100',
            }), encoding='utf-8')
            with State(directory, config.scope) as state:
                state.set('rate_limits', {'demo-api.binance.com': 1_100_000})
            requests = []

            def opener(method, url, headers):
                requests.append(url)
                return (429, b'', {'Retry-After': '120'})

            def connect(loaded, execute_orders=False):
                venue = Binance(key='k', secret='s', environment='demo', capital_limit=D('100'),
                                demo_execution_uid='10001', clock=lambda: 1000, opener=opener)
                venue._monotonic = lambda: 0
                return venue

            notes = []

            def dispatch(directory, report, **kwargs):
                notes.append(report['status'])
                return {'sent': [], 'skipped': 0, 'errors': []}

            with patch('spotquant.cli.connect', connect), \
                    patch('spotquant.ops.dispatch_notifications', dispatch):
                code = main(['kill-switch', '--config', str(path), '--authorize-uid', '10001', '--confirm'])
            self.assertEqual(code, 2)
            self.assertEqual(requests, [])
            self.assertEqual(notes, ['unknown'])
            self.assertTrue((Path(directory) / 'HALT').is_file())
            with State(directory, config.scope) as state:
                self.assertEqual(state.get('rate_limits'), {'demo-api.binance.com': 1_100_000})
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, environment='demo', capital_limit_usdt='100')
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': '10001', 'state_dir': directory,
                'environment': 'demo', 'capital_limit_usdt': '100',
            }), encoding='utf-8')
            requests = []

            def opener(method, url, headers):
                requests.append(url)
                return (429, b'', {'Retry-After': '120'})

            def connect(loaded, execute_orders=False):
                venue = Binance(key='k', secret='s', environment='demo', capital_limit=D('100'),
                                demo_execution_uid='10001', clock=lambda: 1000, opener=opener)
                venue._monotonic = lambda: 0
                return venue

            with patch('spotquant.cli.connect', connect), \
                    patch('spotquant.ops.dispatch_notifications', lambda *args, **kwargs: {
                        'sent': [], 'skipped': 0, 'errors': []}):
                code = main(['kill-switch', '--config', str(path), '--authorize-uid', '10001', '--confirm'])
            self.assertEqual(code, 2)
            self.assertEqual(len(requests), 1)
            self.assertTrue((Path(directory) / 'HALT').is_file())
            with State(directory, config.scope) as state:
                self.assertEqual(state.get('rate_limits'), {'demo-api.binance.com': 1_120_000})

    def test_halt_is_written_when_the_first_recover_fails(self):
        from spotquant.types import Unknown as OrderUnknown
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            sent = list(venue.sent)

            def query(identity):
                raise OrderUnknown('order query unavailable')

            venue.query = query
            with State(directory, config.scope) as state:
                result = kill_switch(state, venue, config, confirm=True, env={})
            self.assertTrue((Path(directory) / 'HALT').is_file())
            self.assertEqual(result['status'], 'unknown', result)
            self.assertTrue(result['halt'])
            self.assertEqual(venue.sent, sent)
            self.assertTrue(any(row.get('status') == 'NEW' and row.get('type') == 'STOP_LOSS'
                                for row in venue.orders.values()))
            self.assertFalse(any(row.get('side') == 'SELL' and row.get('type') == 'MARKET'
                                 for row in venue.orders.values()))

    def test_halt_is_written_on_a_dust_or_flat_account(self):
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            with State(directory, config.scope) as state:
                first = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(first['status'], 'pass', first)
            self.assertTrue(first['dust'])
            (Path(directory) / 'HALT').unlink()
            sent = list(venue.sent)
            with State(directory, config.scope) as state:
                second = kill_switch(state, venue, config, confirm=True, env={})
            self.assertEqual(second['status'], 'pass', second)
            self.assertTrue((Path(directory) / 'HALT').is_file())
            self.assertEqual(venue.sent, sent)
        with tempfile.TemporaryDirectory() as directory:
            config = Config('10001', directory, environment='demo', capital_limit_usdt='100')

            class Flat:
                execution_authorized = True

                def clock(self):
                    return 1_700_000_000

                def snapshot(self, uid):
                    return {
                        'btc': D(0), 'btc_free': D(0), 'last_price': D('100'),
                        'avg_price': D('100'), 'min_notional': D('5'), 'orders': [],
                        'usdt_free': D('100'), 'usdt_locked': D(0),
                    }

                def trades(self, since, from_id=None):
                    return []

                def query(self, identity):
                    raise AssertionError('flat kill queried an order')

            with State(directory, config.scope) as state:
                result = kill_switch(state, Flat(), config, confirm=True, env={})
            self.assertEqual(result['status'], 'pass', result)
            self.assertTrue(result['halt'])
            self.assertEqual(result['cancelled'], [])
            self.assertTrue((Path(directory) / 'HALT').is_file())

    def test_halt_write_failure_aborts_without_orders_and_alerts(self):
        from spotquant.cli import main
        with tempfile.TemporaryDirectory() as directory:
            config, venue = self._open(directory)
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': config.account_uid, 'state_dir': directory,
                'environment': 'demo', 'capital_limit_usdt': '1000',
                'session_seconds': 1, 'poll_seconds': 1,
            }), encoding='utf-8')
            sent = list(venue.sent)
            snapshots = []
            original = venue.snapshot

            def snapshot(uid):
                snapshots.append(uid)
                return original(uid)

            venue.snapshot = snapshot
            notes = []

            def connect(loaded, execute_orders=False):
                return venue

            def dispatch(directory, report, **kwargs):
                notes.append(report)
                return {'sent': [], 'skipped': 0, 'errors': []}

            with patch('spotquant.cli.connect', connect), \
                    patch('spotquant.ops._arm_halt', side_effect=OSError('halt directory is read only')), \
                    patch('spotquant.ops.dispatch_notifications', dispatch):
                code = main(['kill-switch', '--config', str(path), '--authorize-uid', config.account_uid,
                             '--confirm'])
            self.assertEqual(code, 2)
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0]['status'], 'unknown')
            self.assertIn('read only', notes[0]['reason'])
            self.assertTrue(notes[0]['manual_takeover'])
            self.assertEqual(venue.sent, sent)
            self.assertEqual(snapshots, [])
            self.assertFalse((Path(directory) / 'HALT').is_file())
            self.assertFalse(any(row.get('side') == 'SELL' and row.get('type') == 'MARKET'
                                 for row in venue.orders.values()))

    def test_docs_say_kill_switch_always_writes_halt_first(self):
        import contextlib
        from io import StringIO
        from spotquant.cli import main
        root = Path(__file__).resolve().parents[1]
        for name in ('README.md', 'deploy/README.md', 'AGENTS.md',
                     'spotquant/ops.py', 'spotquant/cli.py', 'spotquant/follow.py'):
            text = (root / name).read_text(encoding='utf-8')
            self.assertIn('kill-switch always writes HALT first', text, name)
        combined = '\n'.join((root / name).read_text(encoding='utf-8')
                              for name in ('README.md', 'deploy/README.md', 'AGENTS.md'))
        self.assertNotIn('查询失败或已经只剩尘埃时不写', combined)
        self.assertNotIn('还没进入撤单或卖出就因查询失败', combined)
        self.assertNotIn('查询还没到这一步就失败', combined)
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as caught:
                main(['kill-switch', '--help'])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('kill-switch always writes HALT first', buf.getvalue())
        top = StringIO()
        with contextlib.redirect_stdout(top):
            with self.assertRaises(SystemExit) as caught:
                main(['--help'])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('kill-switch always writes HALT first', top.getvalue())
