import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.native_acceptance import check, digest, verify_case


class NativeEvidenceTests(unittest.TestCase):
    def fixture(self, root):
        package = root / 'spotquant'
        package.mkdir()
        (package / 'session.py').write_text('execution = 1\n')
        manifest = {'project': 'spotquant', 'account_uid': '42', 'environment': 'demo',
                    'capital_limit_usdt': '100', 'execution_code_sha256': digest(package), 'cases': {}}
        record = {k: manifest[k] for k in ('project', 'account_uid', 'environment', 'execution_code_sha256')}
        record.update(origin='owner_captured_binance_demo', synthetic=False,
                      events=[{'observed_at_ms': 1000, 'endpoint': '/api/v3/order',
                               'response': {'symbol': 'BTCUSDT', 'orderId': 10, 'status': 'FILLED',
                                            'side': 'BUY', 'executedQty': '0.01'}}])
        return package, manifest, record

    def test_raw_bytes_source_and_environment_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, manifest, record = self.fixture(root)
            case = root / 'entry.json'
            case.write_text(json.dumps(record))
            manifest['cases']['entry'] = {'file': case.name, 'sha256': hashlib.sha256(case.read_bytes()).hexdigest()}
            path = root / 'manifest.json'
            path.write_text(json.dumps(manifest))
            self.assertTrue(check(path, package)['cases']['entry']['structural_evidence_present'])
            self.assertFalse(check(path, package)['ready_for_owner_native_review'])
            self.assertFalse(check(path, package)['native_execution_verified'])
            case.write_text('{}')
            self.assertFalse(check(path, package)['cases']['entry']['structural_evidence_present'])
            (package / 'session.py').write_text('execution = 2\n')
            self.assertIn('execution code changed or digest missing', check(path, package)['failures'])

    def test_synthetic_and_early_trigger_do_not_establish_native_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, manifest, record = self.fixture(Path(temporary))
            record['synthetic'] = True
            with self.assertRaises(ValueError):
                verify_case('entry', record, manifest)
            record['synthetic'] = False
            record['session_ended_at_ms'] = 1001
            record['events'][0]['response'].update(side='SELL', type='STOP_LOSS', updateTime=999)
            with self.assertRaises(ValueError):
                verify_case('stopped_trigger', record, manifest)

    def test_partial_fill_needs_native_quantity_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, manifest, record = self.fixture(Path(temporary))
            record['events'][0]['response']['status'] = 'PARTIALLY_FILLED'
            record['observed_position_btc'] = '0.02'
            record['events'].append({'observed_at_ms': 1001, 'endpoint': '/api/v3/order',
                                     'response': {'symbol': 'BTCUSDT', 'orderId': 11, 'side': 'SELL',
                                                  'status': 'NEW', 'type': 'STOP_LOSS', 'origQty': '0.01'}})
            with self.assertRaises(ValueError):
                verify_case('partial_protection', record, manifest)
            record['events'][1]['response']['origQty'] = '0.02'
            verify_case('partial_protection', record, manifest)

    def test_symlink_case_cannot_leave_the_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, manifest, record = self.fixture(root)
            original = root / 'original.json'
            original.write_text(json.dumps(record))
            link = root / 'entry.json'
            link.symlink_to(original)
            manifest['cases']['entry'] = {'file': link.name, 'sha256': hashlib.sha256(original.read_bytes()).hexdigest()}
            path = root / 'manifest.json'
            path.write_text(json.dumps(manifest))
            self.assertFalse(check(path, package)['cases']['entry']['structural_evidence_present'])

    def test_repeated_or_canceled_readbacks_cannot_inflate_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, manifest, record = self.fixture(Path(temporary))
            record['events'][0]['response']['status'] = 'PARTIALLY_FILLED'
            record['observed_position_btc'] = '0.02'
            stop = {'symbol': 'BTCUSDT', 'orderId': 11, 'side': 'SELL',
                    'status': 'NEW', 'type': 'STOP_LOSS', 'origQty': '0.01'}
            for stamp in (1001, 1002):
                record['events'].append({'observed_at_ms': stamp, 'endpoint': '/api/v3/order', 'response': dict(stop)})
            with self.assertRaises(ValueError):
                verify_case('partial_protection', record, manifest)
            record['observed_position_btc'] = '0.01'
            verify_case('partial_protection', record, manifest)
            record['events'].append({'observed_at_ms': 1003, 'endpoint': '/api/v3/order',
                                     'response': dict(stop, status='CANCELED')})
            with self.assertRaises(ValueError):
                verify_case('partial_protection', record, manifest)

    def test_reserved_manifest_filename_cannot_overwrite_archive(self):
        from research.native_acceptance import safe_file
        with tempfile.TemporaryDirectory() as temporary:
            for name in ('manifest.json', 'acceptance.json'):
                with self.assertRaises(ValueError):
                    safe_file(Path(temporary), name)
