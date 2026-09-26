"""Persistent storage for OAuth state.

Persistence is not optional here: Claude registers a client once and then holds
tokens indefinitely. If registrations and tokens lived in memory, every restart
of the container would silently break the connector on the phone and require
re-adding it. So this is a small SQLite database in the state directory.

Tokens and authorization codes are stored as SHA-256 hashes. They are
high-entropy random strings, so a plain hash (no salt/KDF) is enough to stop a
database leak from being directly replayable, without the cost of a KDF on
every request.
"""

# Ported from anki-mcp's oauth package, which solved this first. The two are
# near-identical by design: same flow, same threat model, one user each. Fix
# bugs in both.

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# 256 bits, comfortably above the RFC 6749 recommendation of 128 for codes.
TOKEN_BYTES = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id  TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- A login that has been started but not yet completed: created at /authorize,
-- consumed when the login form is submitted successfully.
CREATE TABLE IF NOT EXISTS pending (
    login_id   TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code_hash  TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,          -- 'access' | 'refresh'
    data       TEXT NOT NULL,
    expires_at REAL
);
-- Failed login attempts, for rate limiting. Successful logins clear the row.
CREATE TABLE IF NOT EXISTS login_failures (
    client_ip  TEXT PRIMARY KEY,
    failures   INTEGER NOT NULL,
    first_at   REAL NOT NULL,
    last_at    REAL NOT NULL
);
-- Highest TOTP counter already accepted, to stop replay inside the window.
CREATE TABLE IF NOT EXISTS totp_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_counter  INTEGER NOT NULL
);
"""


def new_secret() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    """Thread-safe SQLite wrapper. All values are JSON blobs keyed by a hash."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False plus an explicit lock: uvicorn's threadpool
        # may touch this from more than one thread.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        # Owner-only: this file holds live credentials.
        path.chmod(0o600)

    # -- clients -----------------------------------------------------------

    def put_client(self, client_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO clients VALUES (?,?,?)",
                (client_id, json.dumps(data), time.time()),
            )
            self._db.commit()

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    # -- pending logins ----------------------------------------------------

    def put_pending(self, login_id: str, data: dict[str, Any], ttl: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pending VALUES (?,?,?)",
                (login_id, json.dumps(data), time.time() + ttl),
            )
            self._db.commit()

    def take_pending(self, login_id: str) -> dict[str, Any] | None:
        """Fetch without consuming -- a failed password attempt must not
        invalidate the login attempt, or a typo would force restarting the
        whole OAuth flow."""
        with self._lock:
            row = self._db.execute(
                "SELECT data, expires_at FROM pending WHERE login_id=?", (login_id,)
            ).fetchone()
        if not row or row[1] < time.time():
            return None
        return json.loads(row[0])

    def drop_pending(self, login_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE login_id=?", (login_id,))
            self._db.commit()

    # -- authorization codes ----------------------------------------------

    def put_code(self, code: str, data: dict[str, Any], expires_at: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO codes VALUES (?,?,?)",
                (hash_secret(code), json.dumps(data), expires_at),
            )
            self._db.commit()

    def get_code(self, code: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data, expires_at FROM codes WHERE code_hash=?",
                (hash_secret(code),),
            ).fetchone()
        if not row or row[1] < time.time():
            return None
        return json.loads(row[0])

    def consume_code(self, code: str) -> None:
        """Codes are strictly single-use (RFC 6749 §10.5)."""
        with self._lock:
            self._db.execute("DELETE FROM codes WHERE code_hash=?", (hash_secret(code),))
            self._db.commit()

    # -- tokens ------------------------------------------------------------

    def put_token(
        self, token: str, kind: str, data: dict[str, Any], expires_at: float | None
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO tokens VALUES (?,?,?,?)",
                (hash_secret(token), kind, json.dumps(data), expires_at),
            )
            self._db.commit()

    def get_token(self, token: str, kind: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data, expires_at FROM tokens WHERE token_hash=? AND kind=?",
                (hash_secret(token), kind),
            ).fetchone()
        if not row:
            return None
        if row[1] is not None and row[1] < time.time():
            return None
        return json.loads(row[0])

    def delete_token(self, token: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM tokens WHERE token_hash=?", (hash_secret(token),)
            )
            self._db.commit()

    # -- rate limiting -----------------------------------------------------

    def record_failure(self, client_ip: str, window: float) -> int:
        """Count a failed login. Returns the failure count inside the window."""
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT failures, first_at FROM login_failures WHERE client_ip=?",
                (client_ip,),
            ).fetchone()
            if row and now - row[1] < window:
                failures, first_at = row[0] + 1, row[1]
            else:
                failures, first_at = 1, now
            self._db.execute(
                "INSERT OR REPLACE INTO login_failures VALUES (?,?,?,?)",
                (client_ip, failures, first_at, now),
            )
            self._db.commit()
        return failures

    def failure_count(self, client_ip: str, window: float) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT failures, first_at FROM login_failures WHERE client_ip=?",
                (client_ip,),
            ).fetchone()
        if not row or time.time() - row[1] >= window:
            return 0
        return row[0]

    def clear_failures(self, client_ip: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM login_failures WHERE client_ip=?", (client_ip,))
            self._db.commit()

    # -- TOTP replay protection -------------------------------------------

    def last_totp_counter(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT last_counter FROM totp_state WHERE id=1"
            ).fetchone()
        return row[0] if row else -1

    def set_totp_counter(self, counter: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO totp_state VALUES (1,?)", (counter,)
            )
            self._db.commit()

    # -- housekeeping ------------------------------------------------------

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE expires_at < ?", (now,))
            self._db.execute("DELETE FROM codes WHERE expires_at < ?", (now,))
            self._db.execute(
                "DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            self._db.commit()
