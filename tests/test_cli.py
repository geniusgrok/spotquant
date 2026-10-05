"""Execution is refused before configuration, credentials, or the network."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from spotquant.cli import main
from spotquant.types import Unknown


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
            with patch('spotquant.cli.connect', side_effect=Unknown('no route')):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(['status', '--config', str(path)])
            self.assertEqual(code, 2)
            saved = json.loads((state / 'latest.json').read_text())
            self.assertEqual(saved['status'], 'unknown')
            self.assertIn('no route', saved['reason'])
            self.assertNotIn('model_bull', saved)
            self.assertFalse(saved['observation_current'])


if __name__ == '__main__':
    unittest.main()
