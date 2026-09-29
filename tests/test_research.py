"""The research loader reads any symbol, and the forward ledger pins its rules."""
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from research.forward import ROOT, rules_sha256
from research.market import file_digest, load_daily
from spotquant.model import DAY, ORIGIN


def _write(directory: Path, symbol: str, days: int, name: str):
    base = directory / '1d'
    base.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(days):
        open_ms = ORIGIN + index * DAY
        rows.append(f'{open_ms},10,12,9,11,1,{open_ms + DAY - 1},100,1,1,1,0')
    with zipfile.ZipFile(base / f'{symbol}-1d-{name}.zip', 'w') as archive:
        archive.writestr(f'{symbol}-1d-{name}.csv', '\n'.join(rows))


class ResearchTests(unittest.TestCase):
    def test_the_loader_reads_the_named_symbol_and_extra_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / 'eth', 'ETHUSDT', 3, '2019-01')
            _write(root / 'btc', 'BTCUSDT', 5, '2019-01')
            eth = load_daily(root / 'eth', ORIGIN + 10 * DAY, 'ETHUSDT')
            self.assertEqual(len(eth), 3)
            with self.assertRaises(FileNotFoundError):
                load_daily(root / 'eth', ORIGIN + 10 * DAY, 'BTCUSDT')
            self.assertNotEqual(file_digest(root / 'eth', 'ETHUSDT'), file_digest(root / 'btc', 'BTCUSDT'))
            self.assertEqual(eth[0][1:], (D(10), D(12), D(9), D(11), D(100)))
            with self.assertRaises(ValueError):
                load_daily(root / 'eth', ORIGIN + 10 * DAY, 'ETHUSDT', require_through=ORIGIN + 10 * DAY)

    def test_the_forward_rules_hash_is_the_one_in_the_spec(self):
        spec = json.loads((ROOT / 'research' / 'spec.json').read_text(encoding='utf-8'))
        self.assertEqual(spec['forward']['rules_sha256'], rules_sha256())
        self.assertEqual(spec['model']['sleeves'], [30, 40, 50])


if __name__ == '__main__':
    unittest.main()
