"""Join selective reruns with unchanged accounts, preserving every measured source."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from research.complete_spot import CANDIDATES, SCENARIOS
from research.rebuild import ROOT, source_identity


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def row_digest(row):
    return digest(json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def verify_measured_source(source, reference):
    """Verify the measured Git tree, independently of today's working source."""
    head = source.get('git_head', '')
    if source.get('dirty') or not re.fullmatch('[0-9a-f]{40}', head):
        raise ValueError('a full clean measured Git revision is required')
    def git(*args):
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, check=True).stdout
    names = sorted(git('ls-tree', '-r', '--name-only', head, '--', 'spotquant', 'research').decode().splitlines())
    blobs = {name: git('show', head + ':' + name) for name in names if name.endswith('.py')}
    full = hashlib.sha256()
    for name, blob in blobs.items():
        full.update(name.encode() + b'\0' + blob + b'\0')
    if full.hexdigest() != source.get('python_sources_sha256'):
        raise ValueError('measured source digest differs from its immutable Git tree')
    if any(name not in blobs or digest(blobs[name]) != sha for name, sha in reference.items()):
        raise ValueError('replacement measured source does not contain the reviewed implementation')
    return {'git_head': head, 'python_sources_sha256': full.hexdigest(), 'reviewed_implementation_verified': True}


def validated(row):
    return (row.get('complete') and row.get('audit', {}).get('passed')
            and not row.get('execution_unresolved_sessions') and not row.get('pending_intents')
            and not row.get('policy_pending') and all(r['archive_verified'] for r in row['sessions']))


def assemble(original, replacement, proof, original_sha, replacement_sha, proof_sha):
    keys = {c + '-' + s for c in CANDIDATES for s in SCENARIOS}
    if set(original['results']) != keys or original['source']['dirty'] or replacement['source']['dirty']:
        raise ValueError('original full matrix and clean measured sources required')
    for name in ('market_sha256', 'schedule_sha256', 'fx_sha256', 'crowding_sha256'):
        if not original.get(name) or original[name] != replacement.get(name):
            raise ValueError('replacement input differs: ' + name)
    bundles = [b for b in proof['input_bundles'] if b['sha256'] == original_sha]
    if len(bundles) != 1 or bundles[0]['source'] != original['source']:
        raise ValueError('proof does not bind the original bytes/source')
    rows = {r['account']: r for r in bundles[0]['accounts']}
    if set(rows) != keys:
        raise ValueError('proof must cover every original account')
    for key, row in original['results'].items():
        if rows[key]['account_result_sha256'] != row_digest(row):
            raise ValueError('proof account content differs: ' + key)
    needed = {key for key, row in rows.items() if not row['zero_hit_reuse_candidate']}
    if set(replacement['results']) != needed:
        raise ValueError('rerun every affected or unqualified account exactly once')
    result, ledger = {}, {}
    for key in sorted(keys):
        rerun = key in needed
        bundle = replacement if rerun else original
        row = bundle['results'][key]
        if not validated(row):
            raise ValueError('unqualified measured account: ' + key)
        if rerun:
            before = original['results'][key]
            if (any(row.get(field) != before.get(field) for field in ('candidate', 'scenario', 'initial_cny'))
                    or [r['start_ms'] for r in row['sessions']] != [r['start_ms'] for r in before['sessions']]):
                raise ValueError('replacement account or scheduled sessions differ: ' + key)
        result[key] = row
        ledger[key] = {'source': bundle['source'], 'run_bundle_sha256': replacement_sha if rerun else original_sha,
                       'account_result_sha256': row_digest(row), 'selective_rerun': rerun,
                       'unchanged_under_zero_effect_proof': not rerun}
    report = {key: value for key, value in original.items() if key != 'results'}
    report.update(results=result, source_mode='mixed_account_sources', account_sources=ledger,
                  complete=True, production_promoted=False, native_execution_verified=False,
                  selective_rerun_proof_sha256=proof_sha,
                  assembly_method='Original measured rows unchanged; every affected account replaced by an independently complete audited rerun. Consult account_sources; source is the original baseline source.')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'replacement', 'proof', 'verifier', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(argv)
    source = source_identity()
    if source['dirty'] or args.out.exists():
        parser.error('commit source and choose a fresh output')
    blobs = [getattr(args, name).read_bytes() for name in ('original', 'replacement', 'proof')]
    original, replacement, proof = [json.loads(blob) for blob in blobs]
    if digest(args.verifier.read_bytes()) != proof['verifier_sha256']:
        raise ValueError('independent verifier bytes differ from proof')
    reference = proof.get('implementation_reference', {})
    expected = {'spotquant/execution.py', 'spotquant/session.py', 'spotquant/preview.py',
                'spotquant/follow.py', 'research/complete_spot.py'}
    if set(reference) != expected or any(digest((ROOT / name).read_bytes()) != sha for name, sha in reference.items()):
        raise ValueError('proof implementation differs from current reviewed fix')
    report = assemble(original, replacement, proof, *[digest(blob) for blob in blobs])
    report['replacement_measured_source_verification'] = verify_measured_source(replacement['source'], reference)
    report['assembly_source'] = source
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        json.dump(report, stream, separators=(',', ':'), allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
