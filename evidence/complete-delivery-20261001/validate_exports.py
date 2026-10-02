"""Require complete, source-bound fixed-capital evidence before final exports."""
import hashlib
import json
import math
from pathlib import Path


def validated(spot_path, perp_path, assessment_path):
    paths = [Path(p) for p in (spot_path, perp_path, assessment_path)]
    blobs = [p.read_bytes() for p in paths]
    spot, perp, report = [json.loads(blob) for blob in blobs]
    for name, blob in zip(('spot', 'perp'), blobs):
        if report.get('inputs', {}).get(name) != hashlib.sha256(blob).hexdigest():
            raise ValueError('assessment input SHA differs: ' + name)
    expected = {'spot/' + key for key in spot['results']} | {
        'perp/' + candidate + '/' + scenario
        for candidate, rows in perp['results'].items() for scenario in rows}
    if len(expected) != 48 or set(report.get('candidate_attribution', {})) != expected:
        raise ValueError('all 48 candidate/scenario attributions required')
    accounts = list(spot['results'].values()) + [r for rows in perp['results'].values() for r in rows.values()]
    if any(not r.get('complete') or not r.get('audit', {}).get('passed') for r in accounts):
        raise ValueError('complete audited account matrix required')
    if report.get('native_execution_verified') is not False or report.get('qualification') != 'NOT_QUALIFIED':
        raise ValueError('historical export cannot establish native qualification')
    selected = (report['spot_selection']['selected_research_candidate'], report['perp_selection']['selected_research_candidate'])
    for key, candidates in (('joint_fixed_capital', ('default', 'incumbent')),
                            ('joint_selected_fixed_capital', selected)):
        rows = report.get(key, [])
        if len(rows) != 5 or [r['spot_initial_cny'] for r in rows] != [0, 2500, 5000, 7500, 10000]:
            raise ValueError('complete five-allocation joint evidence required: ' + key)
        for row in rows:
            if row['spot_initial_cny'] + row['perp_initial_cny'] != 10000:
                raise ValueError('joint original capital must total CNY10000')
            if (row['spot_candidate'], row['perp_candidate']) != candidates:
                raise ValueError('joint candidate identities differ')
            if row.get('fixed_accounts_no_transfers') is not True or row.get('continuous_joint_mdd_verified') is not False:
                raise ValueError('actual fixed accounts and daily-only MDD required')
            values = row.get('daily_equity_cny', [])
            if len(values) != 2454 or any(not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError('2454 positive joint daily marks required')
            peak, mdd = 10000, 0
            for value in values:
                peak = max(peak, value)
                mdd = max(mdd, 1 - value / peak)
            computed = {'final_cny': values[-1], 'daily_mdd': mdd,
                        'cagr': (values[-1] / 10000) ** (365.25 / 2454) - 1}
            for name, value in computed.items():
                recorded = row['metrics'].get(name)
                if recorded is None or not math.isfinite(recorded) or abs(recorded - value) > max(1e-10, abs(value) * 1e-10):
                    raise ValueError('joint curve metric differs: ' + name)
    return spot, perp, report
