"""Configuration rejects anything outside one spot account."""
import json
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from spotquant.config import load
from spotquant.types import Blocked


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
