from copy import deepcopy
from unittest import TestCase

from research.assemble_spot import assemble, row_digest
from research.complete_spot import CANDIDATES, SCENARIOS


class AssembleSpotTests(TestCase):
    def inputs(self):
        original = dict(source=dict(git_head='old', dirty=False), results={})
        for key in ('market_sha256', 'schedule_sha256', 'fx_sha256', 'crowding_sha256'):
            original[key] = key
        for candidate in CANDIDATES:
            for scenario in SCENARIOS:
                original['results'][candidate + '-' + scenario] = dict(
                    candidate=candidate, scenario=scenario, initial_cny='10000',
                    complete=candidate != 'consensus', audit=dict(passed=True),
                    sessions=[dict(start_ms=1, archive_verified=True)])
        replacement = dict(original, source=dict(git_head='new', dirty=False),
                           results={k: dict(v, complete=True) for k, v in original['results'].items()
                                    if k.startswith('consensus-')})
        proof = dict(input_bundles=[dict(sha256='original', source=original['source'], accounts=[
            dict(account=k, account_result_sha256=row_digest(v), zero_hit_reuse_candidate=not k.startswith('consensus-'))
            for k, v in original['results'].items()])])
        return original, replacement, proof

    def test_preserves_source_and_money_rows_and_excludes_failed_original(self):
        original, replacement, proof = self.inputs()
        report = assemble(original, replacement, proof, 'original', 'replacement', 'proof')
        self.assertEqual(report['results']['default-base'], original['results']['default-base'])
        self.assertTrue(report['results']['consensus-base']['complete'])
        self.assertFalse(original['results']['consensus-base']['complete'])
        self.assertEqual(report['account_sources']['default-base']['source']['git_head'], 'old')
        self.assertEqual(report['account_sources']['consensus-base']['source']['git_head'], 'new')
        self.assertFalse(report['production_promoted'])

    def test_rejects_wrong_bytes_missing_rerun_and_invalid_execution(self):
        for mutation in ('bytes', 'missing', 'incomplete', 'schedule', 'audit', 'inputs'):
            with self.subTest(mutation=mutation):
                original, replacement, proof = self.inputs()
                if mutation == 'bytes':
                    original['results']['default-base']['final_cny'] = 12345
                elif mutation == 'missing':
                    replacement['results'].pop('consensus-slip2')
                elif mutation == 'incomplete':
                    replacement['results']['consensus-base']['complete'] = False
                elif mutation == 'schedule':
                    replacement['results']['consensus-base']['sessions'] = []
                elif mutation == 'audit':
                    replacement = deepcopy(replacement)
                    replacement['results']['consensus-base']['audit']['passed'] = False
                elif mutation == 'inputs':
                    replacement['fx_sha256'] = 'other-fx'
                with self.assertRaises(ValueError):
                    assemble(original, replacement, proof, 'original', 'replacement', 'proof')
