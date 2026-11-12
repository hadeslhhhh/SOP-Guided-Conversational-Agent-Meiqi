"""SQLite transactions serialize each turn, including the demo email side effect."""
import sqlite3
import time
import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from .models import Session

class ConcurrentTurnError(RuntimeError):
    pass

class Store:
    def __init__(self, path=None, ttl=1800):
        self.ttl = ttl
        self.lock = RLock()
        path = path or os.getenv("SESSION_DB", ":memory:")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL, expires REAL NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS outbox (session_id TEXT PRIMARY KEY, recipient TEXT, subject TEXT, body TEXT, expires REAL NOT NULL)')
        self.db.commit()

    @contextmanager
    def turn(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                now = time.time()
                self.db.execute('DELETE FROM sessions WHERE expires < ?', (now,))
                self.db.execute('DELETE FROM outbox WHERE expires < ?', (now,))
                yield
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def get(self, sid):
        row = self.db.execute('SELECT state FROM sessions WHERE id=? AND expires>=?', (sid, time.time())).fetchone()
        return Session.model_validate_json(row[0]) if row else None

    def exists(self, sid):
        return self.get(sid) is not None

    def delete(self, sid):
        with self.lock:
            self.db.execute('DELETE FROM sessions WHERE id=?', (sid,))
            self.db.execute('DELETE FROM outbox WHERE session_id=?', (sid,))
            self.db.commit()

    def save(self, sid, state, expected_revision=None):
        if expected_revision is not None:
            current = self.get(sid)
            if current and current.revision != expected_revision:
                raise ConcurrentTurnError("The conversation changed while this response was prepared.")
            state.revision = expected_revision + 1
        self.db.execute('INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)',
                        (sid, state.model_dump_json(), time.time() + self.ttl))

    def queue(self, sid, recipient, body):
        self.db.execute('INSERT OR IGNORE INTO outbox VALUES (?, ?, ?, ?, ?)',
                        (sid, recipient, 'Insurance claim conversation summary', body, time.time() + self.ttl))

    def messages(self):
        with self.lock:
            return [dict(zip(('to', 'subject', 'body'), row)) for row in self.db.execute(
                'SELECT recipient, subject, body FROM outbox WHERE expires>=?', (time.time(),))]
