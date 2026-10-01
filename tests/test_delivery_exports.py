import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / 'evidence/complete-delivery-20261001/validate_exports.py'
spec = importlib.util.spec_from_file_location('delivery_exports', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeliveryExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        row = dict(complete=True, audit=dict(passed=True))
        self.spot = dict(results={f's{i}-{j}': row for i in range(5) for j in range(4)})
        self.perp = dict(results={f'p{i}': {str(j): row for j in range(4)} for i in range(7)})
        self.report = dict(inputs={}, candidate_attribution={}, qualification='NOT_QUALIFIED', native_execution_verified=False,
                           spot_selection=dict(selected_research_candidate='consensus'),
                           perp_selection=dict(selected_research_candidate='incumbent'))
        for kind, data in (('spot', self.spot), ('perp', self.perp)):
            blob = json.dumps(data).encode()
            (self.root / (kind + '.json')).write_bytes(blob)
            self.report['inputs'][kind] = hashlib.sha256(blob).hexdigest()
        self.report['candidate_attribution'] = {k: {} for k in
            ({'spot/' + k for k in self.spot['results']} |
             {'perp/' + k + '/' + j for k, rows in self.perp['results'].items() for j in rows})}
        for key, candidate in (('joint_fixed_capital', 'default'), ('joint_selected_fixed_capital', 'consensus')):
            self.report[key] = [dict(spot_initial_cny=b, perp_initial_cny=10000-b,
                spot_candidate=candidate, perp_candidate='incumbent',
                fixed_accounts_no_transfers=True, continuous_joint_mdd_verified=False,
                daily_equity_cny=[10000] * 2454, metrics=dict(final_cny=10000, daily_mdd=0, cagr=0))
                for b in (0, 2500, 5000, 7500, 10000)]

    def check(self):
        path = self.root / 'assessment.json'
        path.write_text(json.dumps(self.report))
        return module.validated(self.root / 'spot.json', self.root / 'perp.json', path)

    def test_complete_fixed_accounts_export_and_input_substitution_is_rejected(self):
        self.assertEqual(self.check()[2]['qualification'], 'NOT_QUALIFIED')
        (self.root / 'spot.json').write_text(json.dumps(dict(self.spot, altered=True)))
        with self.assertRaisesRegex(ValueError, 'input SHA'):
            self.check()

    def test_empty_joint_cannot_be_exported_as_complete_delivery(self):
        self.report['joint_selected_fixed_capital'] = []
        with self.assertRaisesRegex(ValueError, 'five-allocation'):
            self.check()

    def test_added_capital_and_strategy_mix_are_rejected(self):
        row = self.report['joint_selected_fixed_capital'][2]
        row['perp_initial_cny'] = 10000
        with self.assertRaisesRegex(ValueError, 'total CNY10000'):
            self.check()
        row['perp_initial_cny'] = 5000
        row['spot_candidate'] = 'default'
        with self.assertRaisesRegex(ValueError, 'identities differ'):
            self.check()

    def test_missing_daily_mark_and_forged_terminal_are_rejected(self):
        row = self.report['joint_fixed_capital'][0]
        row['daily_equity_cny'].pop()
        with self.assertRaisesRegex(ValueError, '2454'):
            self.check()
        row['daily_equity_cny'].append(10000)
        row['metrics']['final_cny'] = 20000
        with self.assertRaisesRegex(ValueError, 'metric differs'):
            self.check()
