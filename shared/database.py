"""Postgres access for every app: one connection per call, capped per app and for the whole process.

    database = Database("diary", url=lambda: config.get().database_url, schema=create_tables)

    with database.connection() as conn:
        with conn.cursor() as cur:
            ...

Commits on normal exit, rolls back on an exception, always closes.

Why no connection pool: on Render's free plan a frozen container can leave psycopg's pool workers dead
after waking, and a pool keeps connections open, which keeps Neon awake and burns its free hours. Each
call opens its own connection and closes it.

Two caps:
- per app (`max_connections`), so one busy app can't take every slot;
- per process (PROCESS_MAX_CONNECTIONS), so all apps together stay within what one 512 MB instance and
  its threads can carry. The app slot is taken first, then the process slot, always in that order and
  always with a timeout, so two apps can't deadlock each other.
Waiting longer than `slot_wait` seconds raises DatabaseBusy (shared.flask_app answers 503).

Timeouts: connecting gives up after `connect_timeout`; each statement after `timeout_seconds`
(SET LOCAL, because Neon's -pooler URL is transaction-mode and session settings don't carry); a
database that stops answering mid-query is cut by tcp_user_timeout. prepare_threshold=None turns off
server-side prepared statements, which transaction-mode pooling breaks.

Schema: `schema(cur)` runs once per process on the first connection, inside a transaction holding a
Postgres advisory lock: a process-local lock can't stop two processes — an
overlapping deploy, or the old single-app service still running during a move — from running the DDL
at once. Keep `schema_lock` the same string the app used before moving here, so the old service and
this one queue on the same lock. If it fails, the next connection tries again.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable

import psycopg

PROCESS_MAX_CONNECTIONS = 10

# Process-wide on purpose: this is the cap across all apps (see PROCESS_WIDE in tests/test_isolation.py).
_process_slots = threading.BoundedSemaphore(PROCESS_MAX_CONNECTIONS)


class DatabaseBusy(Exception):
    """All connection slots (this app's or the whole process's) are taken and none freed up in time."""


class DatabaseNotConfigured(Exception):
    """The app's DATABASE_URL is not set."""


def _value(v):
    return v() if callable(v) else v


class Database:
    def __init__(self, app: str, *, url: Callable[[], str], schema: Callable | None = None,
                 schema_lock: str | None = None, max_connections: int = 5, slot_wait: float = 10,
                 connect_timeout: int = 8, timeout_seconds: Callable[[], int] | int = 20):
        if max_connections > PROCESS_MAX_CONNECTIONS:
            raise ValueError("max_connections can't exceed PROCESS_MAX_CONNECTIONS")
        self.app = app
        self._url = url
        self._schema = schema
        self._schema_lock_key = schema_lock or f"{app}:schema"
        self.max_connections = max_connections
        self._slot_wait = slot_wait
        self._connect_timeout = connect_timeout
        self._timeout_seconds = timeout_seconds
        self._slots = threading.BoundedSemaphore(max_connections)
        self._schema_mutex = threading.Lock()
        self._schema_done = schema is None
        # Last successful connection (Unix seconds): /ready-style reports read this instead of connecting.
        self.last_ok: float | None = None

    def configured(self) -> bool:
        return bool(_value(self._url))

    def _ensure_schema(self, conn) -> None:
        if self._schema_done:
            return
        with self._schema_mutex:
            if self._schema_done:
                return
            # conn.transaction(), not `with conn:` — psycopg 3 closes the connection at the end of `with conn:`.
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout = 30000")
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self._schema_lock_key,))
                with conn.cursor() as cur:
                    self._schema(cur)
            self._schema_done = True

    def reset_for_tests(self) -> None:
        """Make the next connection create the schema again (after a test empties the database)."""
        self._schema_done = self._schema is None

    @contextmanager
    def connection(self):
        url = _value(self._url)
        if not url:
            raise DatabaseNotConfigured(f"{self.app}: DATABASE_URL is not set")
        deadline = time.monotonic() + self._slot_wait
        if not self._slots.acquire(timeout=self._slot_wait):
            raise DatabaseBusy(f"{self.app}: connections at this app's limit ({self.max_connections})")
        try:
            if not _process_slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise DatabaseBusy(f"connections at the process limit ({PROCESS_MAX_CONNECTIONS})")
            try:
                timeout_ms = int(_value(self._timeout_seconds)) * 1000
                conn = psycopg.connect(url, connect_timeout=self._connect_timeout,
                                       tcp_user_timeout=timeout_ms, prepare_threshold=None)
                self.last_ok = time.time()
                try:
                    self._ensure_schema(conn)
                    with conn:
                        conn.execute(f"SET LOCAL statement_timeout = {timeout_ms:d}")
                        yield conn
                finally:
                    conn.close()
            finally:
                _process_slots.release()
        finally:
            self._slots.release()
