from unittest import TestCase
from unittest.mock import patch, Mock
from pathlib import Path
import tempfile

from research.operations import combined, observe, account_days, collect


def snapshot(market, qty):
    return {'known': True, 'market': market, 'account_uid': '1', 'environment': 'demo',
            'symbol': 'BTCUSDT', 'observed_at_ms': 1000000, 'btc_price_usdt': '100',
            'equity_usdt': '1000', 'btc_position': qty}


class OperationsTests(TestCase):
    def test_account_days_require_this_pair_and_thirty_actual_dates(self):
        base = {'collection': 'native_read_only_exporters', 'configured_accounts': ['pair'],
                'status': 'read_only', 'account_observed': True, 'recorded_date_utc': '2026-10-01'}
        records = [dict(base, recorded_date_utc=f'2026-09-{day:02d}') for day in range(2, 31)]
        self.assertTrue(account_days([*records, base], base)['thirty_day_observation_complete'])
        records[0]['account_observed'] = False
        records.append(dict(records[0], configured_accounts=['different pair'], account_observed=True))
        report = account_days([*records, base, base], base)
        self.assertEqual(report['days_with_valid_account_observation'], 29)
        self.assertEqual(report['missing_or_failed_days_last_30'], 1)
        self.assertFalse(report['thirty_day_observation_complete'])

    def test_second_exporter_launch_failure_cleans_first_and_records_failed_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text('{"account_uid":"1","environment":"demo"}')
            first = Mock()
            first.poll.return_value = None
            with patch('research.operations.subprocess.Popen', side_effect=[first, OSError('launch failed')]):
                report = collect(config, config, root, root / 'reports/failure.json')
            first.kill.assert_called_once()
            first.communicate.assert_called_once()
            self.assertEqual(report['status'], 'unknown')
            self.assertFalse(report['account_observed'])
            self.assertEqual(report['operations']['days_with_valid_account_observation'], 0)
            self.assertTrue((root / 'reports/failure.json').exists())

    def test_hedge_does_not_erase_gross_exposure_or_double_count_margin(self):
        row = combined([snapshot('spot', '10'), snapshot('perpetual', '-10')], 1000001)
        self.assertEqual(row['btc_net'], '0')
        self.assertEqual(row['btc_gross'], '20')
        self.assertEqual(row['equity_usdt'], '2000')
        self.assertFalse(row['new_risk_authorized'])

    def test_unknown_stale_and_mixed_valuation_refuse_summary(self):
        for override in ({'known': False}, {'observed_at_ms': 1}, {'btc_price_usdt': '101'}, {'environment': 'live'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                combined([snapshot('spot', '1'), dict(snapshot('perpetual', '1'), **override)], 1000001)

    def test_two_attempts_on_one_real_day_do_not_manufacture_two_forward_days(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('research.operations.time.time', return_value=1790856000), \
                patch('research.operations.source_identity', return_value={'dirty': False}), \
                patch('research.operations.load_daily', return_value=[(1790726400000,)]), \
                patch('research.operations.ledger', return_value={'through': '2026-09-30'}), \
                patch('research.operations.file_digest', return_value='fixture'):
            root = Path(directory)
            first = observe(root / 'market', root / 'state')
            second = observe(root / 'market', root / 'state')
            self.assertEqual(first['recorded_date_utc'], '2026-10-01')
            self.assertEqual(second['operations']['days_with_valid_observation'], 1)
            self.assertFalse(second['operations']['thirty_day_observation_complete'])
            self.assertEqual(len(list((root / 'state/observations').glob('*.json'))), 2)
