"""Configuration, command execution gates and account export stay offline."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

from spotquant.cli import main
from spotquant.config import Config, load
from spotquant.crowding import RULE
from spotquant.snapshot import export
from spotquant.types import Blocked, Unknown


class CliTests(unittest.TestCase):
    def test_execute_is_blocked_before_config_is_opened(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['run', '--execute', '--config', '/no/such/spotquant-config.json'])
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('execution is unavailable', report['reason'])
        self.assertNotIn('configuration', report['reason'])

    def test_status_persists_a_failed_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': '10001',
                'state_dir': str(state),
                'environment': 'demo',
            }))
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('spotquant.cli.connect', side_effect=Unknown('no route')), \
                    patch.dict('os.environ', {'SPOTQUANT_SOURCE_SHA': 'test-source-sha'}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(['status', '--config', str(path)])
            self.assertEqual(code, 2)
            saved = json.loads((state / 'latest.json').read_text())
            self.assertEqual(saved['status'], 'unknown')
            self.assertIn('no route', saved['reason'])
            self.assertNotIn('model_bull', saved)
            self.assertFalse(saved['observation_current'])
            self.assertEqual(saved['runtime_identity'], {'rule': RULE, 'source_sha': 'test-source-sha'})

    def test_snapshot_totals_partial_native_protection_and_rejects_stale_collection(self):
        snapshot = dict(usdt_free=D(20), usdt_locked=D(5), btc=D(2), other_assets=[], orders=[
            dict(side='SELL', type='STOP_LOSS', status='PARTIALLY_FILLED', stop_price='90',
                 orig_qty='1.5', executed_qty='.5')])
        ticks = iter((1, 2))
        venue = SimpleNamespace(clock=lambda: next(ticks), snapshot=lambda uid: snapshot,
                                _get=lambda *args, **kw: {'price': '100'})
        config = Config('10001', '/unused', environment='demo')
        report = export(config, venue)
        self.assertEqual(D(report['equity_usdt']), D(225))
        self.assertEqual(D(report['native_stop_quantity_btc']), D(1))
        self.assertEqual(D(report['btc_without_native_stop']), D(1))
        self.assertFalse(report['write_attempted'])
        ticks = iter((1, 7))
        with self.assertRaises(Unknown):
            export(config, venue)


class ConfigTests(unittest.TestCase):
    def test_rejects_unknown_and_empty_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': '10001',
                'state_dir': directory,
                'leverage': 20,
            }), encoding='utf-8')
            with self.assertRaises(Blocked):
                load(path)
            path.write_text(json.dumps({'account_uid': '', 'state_dir': directory}), encoding='utf-8')
            with self.assertRaises(Blocked):
                load(path)
            path.write_text(json.dumps({
                'account_uid': '10001',
                'state_dir': directory,
                'environment': 'demo',
                'capital_limit_usdt': '250.50',
            }), encoding='utf-8')
            config = load(path)
            self.assertEqual(config.scope, 'binance:BTCUSDT:spot:demo:10001')
            self.assertEqual(config.capital_limit, D('250.50'))
            self.assertEqual(config.session_seconds, 300)
            self.assertEqual(config.poll_seconds, 5)


if __name__ == '__main__':
    unittest.main()
