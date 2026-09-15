"""Durable stage 3A discovery cursor and collection queue; no model or SMTP."""
import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

from persistence import RecoveryMixin


class Inbox(RecoveryMixin):
    def __init__(self, directory, config, folder='INBOX'):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.directory / 'inbox.sqlite'
        self._lock_local = threading.local()
        self.stream = hashlib.sha256(json.dumps([
            config['imap_host'].lower(), config['imap_port'], config['address'].lower(), folder
        ]).encode()).hexdigest()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS streams (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS inbox (
                stream TEXT NOT NULL, validity TEXT NOT NULL, uid INTEGER NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, error TEXT, snapshot TEXT,
                PRIMARY KEY(stream, validity, uid))''')
        os.chmod(self.db_path, 0o600)

    def connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def state(self):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM streams WHERE id=?', (self.stream,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_state(self, state):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO streams VALUES (?,?)',
                       (self.stream, json.dumps(state)))

    def discover(self, state, uids, boundary, hook=lambda event: None):
        # Queue insertion and cursor advancement are a single commit.
        updated = dict(state, cursor=boundary)
        with self.connect() as db:
            for uid in uids:
                db.execute('INSERT OR IGNORE INTO inbox(stream,validity,uid,status) VALUES (?,?,?,?)',
                           (self.stream, state['validity'], uid, 'pending'))
            hook('before_cursor_commit')
            db.execute('INSERT OR REPLACE INTO streams VALUES (?,?)',
                       (self.stream, json.dumps(updated)))
        return updated

    def rows(self, validity=None):
        with self.connect() as db:
            if validity is None:
                rows = db.execute('SELECT * FROM inbox WHERE stream=? ORDER BY validity,uid', (self.stream,))
            else:
                rows = db.execute('SELECT * FROM inbox WHERE stream=? AND validity=? ORDER BY uid',
                                  (self.stream, validity))
            return [dict(row) for row in rows]

    def update(self, validity, uid, **fields):
        allowed = {'status', 'attempts', 'retry_at', 'error', 'snapshot'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid inbox update')
        with self.connect() as db:
            db.execute('UPDATE inbox SET ' + ','.join(k+'=?' for k in fields) +
                       ' WHERE stream=? AND validity=? AND uid=?',
                       [*fields.values(), self.stream, validity, uid])

    def summary(self):
        with self.connect() as db:
            counts = {r[0]: r[1] for r in db.execute(
                'SELECT status,COUNT(*) FROM inbox WHERE stream=? GROUP BY status', (self.stream,))}
        state = self.state()
        current = {}
        if state and state.get('validity'):
            for row in self.rows(state['validity']):
                current[row['status']] = current.get(row['status'], 0) + 1
        return {'state': state, 'counts': counts, 'current_generation_counts': current}
