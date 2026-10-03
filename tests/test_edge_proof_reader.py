"""Retrospective proof/source boundary only; no financial accounts or network."""
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from research import edge_assessment as e
from research import edge_forward as f
from tests import test_edge_forward as fixtures


class RetrospectiveProofTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SourceAndExportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        for name in ('edge_spec.json', 'edge-PROTOCOL.md', 'alpha_beta_spec.json',
                     'alpha-beta-PROTOCOL.md', 'complete-delivery-PROTOCOL.md'):
            (self.repo / 'research' / name).write_bytes((e.ROOT / 'research' / name).read_bytes())
        self.fixture.commit()
        self.original = f.source(self.repo)
        # Stand in for the separately reviewed canonical promotion.
        (self.repo / 'spotquant/rule.py').write_text('RULE = 2\n')
        self.fixture.commit()
        self.current = f.source(self.repo)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(e, 'ROOT', self.repo).start()
        patch.object(e.old, 'ROOT', self.repo).start()

    def write(self, name, body):
        path = self.data / name
        path.write_bytes(f.dump(body))
        return path

    def proof_fixture(self):
        # Reuse the tiny six-category export fixture, never the account producer.
        body = self.fixture.export()
        report = f.strict(f.decode(body['analysis_raw']))
        report['analysis_source'] = self.original
        for account in report['accounts'].values():
            account['source'] = self.original
        inventory = e.old.checksum(e.inventory_binding(report))
        report['inventory_sha256'] = inventory
        prior = dict(report, phase='preliminary', status='complete_pending_independent_review')
        prior.pop('financial_review')
        prior_path = self.write('prior.json', prior)
        checks = {}
        for category in f.CATEGORIES:
            artifact = dict(category=category, inventory_sha256=inventory,
                            recomputations=list(body['expected'][category].values()))
            path = self.write(category, artifact)
            checks[category] = dict(passed=True, artifact=dict(path=path.name, sha256=e.sha(path)))
        proof = dict(format='btc-edge-financial-review-v1', inventory_sha256=inventory,
                     checks=checks, reviewer_source=self.original,
                     preliminary=dict(path=prior_path.name, sha256=e.sha(prior_path)))
        proof_path = self.write('review.json', proof)
        report['financial_review'] = dict(sha256=e.sha(proof_path))
        analysis_path = self.write('analysis.json', report)
        bridge = f.strict(f.decode(body['bridges_raw']))['spot']
        bridge['measured_source'] = self.original
        for case in bridge['canonical_accounts'].values():
            case['measured_source'] = self.original
        bridge.pop('canonical_review')
        canonical = dict(format='btc-edge-canonical-forward-bridge-v1',
                         status='independently_reviewed', project='spot', bridge=copy.deepcopy(bridge))
        canonical_path = self.write('canonical.json', canonical)
        bridge['canonical_review'] = dict(path=canonical_path.name, sha256=e.sha(canonical_path))
        bridges_path = self.write('bridges.json', {'spot': bridge})
        return analysis_path, proof_path, bridges_path, body['expected']

    def test_recorded_archive_validates_digest_objects_and_contracts(self):
        files = e._verify_recorded_source(self.original, 'spot')
        for name in ('alpha_beta_spec.json', 'alpha-beta-PROTOCOL.md',
                     'edge_spec.json', 'edge-PROTOCOL.md', 'complete-delivery-PROTOCOL.md'):
            self.assertEqual(files['research/' + name], e.sha(self.repo / 'research' / name))
        for field, value in (('dirty', True), ('python_sources_sha256', '0' * 64)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                e._verify_recorded_source(dict(self.original, **{field: value}), 'spot')
        with self.assertRaises(subprocess.CalledProcessError):
            e._verify_recorded_source(dict(self.original, git_head='0' * 40), 'spot')
        for name in ('edge_spec.json', 'edge-PROTOCOL.md'):
            path = self.repo / 'research' / name
            saved = path.read_bytes()
            path.write_text('tampered contract\n')
            self.fixture.commit()
            with self.subTest(contract=name), self.assertRaisesRegex(ValueError, 'contract'):
                e._verify_recorded_source(f.source(self.repo), 'spot')
            path.write_bytes(saved)
        # A later archive change cannot silently substitute for the recorded digest.
        with self.assertRaisesRegex(ValueError, 'digest'):
            e._verify_recorded_source(dict(self.original, git_head=self.current['git_head']), 'spot')

    def test_current_producer_mismatch_still_rejects_at_ingestion(self):
        with self.assertRaisesRegex(ValueError, 'current frozen producer'):
            e.verify_source(self.original, 'spot')
        with self.assertRaisesRegex(ValueError, 'current frozen producer'):
            e.envelope(dict(edge={}, source=self.original), 'spot', {}, {})
        self.assertTrue(e.verify_source(self.current, 'spot'))

    def test_retrospective_export_preserves_sources_and_rejects_bad_bindings(self):
        analysis, proof, bridges, expected = self.proof_fixture()
        original_bytes = {p: p.read_bytes() for p in self.data.iterdir()}
        out = self.data / 'synthetic-export.json'
        actual_source = f.source
        # Only economic expectation derivation is stubbed: the real reader checks
        # all six exact typed artifacts, original Git archive and canonical bridge.
        with patch.object(e, 'review_expectations', return_value=expected), \
                patch.object(f, 'ROOT', self.repo), \
                patch.object(f, 'source', side_effect=lambda root=None, head=None:
                             actual_source(self.repo if root is None else root, head)):
            digest = f.export_binding(analysis, proof, bridges, out)
            binding, raw = f.validate_export(out, digest, e.sha(proof), root=self.repo)
            exported = f.strict(raw)
            self.assertEqual(f.strict(f.decode(exported['review_raw']))['reviewer_source'], self.original)
            self.assertEqual(binding['bridge']['measured_source'], self.original)
            self.assertEqual(binding['current_source'], self.current)
            self.assertNotEqual(self.original['python_sources_sha256'], self.current['python_sources_sha256'])
            for path, saved in original_bytes.items():
                self.assertEqual(path.read_bytes(), saved)

            # A rehashed artifact still must match the exact typed review value.
            review = f.strict(proof.read_bytes())
            category = 'original_accounting'
            artifact_path = self.data / category
            artifact = f.strict(artifact_path.read_bytes())
            artifact['recomputations'][0]['values']['mdd'] = '.30'
            self.write(category, artifact)
            review['checks'][category]['artifact']['sha256'] = e.sha(artifact_path)
            self.write(proof.name, review)
            with self.assertRaisesRegex(ValueError, 'recomputation mismatch'):
                f.export_binding(analysis, proof, bridges, self.data / 'bad-proof.json')
            proof.write_bytes(original_bytes[proof]); artifact_path.write_bytes(original_bytes[artifact_path])

            bad = f.strict(bridges.read_bytes())
            bad['spot']['source'] = self.original
            self.write(bridges.name, bad)
            with self.assertRaisesRegex(ValueError, 'current source mismatch'):
                f.export_binding(analysis, proof, bridges, self.data / 'bad-bridge.json')
            bridges.write_bytes(original_bytes[bridges])
            # Even a frozen valid export cannot authorize a different current tree.
            (self.repo / 'spotquant/rule.py').write_text('RULE = 3\n')
            self.fixture.commit()
            with self.assertRaisesRegex(ValueError, 'protected source changed'):
                f.validate_export(out, digest, e.sha(proof), root=self.repo)
        self.assertFalse((self.data / 'bad-proof.json').exists())
        self.assertFalse((self.data / 'bad-bridge.json').exists())
