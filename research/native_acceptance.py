"""Archive owner-supplied native readbacks; never authorize trading or certify origin.

Hashes bind supplied bytes to the execution code and Demo account. They do not
authenticate Binance: the owner must review original native exports separately.
This tool makes no network requests and never writes into a runtime state dir.
"""
import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import shutil

CASES = ('entry', 'partial_protection', 'reduction', 'restart',
         'stopped_trigger', 'cancel_replace')


def digest(package):
    value = hashlib.sha256()
    paths = sorted(Path(package).glob('*.py'))
    if not paths:
        raise ValueError('execution package is empty')
    for path in paths:
        value.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return value.hexdigest()


def number(value, positive=False):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('invalid native quantity') from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError('invalid native quantity')
    return result


def identity(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isascii() and str(value).isdigit() and int(value) > 0


def safe_file(root, name):
    if not isinstance(name, str) or Path(name).name != name or name in ('manifest.json', 'acceptance.json'):
        raise ValueError('native artifact must be a sibling file')
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('native artifact missing or symlinked')
    return path


def verify_case(name, record, manifest):
    for field in ('project', 'account_uid', 'environment', 'execution_code_sha256'):
        if record.get(field) != manifest[field]:
            raise ValueError('native case account/environment/source mismatch')
    if record.get('origin') != 'owner_captured_binance_demo' or record.get('synthetic') is not False:
        raise ValueError('synthetic or unspecified origin cannot establish native evidence')
    events = record.get('events')
    if not isinstance(events, list) or not events:
        raise ValueError('native readbacks required; passed flags are not evidence')
    prefix = '/api/v3/' if manifest['project'] == 'spotquant' else '/fapi/'
    for event in events:
        if (type(event.get('observed_at_ms')) is not int or event['observed_at_ms'] <= 0
                or not str(event.get('endpoint', '')).startswith(prefix)
                or not isinstance(event.get('response'), dict)):
            raise ValueError('timestamped native endpoint/response required')
        response = event['response']
        if response.get('symbol') != 'BTCUSDT' or not identity(response.get('orderId', response.get('algoId'))):
            raise ValueError('BTCUSDT native order identity required')
    rows = [event['response'] for event in events]
    latest = {}
    for event in sorted(events, key=lambda e: e['observed_at_ms']):
        response = event['response']
        # Native order and algo namespaces are distinct. Repeated readbacks are
        # observations of one protection, not additional sell quantities.
        key = ('order', str(int(response['orderId']))) if 'orderId' in response else ('algo', str(int(response['algoId'])))
        latest[key] = response
    fills = [r for r in rows if r.get('status') == 'FILLED' and number(r.get('executedQty', 0), True) > 0]
    if name == 'entry' and not any(r.get('side') == 'BUY' for r in fills):
        raise ValueError('filled native entry missing')
    if name == 'reduction' and not any(r.get('side') == 'SELL' for r in fills):
        raise ValueError('filled native reduction missing')
    if name == 'partial_protection':
        partial = [r for r in rows if r.get('status') == 'PARTIALLY_FILLED']
        stops = [r for r in latest.values() if r.get('status', r.get('algoStatus')) == 'NEW' and 'STOP' in r.get('type', r.get('orderType', '')) and r.get('side') == 'SELL']
        held = number(record.get('observed_position_btc'), True)
        protected = sum((held if r.get('closePosition') is True else number(r.get('origQty', 0)) for r in stops), Decimal(0))
        if not partial or not stops or protected < held:
            raise ValueError('partial-fill native protection does not cover observed BTC')
        if manifest['project'] == 'coinquant' and not any(r.get('type', r.get('orderType')) == 'TAKE_PROFIT_MARKET' and r.get('status', r.get('algoStatus')) == 'NEW' and r.get('closePosition') is True for r in latest.values()):
            raise ValueError('full-position native take profit missing')
    if name == 'restart':
        before, after = record.get('before_client_ids'), record.get('after_client_ids')
        if not before or before != after or record.get('new_submissions') != 0:
            raise ValueError('restart must retain stable identities without duplicate submission')
        observed = {r.get('clientOrderId', r.get('clientAlgoId')) for r in rows}
        if not set(before).issubset(observed):
            raise ValueError('restarted identities lack native readbacks')
    if name == 'stopped_trigger':
        ended = record.get('session_ended_at_ms')
        if type(ended) is not int or not any(e['observed_at_ms'] > ended and e['response'] in fills and e['response'].get('side') == 'SELL' and 'STOP' in e['response'].get('type', '') and type(e['response'].get('updateTime')) is int and e['response']['updateTime'] > ended for e in events):
            raise ValueError('native stop fill after client exit missing')
    if name == 'cancel_replace':
        old = [r for r in rows if r.get('status') == 'CANCELED']
        new = [r for r in rows if r.get('status') == 'NEW' and 'STOP' in r.get('type', '')]
        if not old or not new or {str(r.get('orderId', r.get('algoId'))) for r in old} & {str(r.get('orderId', r.get('algoId'))) for r in new}:
            raise ValueError('distinct canceled/replacement native protection missing')
        if manifest['project'] == 'spotquant' and record.get('non_atomic_gap_acknowledged') is not True:
            raise ValueError('spot cancel/replace gap must be recorded')


def check(manifest_path, package):
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text())
    expected = digest(package)
    failures = []
    if manifest.get('project') not in ('spotquant', 'coinquant') or Path(package).name != manifest.get('project'):
        failures.append('execution package/project mismatch')
    if manifest.get('environment') != 'demo' or not identity(manifest.get('account_uid')):
        failures.append('explicit matching Demo account UID required')
    if manifest.get('execution_code_sha256') != expected:
        failures.append('execution code changed or digest missing')
    try:
        number(manifest.get('capital_limit_usdt'), True)
    except ValueError:
        failures.append('positive Demo capital ceiling required')
    outcomes = {}
    for name in CASES:
        try:
            case = manifest.get('cases', {})[name]
            artifact = safe_file(path.parent, case['file'])
            blob = artifact.read_bytes()
            if hashlib.sha256(blob).hexdigest() != case['sha256']:
                raise ValueError('native bytes differ from registered SHA256')
            verify_case(name, json.loads(blob), manifest)
            outcomes[name] = {'structural_evidence_present': True, 'sha256': case['sha256']}
        except (KeyError, TypeError, ValueError, OSError) as exc:
            outcomes[name] = {'structural_evidence_present': False, 'reason': str(exc)}
    complete = not failures and all(r['structural_evidence_present'] for r in outcomes.values())
    return {'execution_code_sha256': expected, 'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'account_uid': manifest.get('account_uid'), 'project': manifest.get('project'),
            'failures': failures, 'cases': outcomes, 'ready_for_owner_native_review': complete,
            'native_execution_verified': False, 'qualification': 'NOT_QUALIFIED',
            'writes_authorized': False, 'origin_authenticated': False,
            'limitations': 'Structural checks of owner supplied files. Hashes cannot authenticate Binance or establish 30 natural days.'}


def archive(manifest_path, package, output):
    result = check(manifest_path, package)
    source = Path(manifest_path).resolve()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    # Copy only files whose registered bytes passed structural verification.
    manifest = json.loads(source.read_text())
    shutil.copyfile(source, output / 'manifest.json')
    for name, case in manifest.get('cases', {}).items():
        if name in result['cases'] and result['cases'][name]['structural_evidence_present']:
            shutil.copyfile(safe_file(source.parent, case['file']), output / case['file'])
    with (output / 'acceptance.json').open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args(argv)
    result = archive(args.manifest, args.package, args.archive) if args.archive else check(args.manifest, args.package)
    print(json.dumps(result, indent=2))
    return 0 if result['ready_for_owner_native_review'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
