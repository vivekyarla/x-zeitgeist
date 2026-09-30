"""SQLite storage (stdlib sqlite3). One file holds every account; per-user pipeline files live
under DATA_DIR/users/<id>/. Times are UNIX seconds (REAL)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused, so a new account can't inherit old files
  email       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash     TEXT NOT NULL,
  created_at  REAL NOT NULL,
  onboarded   INTEGER NOT NULL DEFAULT 0,
  settings    TEXT NOT NULL,
  recap_token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS keys (
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  value      TEXT NOT NULL,                        -- Fernet ciphertext
  updated_at REAL NOT NULL,
  PRIMARY KEY (user_id, name)
);
CREATE TABLE IF NOT EXISTS runs (
  user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  state           TEXT NOT NULL DEFAULT 'new',     -- new | queued | running | ready | error
  slack_mode      TEXT NOT NULL DEFAULT 'auto',
  queued_at       REAL,
  started_at      REAL,
  last_run_at     REAL,
  next_run_at     REAL,
  last_error      TEXT,
  last_deliver_at REAL,
  lease_until     REAL NOT NULL DEFAULT 0,         -- a process holding this user's files until then
  lease_owner     TEXT,
  rerun           INTEGER NOT NULL DEFAULT 0       -- settings changed mid-run: run again right after
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            try:  # databases created before `rerun` existed
                c.execute("ALTER TABLE runs ADD COLUMN rerun INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
        finally:
            c.close()

    # -- users -------------------------------------------------------------
    def create_user(self, email: str, pw_hash: str, settings: dict, recap_token: str) -> int | None:
        with self.conn() as c:
            try:
                cur = c.execute("INSERT INTO users (email, pw_hash, created_at, settings, recap_token) "
                                "VALUES (?, ?, ?, ?, ?)", (email, pw_hash, time.time(), json.dumps(settings),
                                                            recap_token))
            except sqlite3.IntegrityError:
                return None
            uid = cur.lastrowid
            c.execute("INSERT INTO runs (user_id) VALUES (?)", (uid,))
            return uid

    def user(self, user_id: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def user_by_email(self, email: str) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    def user_by_token(self, token: str) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM users WHERE recap_token = ?", (token,)).fetchone()

    def set_settings(self, user_id: int, settings: dict) -> None:
        with self.conn() as c:
            c.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(settings), user_id))

    def set_onboarded(self, user_id: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE users SET onboarded = 1 WHERE id = ?", (user_id,))

    def set_recap_token(self, user_id: int, token: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE users SET recap_token = ? WHERE id = ?", (token, user_id))

    def delete_user(self, user_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def user_ids(self) -> list[int]:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT id FROM users")]

    # -- keys (ciphertext only; encryption happens in the caller) ------------
    def keys(self, user_id: int) -> dict[str, str]:
        with self.conn() as c:
            return {r["name"]: r["value"] for r in c.execute("SELECT name, value FROM keys WHERE user_id = ?",
                                                             (user_id,))}

    def set_key(self, user_id: int, name: str, sealed: str | None) -> None:
        with self.conn() as c:
            if sealed is None:
                c.execute("DELETE FROM keys WHERE user_id = ? AND name = ?", (user_id, name))
            else:
                c.execute("INSERT INTO keys (user_id, name, value, updated_at) VALUES (?, ?, ?, ?) "
                          "ON CONFLICT(user_id, name) DO UPDATE SET value = excluded.value, "
                          "updated_at = excluded.updated_at", (user_id, name, sealed, time.time()))

    # -- runs --------------------------------------------------------------
    def run(self, user_id: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM runs WHERE user_id = ?", (user_id,)).fetchone()

    def update_run(self, user_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE user_id = ?", (*fields.values(), user_id))

    def queue(self, user_id: int, slack_mode: str = "auto") -> bool:
        """Queue a run unless one is already queued or running."""
        with self.conn() as c:
            cur = c.execute("UPDATE runs SET state = 'queued', queued_at = ?, slack_mode = ? "
                            "WHERE user_id = ? AND state NOT IN ('queued', 'running')",
                            (time.time(), slack_mode, user_id))
            return cur.rowcount == 1

    def claim(self, user_id: int, owner: str, seconds: float, *, start_run: bool) -> bool:
        """Take this user's lease if nobody holds it. With start_run, only a queued run is claimed and
        it moves to running. Atomic, so several processes never work on one user's files at once."""
        now = time.time()
        with self.conn() as c:
            if start_run:
                cur = c.execute("UPDATE runs SET lease_until = ?, lease_owner = ?, state = 'running', started_at = ? "
                                "WHERE user_id = ? AND state = 'queued' AND lease_until < ?",
                                (now + seconds, owner, now, user_id, now))
            else:
                cur = c.execute("UPDATE runs SET lease_until = ?, lease_owner = ? WHERE user_id = ? AND lease_until < ?",
                                (now + seconds, owner, user_id, now))
            return cur.rowcount == 1

    def release(self, user_id: int, owner: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET lease_until = 0, lease_owner = NULL WHERE user_id = ? AND lease_owner = ?",
                      (user_id, owner))

    def queue_due(self, now: float, required_keys: tuple[str, ...]) -> int:
        """Queue every onboarded user whose next run is due (and who has the required keys)."""
        need = " ".join(f"AND EXISTS (SELECT 1 FROM keys k WHERE k.user_id = r.user_id AND k.name = '{k}')"
                        for k in required_keys)
        with self.conn() as c:
            # Runs orphaned by a crash (lease expired while running) go back in the queue.
            c.execute("UPDATE runs SET state = 'queued' WHERE state = 'running' AND lease_until < ?", (now,))
            cur = c.execute(
                "UPDATE runs SET state = 'queued', queued_at = ? WHERE user_id IN ("
                " SELECT r.user_id FROM runs r JOIN users u ON u.id = r.user_id"
                " WHERE u.onboarded = 1 AND r.state NOT IN ('queued', 'running')"
                f" AND r.next_run_at IS NOT NULL AND r.next_run_at <= ? {need})", (now, now))
            return cur.rowcount

    def queued(self, limit: int) -> list[int]:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT user_id FROM runs WHERE state = 'queued' AND lease_until < ? "
                                            "ORDER BY queued_at LIMIT ?", (time.time(), limit))]

    def onboarded_users(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT u.id, u.settings, r.last_deliver_at, r.state FROM users u "
                             "JOIN runs r ON r.user_id = u.id WHERE u.onboarded = 1").fetchall()
