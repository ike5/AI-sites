"""SQLite storage: one file per site, safe for several threads and processes."""

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS structures (name TEXT PRIMARY KEY, def TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    collection TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, version INTEGER NOT NULL,
    created TEXT NOT NULL, updated TEXT NOT NULL, confirmed TEXT, live_as_of TEXT,
    PRIMARY KEY (collection, key));
CREATE TABLE IF NOT EXISTS nodes (
    tree TEXT NOT NULL, id TEXT NOT NULL, parent TEXT, position INTEGER NOT NULL,
    label TEXT NOT NULL, data TEXT NOT NULL, updated TEXT NOT NULL,
    PRIMARY KEY (tree, id));
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, collection TEXT NOT NULL, key TEXT NOT NULL,
    set_json TEXT NOT NULL, start TEXT NOT NULL, end TEXT, announced INTEGER NOT NULL,
    created TEXT NOT NULL, seq INTEGER);
CREATE INDEX IF NOT EXISTS schedules_by_record ON schedules (collection, key);
CREATE TABLE IF NOT EXISTS log (
    seq INTEGER PRIMARY KEY, time TEXT NOT NULL, op TEXT NOT NULL, path TEXT NOT NULL,
    commit_json TEXT NOT NULL, detail_json TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS log_by_path ON log (path);
CREATE TABLE IF NOT EXISTS log_values (
    seq INTEGER NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL, salt TEXT NOT NULL,
    PRIMARY KEY (seq, field));
CREATE TABLE IF NOT EXISTS log_sealed (
    seq INTEGER PRIMARY KEY, reveal_at TEXT, inner_json TEXT NOT NULL, salt TEXT NOT NULL, path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, path TEXT NOT NULL, what TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS timers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, path TEXT NOT NULL, what TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS timers_by_time ON timers (at);

-- Milestone 2: signed checkpoints, redaction markers, roles, keys and the private audit trail.
CREATE TABLE IF NOT EXISTS checkpoints (
    seq INTEGER PRIMARY KEY, time TEXT NOT NULL, body TEXT NOT NULL, signature TEXT NOT NULL,
    proofs TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS log_redactions (
    seq INTEGER NOT NULL, field TEXT NOT NULL, at TEXT NOT NULL, by_seq INTEGER NOT NULL,
    PRIMARY KEY (seq, field));
CREATE TABLE IF NOT EXISTS roles (
    name TEXT PRIMARY KEY, grants TEXT NOT NULL, description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS keys (
    id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, role TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL, revoked TEXT);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, who TEXT NOT NULL, role TEXT NOT NULL,
    op TEXT NOT NULL, target TEXT, key TEXT, detail TEXT NOT NULL DEFAULT '{}', ok INTEGER NOT NULL);

-- Milestone 3: retry safety and quotas for visitor calls, bookings, and messages sent through forms.
CREATE TABLE IF NOT EXISTS idempotency (
    caller TEXT NOT NULL, scope TEXT NOT NULL, idem_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    response TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY (caller, scope, idem_key));
CREATE TABLE IF NOT EXISTS usage (
    caller TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (caller, day));
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY, collection TEXT NOT NULL, key TEXT NOT NULL, quantity INTEGER NOT NULL,
    status TEXT NOT NULL, caller TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL,
    expires TEXT, confirmed TEXT, released TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS bookings_by_record ON bookings (collection, key);
CREATE INDEX IF NOT EXISTS bookings_by_caller ON bookings (caller, status);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, form TEXT NOT NULL, caller TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
    text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created TEXT NOT NULL, read_at TEXT,
    reply TEXT, replied_at TEXT);
CREATE INDEX IF NOT EXISTS messages_by_form ON messages (form, created);
CREATE INDEX IF NOT EXISTS messages_by_caller ON messages (caller, created);
"""


class Store:
    def __init__(self, path: str, schema: str = SCHEMA):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._depth = 0
        self._scrub_due = False
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # Deleted content is overwritten with zeros, so a redacted value does not linger in free pages.
        self.db.execute("PRAGMA secure_delete=ON")
        with self.lock:
            self.db.executescript(schema)

    @contextmanager
    def tx(self):
        """A re-entrant write transaction. Any exception rolls the whole outermost transaction back."""
        with self.lock:
            if self._depth == 0:
                self.db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.db.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.db.execute("COMMIT")
                    if self._scrub_due:
                        self.scrub()

    def q(self, sql: str, params: tuple = ()) -> list:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    def x(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, params)

    def meta(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM meta WHERE k=?", (key,))
        return row["v"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.x("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (key, str(value)))

    def scrub(self) -> None:
        """Fold the write-ahead log into the database and empty it, so no old page survives beside it.

        Inside a transaction this waits for the outermost commit; a rollback leaves it due for the next.
        """
        with self.lock:
            if self._depth > 0:
                self._scrub_due = True
                return
            self._scrub_due = False
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        with self.lock:
            self.db.close()
