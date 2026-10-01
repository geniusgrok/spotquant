import hashlib
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from research.restore_btc import fetch


class RestoreTests(TestCase):
    def test_corrupt_existing_input_is_refused_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.zip'
            path.write_bytes(b'corrupt')
            Path(str(path) + '.CHECKSUM').write_text(hashlib.sha256(b'original').hexdigest() + ' archive.zip')
            with patch('research.restore_btc.urlopen') as request, self.assertRaises(ValueError):
                fetch(path, 'https://data.binance.vision/archive.zip')
            request.assert_not_called()
            self.assertEqual(path.read_bytes(), b'corrupt')
