"""Local single-writer lock, durable intents and the latest session report.

The database is a recovery aid, never an authority for balances. One persistent
directory belongs to one spot account on one machine. This module does not
submit orders. Explicit execution records stable intents before dispatch.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from decimal import Decimal as D
from time import time

from .types import Blocked, Unknown, serial


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
            self.db.execute('CREATE TABLE IF NOT EXISTS fills ('
                            'id INTEGER PRIMARY KEY, time_ms INTEGER NOT NULL, '
                            'order_id INTEGER NOT NULL, payload TEXT NOT NULL)')
            self.db.execute('CREATE INDEX IF NOT EXISTS fills_time ON fills(time_ms,id)')
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
        pending = []
        for item_id, kind, encoded, status, result in self.db.execute(
                "SELECT id,kind,payload,status,result FROM intents WHERE status IN "
                "('unknown','partial','prepared','canceling','resting') ORDER BY updated"):
            payload = json.loads(encoded)
            order = payload.get('order', {})
            if (status == 'resting' and order.get('type') != 'MARKET'
                    and json.loads(result).get('status') != 'PARTIALLY_FILLED'):
                continue
            if (status == 'prepared' and order.get('side') == 'BUY'
                    and order.get('type') == 'MARKET' and json.loads(result).get('not_sent') is True):
                continue
            pending.append(dict(id=item_id, kind=kind, payload=payload, status=status))
        return pending

    def trades(self, venue, since_ms: int) -> list[dict]:
        """Retain immutable fills; refresh a one-day overlap after the last read.

        A long gap resumes from the last immutable fill ID when one is stored.
        No balance anchor is replaced, and identical timestamps retain every ID.
        """
        def observed_ms():
            if hasattr(venue, '_timestamp'):
                return venue._timestamp()
            return int((getattr(venue, 'clock', None) or getattr(venue, '_clock', time))() * 1000)
        now_ms = observed_ms()
        watermark = self.get('fill_watermark')
        if watermark and now_ms < watermark['through_ms']:
            raise Unknown('fill observation clock moved backwards')
        resume_id = None
        if watermark and now_ms - watermark['through_ms'] > 80 * 86400000:
            row = self.db.execute('SELECT MAX(id) FROM fills').fetchone()
            if not row or row[0] is None:
                raise Unknown('fill observation gap exceeds supported recovery window')
            resume_id = int(row[0])
        start = since_ms if watermark is None else max(
            watermark['from_ms'], watermark['through_ms'] - 86400000)
        # Older requested fills remain available locally. Only an uncovered prefix
        # requires a historical request; empty responses do not adopt holdings.
        if watermark and since_ms < watermark['from_ms']:
            start = since_ms
        observed = (list(venue.trades(start, from_id=resume_id)) if resume_id is not None
                    else list(venue.trades(start)))
        anchor_time = None
        if resume_id is not None:
            stored = self.db.execute('SELECT time_ms FROM fills WHERE id=?', (resume_id,)).fetchone()
            anchor_time = None if stored is None else stored[0]
        with self.db:
            for row in observed:
                if type(row.get('id')) is not int or type(row.get('time')) is not int:
                    raise Unknown('fill identity or timestamp is invalid')
                if row['time'] > observed_ms() or (
                        resume_id is not None and (row['id'] < resume_id
                            or anchor_time is not None and row['time'] < anchor_time)) or (
                        resume_id is None and row['time'] < start):
                    raise Unknown('fill lies outside the requested observation')
                payload = json.dumps(serial(row), sort_keys=True, allow_nan=False)
                prior = self.db.execute('SELECT payload FROM fills WHERE id=?', (row['id'],)).fetchone()
                if prior and prior[0] != payload:
                    raise Unknown('previously recorded fill changed')
                self.db.execute('INSERT OR IGNORE INTO fills VALUES (?,?,?,?)',
                                (row['id'], row['time'], row['order_id'], payload))
            saved = {'from_ms': min(since_ms, watermark['from_ms']) if watermark else since_ms,
                     'through_ms': now_ms}
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                            ('fill_watermark', json.dumps(saved, sort_keys=True)))
        rows = []
        for payload, in self.db.execute('SELECT payload FROM fills WHERE time_ms>=? ORDER BY time_ms,id',
                                       (since_ms,)):
            row = json.loads(payload)
            for key in ('qty', 'quote', 'price', 'commission'):
                if key in row:
                    row[key] = D(row[key])
            rows.append(row)
        return rows

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
