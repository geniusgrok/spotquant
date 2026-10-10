"""Demo band probe: read-only preview, and an execute path that records msgs."""
import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal as D
from pathlib import Path

from spotquant.band_probe import execute_probe
from spotquant.cli import main
from spotquant.types import Blocked
from test_demo_verify import SpotScript, _config, _venue


class BandProbeTests(unittest.TestCase):
    def test_preview_reads_prices_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            script = SpotScript()
            script.percent_band = True
            venue = _venue(script)
            report = execute_probe(_config(directory), venue, execute=False)
            self.assertEqual(report['status'], 'read_only')
            self.assertFalse(report['write_attempted'])
            self.assertEqual(script.posts, [])
            self.assertEqual([row['stop_price'] for row in report['attempts']],
                             ['85.00', '80.00', '75.00', '72.00'])
            self.assertEqual(report['percent_price_by_side']['ask_multiplier_down'], D('0.8'))

    def test_live_config_is_refused_before_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'account_uid': '10001', 'state_dir': directory, 'environment': 'live',
            }))
            with unittest.mock.patch('spotquant.cli.connect', side_effect=AssertionError('connect')):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(['demo-band-probe', '--config', str(path)])
            self.assertEqual(code, 2)
            self.assertIn('environment is demo', json.loads(stdout.getvalue())['reason'])

    def test_execute_records_the_exchange_message_and_sells_the_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            script = SpotScript()
            script.min_stop = D('80')
            venue = _venue(script)
            report = execute_probe(_config(directory), venue, execute=True, sleep=lambda _seconds: None)
            self.assertEqual(report['status'], 'pass', report)
            self.assertFalse(report['unprotected'])
            self.assertEqual(report['cleanup']['status'], 'pass')
            by_depth = {row['depth']: row for row in report['attempts']}
            self.assertTrue(by_depth['0.15']['accepted'])
            self.assertTrue(by_depth['0.20']['accepted'])
            self.assertFalse(by_depth['0.25']['accepted'])
            self.assertEqual(by_depth['0.25']['msg'], 'Filter failure: PERCENT_PRICE_BY_SIDE')
            self.assertEqual(by_depth['0.25']['code'], -1013)
            self.assertFalse(by_depth['0.28']['accepted'])
            self.assertEqual(by_depth['0.28']['msg'], 'Filter failure: PERCENT_PRICE_BY_SIDE')
            self.assertEqual(script.btc_free, D('0'))
            self.assertEqual(script.btc_locked, D('0'))
            text = json.dumps(report, default=str)
            self.assertNotIn('test-secret', text)

    def test_execute_refuses_foreign_btc(self):
        with tempfile.TemporaryDirectory() as directory:
            script = SpotScript()
            script.btc_free = D('0.2')
            venue = _venue(script)
            report = execute_probe(_config(directory), venue, execute=True, sleep=lambda _seconds: None)
            self.assertEqual(report['status'], 'failed')
            self.assertIn('will not adopt', report['reason'])
            self.assertEqual(script.posts, [])
            self.assertEqual(script.btc_free, D('0.2'))
