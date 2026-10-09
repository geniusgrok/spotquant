"""Current entry sizing keeps unavailable/future public data away from new risk."""
from decimal import Decimal as D
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from spotquant import crowding as c, preview
from spotquant.model import DAY, ORIGIN, Model, SLEEVES
from spotquant.session import _view
from spotquant.types import floor_step


class Features:
    def __init__(self, funding='.0004', basis='.02'):
        self.funding, self.basis = funding, basis

    def value(self, name, now):
        value = self.funding if name == 'funding' else self.basis
        boundary = now // DAY * DAY
        self.last_lookup = dict(name=name, value=value, cause='unavailable' if value is None else None,
                                observation_ms=now - c.FUNDING_LAG if name == 'funding' else boundary,
                                available_ms=now if name == 'funding' else boundary + c.BASIS_LAG)
        return D(value) if value is not None else None


def raw(body):
    return json.dumps(body, sort_keys=True, separators=(',', ':')).encode()


class CrowdingTests(unittest.TestCase):
    def setUp(self):
        self.views = {}
        for window in SLEEVES:
            model = Model(window)
            for index, price in enumerate([D(90)] * 400 + [D(100), D(100)]):
                model.update(ORIGIN + index * DAY, price, price, price)
            model.closes[-6] = model.close
            self.views[window] = model
        self.now = self.views[SLEEVES[0]].last + DAY + 120000
        self.owned = {w: D(0) for w in SLEEVES}
        self.snap = dict(usdt_free='1000', btc='0', avg_price='100', open_orders=0)
        self.kw = dict(positions={}, owners={}, entries_enabled=True, capital_limit=D(10000))

    def decide(self, features=None, **kw):
        return preview.decision(self.views, self.owned, self.snap, **dict(self.kw, **kw),
                                crowding_source=features, decision_ms=self.now)

    def test_crowded_new_buy_is_halved_once_and_thresholds_are_strict(self):
        full = self.decide(Features('.0001', '.001'))
        crowded = self.decide(Features())
        self.assertTrue(full['orders'])
        self.assertEqual(D(crowded['orders'][0]['quoteOrderQty']),
                         floor_step(D(full['orders'][0]['quoteOrderQty']) / 2, preview.QUOTE_STEP))
        self.assertEqual(crowded['protections'], full['protections'])
        for funding, basis in [('.0003', '.02'), ('.0004', '.01'), ('-.0001', '.02')]:
            with self.subTest(funding=funding, basis=basis):
                self.assertEqual(self.decide(Features(funding, basis))['orders'], full['orders'])
        self.views[40].closes[-6] -= 1
        self.assertEqual(self.decide(Features())['orders'], full['orders'])

    def test_missing_blocks_new_buy_but_keeps_safety_exit_and_native_stop_floor(self):
        self.assertEqual(self.decide()['orders'], [])
        self.assertEqual(self.decide(Features(funding=None))['orders'], [])
        self.owned[40] = D(1)
        self.views[40], _ = _view(self.views[40], dict(entry_fill='100', peak='100', qty='1',
                                                    repair=False, repair_peak=None, adverse=False))
        self.views[40].bull = False
        self.snap['btc'] = '1'
        position = dict(qty='1', peak='100', first_ms=self.views[40].last, repair=False)
        owner = dict(sleeves=[40], signal_ms=self.views[40].last, native_status='NEW',
                     position_first_ms={'40': position['first_ms']},
                     order=dict(type='STOP_LOSS', stopPrice='99'))
        missing = self.decide(None, positions={40: position}, owners={'stop': owner})
        present = self.decide(Features(), positions={40: position}, owners={'stop': owner})
        self.assertTrue(any(o['side'] == 'SELL' for o in missing['orders']))
        self.assertEqual(missing['orders'], present['orders'])
        self.assertEqual(preview.decision_view(self.views[40], position, {'stop': owner})._stop_floor, D(99))

    def test_expiry_availability_and_forming_day_block_new_risk(self):
        stamp = self.now - c.FUNDING_LAG + 17
        record = dict(observation_ms=stamp, available_ms=stamp + c.FUNDING_LAG, value='-.0004')
        self.assertEqual(c.value_at('funding', record, stamp + c.FUNDING_LAG - 1)[1], 'not_yet_available')
        self.assertEqual(c.value_at('funding', record, stamp + c.FUNDING_LAG)[0], D('-.0004'))
        self.assertEqual(c.value_at('funding', record, stamp + 2 * c.FUNDING_LAG)[1], 'stale_funding')
        day = self.now // DAY * DAY
        record = dict(observation_ms=day, available_ms=day + 60000, value='.02')
        self.assertEqual(c.value_at('basis', record, day + 59999)[1], 'not_yet_available')
        self.assertEqual(c.value_at('basis', record, day + DAY)[1], 'basis_availability_date_mismatch')
        self.views[40].last = self.now
        self.assertEqual(self.decide(Features())['orders'], [])

    def observations(self):
        day = self.now // DAY * DAY
        def bar(start, close):
            return [start, close, close, close, close, '1', start + DAY - 1, '1', 1, '1', '1', '0']
        bodies = {'funding': [{'symbol': 'BTCUSDT', 'fundingTime': self.now - c.FUNDING_LAG,
                               'fundingRate': '.0004'}],
                  'spot_bars': [bar(day - DAY, '100'), bar(day, '999999')],
                  'futures_bars': [bar(day - DAY, '102'), bar(day, '1')]}
        return [dict(category=k, url=c.PUBLIC_URLS[k], request_ms=self.now - 20,
                     receipt_ms=self.now - 10, sha256=hashlib.sha256(raw(body)).hexdigest(), body=body)
                for k, body in bodies.items()]

    def test_future_conflicting_or_malformed_observations_are_unavailable(self):
        source = c.PublicFeatures(self.observations())
        self.assertEqual(source.value('funding', self.now), D('.0004'))
        self.assertEqual(source.value('basis', self.now), D('.02'))
        self.assertIsNone(c.PublicFeatures(self.observations()[:-1]).value('basis', self.now))
        for mutate in (lambda rows: rows[0].update(receipt_ms=self.now + 1),
                       lambda rows: rows[0]['body'].append(dict(rows[0]['body'][0], fundingRate='.1')),
                       lambda rows: rows[0]['body'][0].update(fundingTime=self.now + 1)):
            rows = self.observations()
            mutate(rows)
            self.assertIsNone(c.PublicFeatures(rows).value('funding', self.now))
        rows = self.observations()
        rows[2]['body'][0][6] -= 1
        self.assertIsNone(c.PublicFeatures(rows).value('basis', self.now))
        for malformed in (b'<html>unavailable</html>', b'{"value":NaN}', b'\xff', b'{"value":1,"value":2}'):
            rows = self.observations()
            rows[0].update(c.public_body(malformed))
            self.assertEqual(self.decide(c.PublicFeatures(rows))['orders'], [])

    def test_collection_reuses_one_snapshot_and_honors_deadline(self):
        bodies = {r['url']: raw(r['body']) for r in self.observations()}
        class Response(io.BytesIO):
            def __init__(self, url):
                super().__init__(bodies[url])
                self.url = url
            def geturl(self):
                return self.url
        source = c.PublicFeatures()
        with patch.object(c, 'urlopen', side_effect=lambda url, timeout: Response(url)) as opener, \
                patch.object(c.time, 'time_ns', return_value=(self.now - 1) * 1000000):
            source.refresh(self.now - 1)
            self.assertEqual(source.value('basis', self.now), D('.02'))
            self.assertEqual(source.value('funding', self.now), D('.0004'))
            source.refresh(self.now)
            self.assertEqual(opener.call_count, 3)
        with patch.object(c, 'urlopen') as opener, patch.object(c.time, 'time_ns', return_value=self.now * 1000000):
            source.refresh(self.now + 60000, lambda: True)
            opener.assert_not_called()
            self.assertIsNone(source.value('funding', self.now + 60000))
