"""Necessary ownership, causal reason, protection and recovery boundaries.

Pure fills/preview fixtures are software checks, not independent account or
native execution evidence. The root runs these once with the final full suite.
"""
from decimal import Decimal as D
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from research import edge_spot as edge
from research.reason_exit import SMA_REASON, _prepare, configured, model_for
from spotquant import execution, session
from spotquant.model import DAY, ORIGIN
from spotquant.state import State
from spotquant.types import Blocked, Unknown
from spotquant.types import floor_step


def warm(cls, window=30, prices=None):
    model = cls(window)
    for i, price in enumerate(prices or ([D(90)] * 400 + [D(100), D(101)])):
        model.update(ORIGIN + i * DAY, price, price, price)
    return model


def position(model, quantity='1.000005'):
    return dict(qty=quantity, first_ms=model.last - DAY + 60000,
        entry_open_ms=model.last - DAY, entry_fill='100', peak='100', repair=False,
        repair_peak=None, adverse=False, through=model.last, protection='resting', sell_applied={})


def owner_for(model, *, kind='MARKET', cause='sma-trend-exit', half=False,
              intended='1', executed='1', status='FILLED'):
    item = position(model)
    return dict(sleeves=[30], weights={'30': item['qty']},
        order=dict(side='SELL', type=kind, quantity=intended),
        signal_ms=model.last, native_status=status, native_executed_qty=executed,
        reason_exit=dict(expression='sma-half-hold' if half else 'reason-stop-reentry',
            sleeves={'30':dict(reason=SMA_REASON, cause=cause, half=half,
                first_ms=item['first_ms'], owned_before=item['qty'], requested_quantity=intended)}))


def sale(model, qty='1', identity=1, elapsed=1000):
    return dict(id=identity, order_id=7, buyer=False, time=model.last + DAY + elapsed,
                qty=D(qty), price=D(100), commission=D(0), commission_asset='USDT')


class ReasonExitBoundaries(unittest.TestCase):
    def scope(self, expression, venue=None):
        return configured(expression, venue=venue or SimpleNamespace(offline=True, now_ms=ORIGIN+500*DAY),
                          features=None, binding=dict(specification_sha256='b'*64))

    def test_stop_reason_is_native_owned_terminal_and_keeps_fixed_reentry_conditions(self):
        for kind, cause, allowed in (('STOP_LOSS', 'unknown', True),
                ('MARKET', 'sma-trend-exit', False), ('MARKET', 'unknown', False),
                ('MARKET', 'safety-stop-through', False)):
            with self.subTest(kind=kind, cause=cause), self.scope('reason-stop-reentry'):
                model = warm(session.Model)
                own = owner_for(model, kind=kind, cause=cause)
                session.apply_day({30:model}, {30:position(model)}, {30:None}, set(),
                    model.last+DAY, [sale(model)], lambda: [], owners={'7':own})
                self.assertEqual(model.exit_memory['cause'], 'native-stop-loss' if kind == 'STOP_LOSS' else cause)
                cls = session.Model
                model = cls.restore(model.checkpoint())
                for i in range(7):
                    close = D(102+i)
                    model.update(model.last+DAY, close, close, close)
                    if i < 6:
                        self.assertFalse(model.enter)
                self.assertEqual(model.enter, allowed)

    def test_stop_reentry_cannot_bridge_an_intervening_broken_trend(self):
        cls = model_for('reason-stop-reentry')
        model = warm(cls)
        model.note_flat()
        model.exit_memory = dict(cause='native-stop-loss', completed_day=model.last,
            trend_intact=True, first_ms=model.last-DAY, fill_ms=model.last+DAY+1000,
            order_id=7, original_preview_reason=None)
        model.update(model.last+DAY, D(50), D(50), D(50))
        self.assertFalse(model.exit_memory['trend_intact'])
        self.assertFalse(cls.restore(model.checkpoint()).exit_memory['trend_intact'])

    def test_unknown_or_partial_full_close_never_creates_a_reason_marker(self):
        for status, qty in (('NEW','1'), ('PARTIALLY_FILLED','.5'), ('EXPIRED','.5')):
            with self.subTest(status=status), self.scope('reason-stop-reentry'):
                model = warm(session.Model)
                own = owner_for(model, kind='STOP_LOSS', status=status, executed=qty)
                session.apply_day({30:model}, {30:position(model)}, {30:None}, set(),
                    model.last+DAY, [sale(model, qty)], lambda: [], owners={'7':own})
                self.assertIsNone(model.exit_memory)

    def test_close_timestamp_is_last_allocated_fill_and_owned_dust_is_retained(self):
        with self.scope('reason-stop-reentry') as selected:
            model = warm(session.Model)
            trades = [sale(model, '.4', 1, 1000), sale(model, '.6', 2, 2000)]
            result = session.apply_day({30:model}, {30:position(model)}, {30:None}, set(),
                model.last+DAY, trades, lambda: [], owners={'7':owner_for(model, kind='STOP_LOSS')})
            self.assertEqual(model.exit_memory['fill_ms'], trades[-1]['time'])
            self.assertEqual(result[0][30]['qty'], '0.000005')
            self.assertTrue(result[0][30]['dust'])
            self.assertEqual(len(selected.policy.journal), 1)

    def test_half_marker_requires_folded_terminal_execution_and_keeps_actual_remainder(self):
        for status, filled, marked in (('FILLED','.5',True), ('EXPIRED','.2',True),
                ('PARTIALLY_FILLED','.2',False), ('NEW','.2',False)):
            with self.subTest(status=status), self.scope('sma-half-hold'):
                model = warm(session.Model)
                own = owner_for(model, half=True, intended='.5', executed=filled, status=status)
                result = session.apply_day({30:model}, {30:position(model)}, {30:None}, set(),
                    model.last+DAY, [sale(model, filled)], lambda: [], owners={'7':own})
                self.assertEqual(D(result[0][30]['qty']), D('1.000005')-D(filled))
                self.assertFalse(result[0][30].get('dust', False))
                self.assertEqual(model.half_memory is not None, marked)
                if marked:
                    restored = session.Model.restore(model.checkpoint())
                    self.assertEqual(restored.half_memory['executed_group_quantity'], filled)
                self.assertIsNone(model.exit_memory)

    def test_half_intent_matches_actual_rounded_quantity_and_never_repeats_while_held(self):
        with self.scope('sma-half-hold') as selected:
            models = {w:warm(session.Model, w, [D(110)]*400+[D(99)]) for w in session.SLEEVES}
            positions = {w:position(m) for w,m in models.items()}
            views = {w:session._view(m, positions[w])[0] for w,m in models.items()}
            owned = {w:D(p['qty']) for w,p in positions.items()}
            snap = dict(btc=str(sum(owned.values())), usdt_free='100', usdt_locked='0',
                        avg_price='99', open_orders=0)
            def decide(v=views, p=positions, o=owned, owners=None):
                return selected.policy(v, o, snap, positions=p, owners=owners or {},
                    entries_enabled=True, capital_limit=D(1000))
            first = decide()
            sells = [o for o in first['orders'] if o['side']=='SELL']
            sell = sells[0]
            self.assertEqual(sum(D(o['quantity']) for o in sells), D('1.5'))
            self.assertTrue(all(len(o['sleeves']) == 1 for o in sells))
            self.assertTrue(all(item['half'] for o in sells for item in o['reason_exit']['sleeves'].values()))
            self.assertEqual(selected.policy.journal[-2]['accepted_orders'][0]['quantity'], sell['quantity'])
            for w in models:
                models[w].half_memory = dict(first_ms=positions[w]['first_ms'], fill_ms=models[w].last+DAY+1000,
                    order_id=7, executed_group_quantity='1.5')
            held_views = {w:session._view(m, positions[w])[0] for w,m in models.items()}
            held = decide(held_views)
            self.assertFalse(any(o['side']=='SELL' for o in held['orders']))
            self.assertFalse(any(o['side']=='BUY' for o in held['orders']))
            self.assertEqual(sum(D(o['quantity']) for o in held['protections']),
                             sum(floor_step(q, D('.00001')) for q in owned.values()))
            # A known native floor through the mark overrides this hold fully.
            stop = dict(sleeves=list(models), signal_ms=models[30].last, native_status='NEW',
                weights={str(w):str(owned[w]) for w in models},
                native_executed_qty='0', order=dict(type='STOP_LOSS', side='SELL', quantity='3', stopPrice='100'))
            safety = decide(held_views, owners={'9':stop})
            full = next(o for o in safety['orders'] if o['side']=='SELL')
            self.assertEqual(D(full['quantity']), D(3))
            self.assertFalse(any(item['half'] for item in full['reason_exit']['sleeves'].values()))
            with self.assertRaisesRegex(Unknown, 'no recorded spotquant fill'):
                selected.policy(held_views, owned, dict(snap, btc='4'), positions=positions,
                    owners={}, entries_enabled=True, capital_limit=D(1000))

    def test_mixed_full_and_half_orders_fold_the_requested_sleeve_quantities(self):
        with self.scope('sma-half-hold') as selected:
            models = {w:warm(session.Model,w,[D(110)]*400+[D(99)]) for w in session.SLEEVES}
            positions = {w:position(m) for w,m in models.items()}
            positions[30]['adverse'] = True
            views = {w:session._view(m, positions[w])[0] for w,m in models.items()}
            owned = {w:D(p['qty']) for w,p in positions.items()}
            snap = dict(btc=str(sum(owned.values())), usdt_free='100', usdt_locked='0', avg_price='99', open_orders=0)
            decision = selected.policy(views,owned,snap,positions=positions,owners={},
                entries_enabled=True,capital_limit=D(1000))
            sells = [o for o in decision['orders'] if o['side']=='SELL']
            self.assertEqual([(o['sleeves'], D(o['quantity'])) for o in sells],
                             [([30],D(1)),([40],D('.5')),([50],D('.5'))])
            owners, trades = {}, []
            for i, order in enumerate(sells, 1):
                group = order['sleeves']
                owners[str(i)] = dict(order=order, sleeves=group,
                    weights={str(w):positions[w]['qty'] for w in group},
                    native_status='FILLED', native_executed_qty=order['quantity'], reason_exit=order['reason_exit'])
                trades.append(dict(sale(models[30],order['quantity'],i),order_id=i))
            folded = session.apply_day(models,positions,{w:None for w in models},set(),
                models[30].last+DAY,trades,lambda:[],owners=owners)
            self.assertEqual(D(folded[0][30]['qty']),D('.000005'))
            self.assertTrue(folded[0][30]['dust'])
            for w in (40,50):
                self.assertEqual(D(folded[0][w]['qty']),D('.500005'))
                self.assertIsNotNone(models[w].half_memory)
            self.assertEqual(models[30].exit_memory['cause'],'adverse-close')

    def test_next_ordinary_half_waits_for_confirmed_protection_of_actual_remainder(self):
        with self.scope('sma-half-hold') as selected:
            models = {w:warm(session.Model,w,[D(110)]*400+[D(99)]) for w in session.SLEEVES}
            positions = {w:position(m) for w,m in models.items()}
            positions[30]['qty'] = '.500005'
            models[30].half_memory = dict(first_ms=positions[30]['first_ms'], fill_ms=models[30].last+DAY+1000,
                                         order_id=7, executed_group_quantity='.5')
            views = {w:session._view(m,positions[w])[0] for w,m in models.items()}
            owned = {w:D(p['qty']) for w,p in positions.items()}
            snap = dict(btc=str(sum(owned.values())), usdt_free='150', usdt_locked='0', avg_price='99', open_orders=0)
            def decide(owners):
                return selected.policy(views,owned,snap,positions=positions,owners=owners,
                    entries_enabled=True,capital_limit=D(1000))
            waiting = decide({})
            self.assertEqual(waiting['orders'],[])
            self.assertTrue(any(30 in o['sleeves'] and D(o['quantity']) >= D('.5') for o in waiting['protections']))
            owner = dict(sleeves=[30],weights={'30':'.500005'},signal_ms=models[30].last,
                order=dict(type='STOP_LOSS',side='SELL',quantity='.5',stopPrice='90'),
                native_status='NEW',native_executed_qty='0')
            self.assertEqual([o['sleeves'] for o in decide({'9':owner})['orders']],[ [40],[50] ])

    def test_durable_intent_binds_reason_atomically_and_unknown_identity_never_resubmits(self):
        rows, saves = [], []
        def save(identity, payload, status, result):
            saves.append((identity,payload,status,result))
            rows[:] = [(identity,payload,status,result)]
        lifecycle = SimpleNamespace(state=SimpleNamespace(identity='reason-test'), rows=lambda:rows, save=save)
        with self.scope('sma-half-hold'):
            model = warm(session.Model)
            metadata = owner_for(model, half=True, intended='.5', executed='.5')['reason_exit']
            order = dict(symbol='BTCUSDT', side='SELL', type='MARKET', quantity='.5', sleeves=[30], reason_exit=metadata)
            identity = _prepare(execution.Lifecycle.prepare, lifecycle, order, model.last,
                                {'30':position(model)}, {})
            self.assertEqual(len(saves), 1)
            self.assertEqual(rows[0][1]['reason_exit'], metadata)
            self.assertEqual(rows[0][1]['order']['quantity'], '.5')
            rows[0] = (identity, rows[0][1], 'unknown', {})
            self.assertEqual(_prepare(execution.Lifecycle.prepare, lifecycle, order, model.last,
                                     {'30':position(model)}, {}), identity)
            self.assertEqual(len(saves), 1)
            rows[0] = (identity, rows[0][1], 'prepared', {})
            changed = dict(order, quantity='.6')
            with self.assertRaisesRegex(Blocked, 'cannot change'):
                _prepare(execution.Lifecycle.prepare, lifecycle, changed, model.last, {'30':position(model)}, {})

    def test_foreign_or_unbound_intent_rejects_before_recovery_and_hooks_restore(self):
        original = (session.Model, session.apply_day, execution.Lifecycle.prepare, session._guard_state, edge.Model)
        with tempfile.TemporaryDirectory() as directory, State(str(Path(directory)/'state'), 'reason-test') as state:
            foreign = dict(reason_exit_expression='foreign')
            state.set('edge_identity', foreign)
            with self.scope('reason-stop-reentry'):
                with self.assertRaisesRegex(Blocked, 'mismatched edge account'):
                    session._guard_state(state)
            self.assertEqual(state.get('edge_identity'), foreign)
        self.assertEqual((session.Model, session.apply_day, execution.Lifecycle.prepare, session._guard_state, edge.Model), original)
        # A malformed pending MARKET reason must fail before a venue read.
        with tempfile.TemporaryDirectory() as directory, State(str(Path(directory)/'state'), 'reason-test') as state:
            with self.scope('reason-stop-reentry') as selected:
                models = {w:warm(session.Model,w) for w in session.SLEEVES}
                state.set_many(dict(rule=session.RULE, edge_identity=selected.policy.identity,
                    models={str(w):m.checkpoint() for w,m in models.items()},
                    positions={str(w):None for w in models}, follows={str(w):None for w in models},
                    entries_after=models[30].last))
                payload = dict(order=dict(symbol='BTCUSDT', side='SELL', type='MARKET', quantity='1'),
                    sleeves=[30], weights={'30':'1'}, repair={'30':False}, signal_ms=models[30].last)
                state.db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?)', ('sq-test','p4',json.dumps(payload),'prepared','{}',0))
                state.db.commit()
                with self.assertRaisesRegex(Blocked, 'before recovery'):
                    session._guard_state(state)

    def test_adapter_and_nonunity_budget_refused_without_reading_a_clock(self):
        with self.assertRaisesRegex(ValueError, 'before recovery'):
            with self.scope('reason-stop-reentry', SimpleNamespace(offline=False, clock=lambda:self.fail('clock read'))):
                self.fail('adapter entered')
        with self.assertRaisesRegex(ValueError, 'original initial budget'):
            with configured('sma-half-hold', venue=SimpleNamespace(offline=True), features=None,
                            binding={'specification_sha256':'a'*64}, risk={'scale':'.5','sha256':None}):
                self.fail('combined components')
