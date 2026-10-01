"""Validate an immutable session backup without adopting or changing an account."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def check(path):
    path = Path(path).resolve()
    report = json.loads(path.read_text())
    name = report['backup_file']
    if Path(name).name != name or not name.endswith('.sqlite'):
        raise ValueError('backup path must stay beside its manifest')
    backup = path.parent / name
    if backup.is_symlink() or hashlib.sha256(backup.read_bytes()).hexdigest() != report['backup_sha256']:
        raise ValueError('backup identity mismatch')
    with sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        identity = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if not identity or json.loads(identity[0]) != report['state_identity']:
            raise ValueError('backup account identity mismatch')
        core = {k: v for k, v in report.items() if k not in (
            'state_identity', 'backup_sha256', 'backup_file', 'archive_format', 'archived_report_sha256')}
        core_digest = hashlib.sha256(json.dumps(core, sort_keys=True, allow_nan=False).encode()).hexdigest()
        binding = db.execute("SELECT value FROM meta WHERE key='last_archived_report_sha256'").fetchone()
        if (core_digest != report.get('archived_report_sha256') or not binding
                or json.loads(binding[0]) != core_digest):
            raise ValueError('archived report/source identity differs from backed-up state')
        anchor = db.execute("SELECT value FROM meta WHERE key='execution_anchor'").fetchone()
        count = db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM intents WHERE status IN "
                             "('unknown','partial','prepared','canceling')").fetchone()[0]
    return {'integrity_verified': True, 'state_identity': report['state_identity'],
            'backup_sha256': report['backup_sha256'], 'durable_fills': count,
            'pending_intents': pending, 'execution_anchor_preserved': bool(anchor),
            'account_reconciled': False, 'new_risk_authorized': False,
            'next_action': 'restore to an isolated directory, then owner read-only account reconciliation'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(check(args.manifest), indent=2))


if __name__ == '__main__':
    main()
