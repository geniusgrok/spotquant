"""Local single-writer lock and the latest read-only report.

The database is a recovery aid, never an authority for balances. One persistent
directory belongs to one spot account on one machine. This module does not
submit orders. While execution is blocked, sessions do not insert order intents.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from time import time

from .types import Blocked, serial


def client_id(account: str, bar: int, operation: str) -> str:
    """Stable id for a future order. Independent of the parameter set."""
    identity = f'BTCUSDT|spot|{account}|{bar}|{operation}'.encode()
    return 'sq-' + hashlib.sha256(identity).hexdigest()[:30]


OBSERVATION_LIMIT = 1000


class State:
    def __init__(self, directory: str | Path, identity: str):
        self.directory = Path(directory).expanduser().resolve()
        self.identity = identity
        self.lock = None
        self.db = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = open(self.directory / 'execution.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.lock.seek(0)
                if self.lock.read(1) == b'':
                    self.lock.write(b'0')
                    self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(self.directory / 'intents.sqlite', timeout=0)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self.db.execute(
                'CREATE TABLE IF NOT EXISTS intents ('
                'id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, '
                'status TEXT NOT NULL, result TEXT NOT NULL, updated REAL NOT NULL)')
            self.db.execute(
                'CREATE TABLE IF NOT EXISTS observations ('
                'sequence INTEGER PRIMARY KEY, recorded_at REAL NOT NULL, payload TEXT NOT NULL)')
            saved = self.get('identity')
            if saved is not None and saved != self.identity:
                raise Blocked('state directory belongs to another account or environment')
            self.set('identity', self.identity)
            return self
        except Blocked:
            self.__exit__(None, None, None)
            raise
        except (OSError, sqlite3.Error) as exc:
            self.__exit__(None, None, None)
            raise Blocked('state unavailable or another run holds the execution lock') from exc
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def get(self, key: str):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value) -> None:
        self.set_many({key: value})

    def set_many(self, values: dict) -> None:
        with self.db:
            for key, value in values.items():
                self.db.execute(
                    'INSERT OR REPLACE INTO meta VALUES (?,?)',
                    (key, json.dumps(serial(value), sort_keys=True)),
                )

    def pending(self) -> list[dict]:
        return [
            dict(id=item_id, kind=kind, payload=json.loads(payload), status=status)
            for item_id, kind, payload, status in self.db.execute(
                "SELECT id,kind,payload,status FROM intents WHERE status IN "
                "('unknown','partial','prepared','canceling') ORDER BY updated")
        ]

    def report(self, value: dict) -> None:
        """Persist the latest observation. Reports never contain credentials."""
        with self.db:
            self.db.execute(
                'INSERT INTO observations(recorded_at,payload) VALUES (?,?)',
                (time(), json.dumps(serial(value), sort_keys=True, allow_nan=False)),
            )
            self.db.execute(
                'DELETE FROM observations WHERE sequence <= (SELECT MAX(sequence) FROM observations) - ?',
                (OBSERVATION_LIMIT,),
            )
        output = self.directory / 'latest.json'
        temporary = output.with_suffix('.tmp')
        with open(temporary, 'w', encoding='utf-8') as stream:
            json.dump(serial(value), stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        if os.name != 'nt':
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
