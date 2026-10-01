"""Synthetic actual-session backup/restart drill, without accounts or credentials."""
import argparse
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from research.restore_check import check
from research.rebuild import source_identity
from spotquant.config import Config
from spotquant.model import DAY, ORIGIN
from spotquant.offline import P4Venue
from spotquant.session import run
from spotquant.types import serial


def drill(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    prices = [D(100)] * 400 + [D(98)]
    venue = P4Venue([(ORIGIN + i * DAY, p, p, p) for i, p in enumerate(prices)])
    with tempfile.TemporaryDirectory(prefix='spot-recovery-drill-') as scratch:
        config = Config('1', str(Path(scratch) / 'original'), 1, 1, 'demo', '1000')
        reports = [run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait)]
        for close in (D(101), D(102)):
            venue.bars.append((venue.bars[-1][0] + DAY, close, close, close))
            venue.now_ms, venue.price = venue.bars[-1][0] + DAY, close
            reports.append(run(config, venue, execute=True, monotonic=venue.monotonic, wait=venue.wait))
        archived = reports[-1]['session_archive']
        proof = check(archived['report'])
        if not proof['execution_anchor_preserved'] or not proof['durable_fills'] or proof['pending_intents']:
            raise ValueError('drill requires an entered, durably reconciled account')
        for key in ('report', 'backup'):
            file = Path(archived[key])
            shutil.copyfile(file, output / file.name)
        restored = Path(scratch) / 'restored'
        restored.mkdir()
        shutil.copyfile(archived['backup'], restored / 'intents.sqlite')
        restored_config = Config('1', str(restored), 1, 1, 'demo', '1000')
        before = serial({'cash': venue.cash, 'btc': venue.btc, 'submitted_ids': venue.sent})
        observed = run(restored_config, venue, execute=False, monotonic=venue.monotonic, wait=venue.wait)
        after = serial({'cash': venue.cash, 'btc': venue.btc, 'submitted_ids': venue.sent})
        if before != after or observed['status'] != 'read_only':
            raise ValueError('restored read-only account did not reconcile unchanged')
        venue.btc += D('.001')
        external = run(restored_config, venue, execute=False, monotonic=venue.monotonic, wait=venue.wait)
        if external['status'] != 'unknown' or before['submitted_ids'] != venue.sent:
            raise ValueError('external BTC was adopted or a read-only cycle submitted an order')
    result = serial({'synthetic': True, 'source': source_identity(), 'backup': proof,
                     'retained_manifest': Path(archived['report']).name,
                     'retained_backup': Path(archived['backup']).name,
                     'retained_backup_sha256': hashlib.sha256((output / Path(archived['backup']).name).read_bytes()).hexdigest(),
                     'balance_and_submission_ids_unchanged': before == after,
                     'restored_readonly_status': observed['status'], 'external_btc_status': external['status'],
                     'external_btc_reason': external.get('reason'), 'before': before, 'after': after,
                     'initial_sessions': reports, 'restored_report': observed,
                     'new_risk_authorized': False, 'native_execution_verified': False})
    with (output / 'drill.json').open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    result = drill(args.out)
    print(json.dumps({k: result[k] for k in ('synthetic', 'balance_and_submission_ids_unchanged',
                                            'restored_readonly_status', 'external_btc_status', 'native_execution_verified')}))


if __name__ == '__main__':
    main()
