"""Registered P5 deletions through the same Model and account meter as P4."""
import json
from decimal import Decimal as D

from research.rebuild import ROOT, outage_set, run_sleeves, skip_set

PROFILES = {
    'P4': {},
    'no-adverse': {'adverse_stop': D(0)},
    'no-blowoff': {'extend': D(0)},
    'no-reversal': {'cap_drop': D(0)},
    'simple': {'adverse_stop': D(0), 'extend': D(0), 'cap_drop': D(0)},
}


def choose(results):
    eligible = []
    for name, scenarios in results.items():
        if name == 'P4':
            continue
        if all(
            row['cost_net_cagr'] >= results['P4'][scenario]['cost_net_cagr'] - 0.01
            and D(row['continuous_mdd']).quantize(D('1e-9'))
            <= D(results['P4'][scenario]['continuous_mdd']).quantize(D('1e-9'))
            for scenario, row in scenarios.items()
        ):
            eligible.append(name)
    selected = max(eligible, key=lambda name: (
        len(PROFILES[name]), D(results[name]['base']['final_cny']), name,
    )) if eligible else 'P4'
    return {'selected': selected, 'eligible': eligible,
            'rules_deleted': list(PROFILES[selected]), 'production_change_required': selected != 'P4'}


def run_simplification(bars, fx, out, source, market_hash):
    scenarios = {
        'base': {}, 'fee150': {'fee': D('0.0015')},
        'slip2': {'exit_slip': D('0.001'), 'stop_slip': D('0.002')},
        'skip': {'skip_entries': skip_set(bars)}, 'outage': {'frozen': outage_set(bars)},
    }
    results = {}
    for profile, rules in PROFILES.items():
        results[profile] = {}
        for scenario, kwargs in scenarios.items():
            name = f'P5-{profile}-{scenario}'
            row = run_sleeves(bars, fx, name, rules=rules, **kwargs)
            row.update(source=source, market_sha256=market_hash,
                       profile=profile, scenario=scenario, selection='full_sample')
            (out / f'{name}.json').write_text(json.dumps(row, indent=2) + '\n')
            results[profile][scenario] = {key: row[key] for key in (
                'final_cny', 'cost_net_cagr', 'continuous_mdd', 'trades', 'targets_met',
            )}
    original = json.loads((ROOT / 'evidence/sleeves-20260929/P4.json').read_text())
    base = results['P4']['base']
    reproduced = (
        abs(D(base['final_cny']) - D(original['final_cny'])) < D('1e-9')
        and abs(D(base['continuous_mdd']) - D(original['continuous_mdd'])) < D('1e-9')
        and base['trades'] == original['trades']
    )
    decision = choose(results) if reproduced else {
        'selected': 'P4', 'eligible': [], 'blocked': 'baseline did not reproduce',
        'production_change_required': False,
    }
    payload = {'source': source, 'market_sha256': market_hash,
               'baseline_reproduced': reproduced, 'decision': decision, 'results': results,
               'native_qualification': 'NOT_QUALIFIED', 'out_of_sample': False}
    (out / 'suite.json').write_text(json.dumps(payload, indent=2) + '\n')
    return payload
