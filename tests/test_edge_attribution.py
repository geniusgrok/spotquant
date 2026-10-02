import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import edge_attribution as a


class AttributionTests(unittest.TestCase):
    def spot(self, stop=False, sold='1', reason='sma', linked=True):
        buy = {'id': 1, 'order_id': 10, 'time': 100, 'qty': '1', 'quote': '100',
               'price': '100', 'buyer': True, 'commission': '0', 'commission_asset': 'BTC'}
        sell = dict(buy, id=2, order_id=11, time=200, qty=sold, quote=str(float(sold) * 110),
                    price='110', buyer=False)
        def allocation(order_id, side, kind):
            payload = {'order': {'side': side, 'type': kind, 'quantity': '1'},
                       'sleeves': [30], 'weights': {'30': '1'}}
            result = {'orderId': order_id, 'status': 'FILLED', 'executedQty': sold if side == 'SELL' else '1'}
            return ['client-' + str(order_id), json.dumps(payload), 'settled', json.dumps(result)]
        account = {'fills': [buy, sell], 'allocations': [allocation(10, 'BUY', 'MARKET'),
                    allocation(11, 'SELL', 'STOP_LOSS' if stop else 'MARKET')],
                   'daily': {'0': {'timestamp_ms': 50, 'price_usdt': '100', 'btc': '0'},
                             '1': {'timestamp_ms': 150, 'price_usdt': '105', 'btc': '1'},
                             '2': {'timestamp_ms': 250, 'price_usdt': '110', 'btc': str(1 - float(sold))},
                             '3': {'timestamp_ms': 86400250, 'price_usdt': '220', 'btc': str(1 - float(sold))}}}
        rich = {'opportunity_ledger': [dict(buy, event='fill', sleeves=[30]),
                dict(sell, event='fill', sleeves=[30], exit_type={'30': reason})] if linked else []}
        return account, rich

    def test_actual_stop_versus_sma_and_price_opportunity_is_not_profit(self):
        for stop, expected in ((True, 'stop'), (False, 'sma')):
            with self.subTest(stop=stop):
                result = a.spot_attribution(*self.spot(stop=stop))
                self.assertEqual(result['actual_sell_fills'][0]['sleeves'][0]['classification'], expected)
                flat = [r for r in result['flat_intervals'] if r['sleeve'] == 30 and r['classification'] == expected]
                self.assertEqual(len(flat), 1)
                self.assertEqual(flat[0]['price_change_diagnostic'], '1')
                self.assertIsNone(flat[0]['realizable_profit'])

    def test_partial_sell_does_not_create_flat_campaign(self):
        result = a.spot_attribution(*self.spot(sold='.4'))
        sale = result['actual_sell_fills'][0]['sleeves'][0]
        self.assertFalse(sale['flat_after'])
        self.assertEqual(sale['retained_btc'], '0.6')
        self.assertFalse(any(r['sleeve'] == 30 and r['classification'] == 'sma' for r in result['flat_daily']))

    def test_missing_fill_reason_or_owner_stays_unknown(self):
        account, rich = self.spot(linked=False)
        result = a.spot_attribution(account, rich)
        self.assertEqual(result['actual_sell_fills'][0]['sleeves'][0]['classification'], 'unknown')
        # Stop is proven by actual durable order type even without journal reasons.
        result = a.spot_attribution(*self.spot(stop=True, linked=False))
        self.assertEqual(result['actual_sell_fills'][0]['sleeves'][0]['classification'], 'stop')
        account['allocations'].pop()
        result = a.spot_attribution(account, rich)
        self.assertEqual(result['actual_sell_fills'][0]['classification'], 'unknown')
        self.assertTrue(all(r['classification'] == 'unknown' for r in result['flat_daily'] if r['timestamp_ms'] >= 250))

    def test_retained_dust_is_owned_and_not_exact_account_zero(self):
        result = a.spot_attribution(*self.spot(sold='.999999'))
        flat = next(r for r in result['flat_daily'] if r['sleeve'] == 30 and r['classification'] == 'sma')
        self.assertEqual(flat['retained_owned_btc'], '0.000001')
        self.assertFalse(flat['account_exactly_zero'])

    def test_extended_other_and_tampered_rich_fill(self):
        for reason, expected in (('extended', 'extended'), ('emergency', 'other'), (None, 'unknown')):
            result = a.spot_attribution(*self.spot(reason=reason))
            self.assertEqual(result['actual_sell_fills'][0]['sleeves'][0]['classification'], expected)
        account, rich = self.spot()
        rich['opportunity_ledger'][-1]['qty'] = '.9'
        with self.assertRaisesRegex(ValueError, 'rich fill differs'):
            a.spot_attribution(account, rich)

    def coin(self):
        return {'opportunity_ledger': [
            {'event': 'opportunity', 'identity': 100, 'at_ms': 100, 'close': '10'},
            {'event': 'decision', 'opportunity': 100, 'at_ms': 110, 'decision_mark': '10',
             'wallet_usdt': '100', 'quantity_before': '0', 'action': 'enter'},
            {'event': 'entry_sizing', 'opportunity': 100, 'at_ms': 120, 'desired_btc': '1.01', 'accepted_btc': '1'},
            {'event': 'write_attempt', 'opportunity': 100, 'at_ms': 130, 'identity': 'client', 'payload': {'quantity': '1'}},
            {'event': 'fill', 'client_order_id': 'client', 'at_ms': 140, 'trade': {'qty': '1', 'price': '10'}},
            {'event': 'opportunity', 'identity': 200, 'at_ms': 200, 'close': '11'},
            {'event': 'decision', 'opportunity': 200, 'at_ms': 210, 'decision_mark': '11',
             'wallet_usdt': '101', 'quantity_before': '1', 'action': 'hold'}],
            'daily': [{'stamp_ms': 150, 'equity_usdt': '101'}], 'sessions': []}

    def test_first_difference_and_later_equity_propagation_are_distinct(self):
        left, right = self.coin(), self.coin()
        right['opportunity_ledger'][1]['decision_mark'] = '10.1'
        right['daily'][0]['equity_usdt'] = '99'
        right['opportunity_ledger'][-1]['wallet_usdt'] = '99'
        result = a.coin_comparison(left, right)
        self.assertEqual(result['first_account_operational_divergence']['category'], 'different_observations')
        later = result['opportunities'][1]
        self.assertEqual(later['first_operational_divergence']['category'], 'prior_equity_propagation')
        self.assertTrue(later['prior_equity_already_different'])

    def test_same_opportunity_retains_raw_timing_numeric_and_rounding_differences(self):
        left, right = self.coin(), self.coin()
        right['opportunity_ledger'][1]['at_ms'] += 60
        right['opportunity_ledger'][2]['accepted_btc'] = '0.9'
        result = a.coin_comparison(left, right)
        first = result['opportunities'][0]
        self.assertIn('at_ms', first['first_exact_difference']['exact_differences'])
        self.assertEqual(first['first_operational_divergence']['category'], 'rounding')
        self.assertEqual(first['first_operational_divergence']['exact_differences']['accepted_btc']['right'], '0.9')
        self.assertFalse(result['unnecessary_dependency_established'])
        # Decimal-equivalent representation is still a raw numeric difference.
        self.assertTrue(a.differences({'qty': '1.0'}, {'qty': '1'}))

    def test_unlinked_coin_fill_does_not_inherit_nearest_opportunity(self):
        left, right = self.coin(), self.coin()
        right['opportunity_ledger'][4]['client_order_id'] = 'unknown'
        result = a.coin_comparison(left, right)
        self.assertEqual(len(result['unowned_fill_rows']['right']), 1)
        self.assertEqual(result['unowned_fill_rows']['right'][0]['ledger_index'], 4)

    def test_mixed_observations_and_prior_equity_remain_unisolated(self):
        left, right = self.coin(), self.coin()
        right['daily'][0]['equity_usdt'] = '99'
        right['opportunity_ledger'][-1].update(decision_mark='11.1', wallet_usdt='99')
        result = a.coin_comparison(left, right)['opportunities'][1]
        self.assertEqual(result['first_operational_divergence']['category'], 'different_observations')
        self.assertEqual(result['causal_isolation'], 'unknown')

    def test_post_execution_summary_cannot_precede_actual_cause(self):
        left, right = self.coin(), self.coin()
        left['opportunity_ledger'][1]['quantity_after'] = '1'
        right['opportunity_ledger'][1]['quantity_after'] = '.9'
        right['opportunity_ledger'][2]['accepted_btc'] = '.9'
        result = a.coin_comparison(left, right)['opportunities'][0]
        self.assertEqual(result['first_exact_difference']['event'], 'decision')
        self.assertEqual(result['first_operational_divergence']['event'], 'entry_sizing')

    def test_topup_exit_timeout_and_unavailable_event_categories(self):
        self.assertEqual(a.divergence_category('topup_sizing', {'entry_estimate'}), 'top_up')
        self.assertEqual(a.divergence_category('decision', {'action'},
                         ({'raw': {'action': 'exit'}},)), 'exit')
        self.assertEqual(a.divergence_category('cycle_blocked', {'reason'}), 'timeout_or_blocked_observation')
        self.assertEqual(a.divergence_category('fill', {''}), 'event_availability_unknown')

    def test_macro_context_pairing_retains_distinct_actual_identities(self):
        left, right = self.coin(), self.coin()
        for body, identity, stamp in ((left, -100, 100), (right, -160, 160)):
            body['opportunity_ledger'] = [
                {'event': 'opportunity', 'identity': identity, 'at_ms': stamp, 'kind': 'macro',
                 'direction': 1, 'dfii10': {'latest_value': '1', 'latest_observation_date': '2020-01-01'}},
                {'event': 'decision', 'opportunity': identity, 'at_ms': stamp + 10, 'decision_mark': '10'}]
        result = a.coin_comparison(left, right)['opportunities']
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]['actual_identity_equal'])
        self.assertEqual(result[0]['actual_identity_left'], -100)
        self.assertEqual(result[0]['actual_identity_right'], -160)
        self.assertIn('identity', result[0]['first_exact_difference']['exact_differences'])
        right['opportunity_ledger'][0]['dfii10']['latest_value'] = '1.0'
        self.assertEqual(len(a.coin_comparison(left, right)['opportunities']), 2)

    def test_hash_mismatch_duplicate_json_and_output_overwrite_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'input.json'
            path.write_text('{"qty":"1"}')
            with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
                a.read_json(path, '0' * 64)
            path.write_text('{"qty":1,"qty":2}')
            with self.assertRaisesRegex(ValueError, 'duplicate JSON'):
                a.read_json(path)
            with patch.object(a, 'generate') as generate:
                with self.assertRaisesRegex(ValueError, 'output already exists'):
                    a.main(['--evidence', root, '--out', root])
                generate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
