"""Execution is refused before configuration, credentials, or the network."""
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spotquant.cli import main


class CliTests(unittest.TestCase):
    def test_execute_is_blocked_before_config_is_opened(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['run', '--execute', '--config', '/no/such/spotquant-config.json'])
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('execution unavailable', report['reason'])
        self.assertNotIn('configuration', report['reason'])


if __name__ == '__main__':
    unittest.main()
