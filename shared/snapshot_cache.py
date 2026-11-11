"""An in-memory copy of an app's public data, reloaded from its database (from mama-fufood's
services/public_cache.py and the diary's cache.py, which were the same code with different contents).

The point is that a hot path (the daily check) never touches the database, so Neon can sleep.

- **The database is the only source; memory is a copy.** The cache is only ever replaced whole by
  loading from the database; nothing edits it in place. An admin write goes to the database first and
  calls reload() after it succeeds.
- **Rebuilt after a restart.** start_background_loader() is meant for a before_request hook: the first
  request starts a thread that loads and retries every `retry_seconds` until it succeeds. Never start
  it at import or in create_app (work started at import never ran on Render — mama-fufood, mama-stocker).
- **Before the first load**, ensure_loaded() tries once (at most every REQUEST_RETRY_MIN_SECONDS) and
  returns None on failure, so the endpoint can answer "try again later".
- ★ ensure_loaded() never waits for the load lock (known-issue-check #16): if another request or the
  background thread is already loading, it returns None at once instead of queueing behind a network
  call.

    public = SnapshotCache("diary", load=read_from_db, retry_seconds=lambda: config.get().cache_retry_seconds,
                           enabled=lambda: bool(config.get().database_url))
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

REQUEST_RETRY_MIN_SECONDS = 5


class SnapshotCache:
    def __init__(self, app: str, *, load: Callable[[], dict], retry_seconds: Callable[[], int],
                 enabled: Callable[[], bool] = lambda: True):
        self.app = app
        self._load = load
        self._retry_seconds = retry_seconds
        self._enabled = enabled
        # The whole snapshot is swapped in one assignment, so readers always get a complete copy.
        self._snapshot: dict | None = None
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._last_attempt = 0.0
        self._loader_started = False

    def snapshot(self) -> dict | None:
        return self._snapshot

    def _reload_locked(self) -> dict:
        self._last_attempt = time.monotonic()
        fresh = self._load()
        self._snapshot = fresh
        log.info("[%s] public data cache loaded", self.app)
        return fresh

    def reload(self) -> dict:
        """Load everything again from the database. Raises on failure; the old snapshot stays."""
        with self._lock:
            return self._reload_locked()

    def ensure_loaded(self) -> dict | None:
        """For requests: the snapshot, or one quick attempt to load it; None if that isn't possible now."""
        if self._snapshot is not None:
            return self._snapshot
        if time.monotonic() - self._last_attempt < REQUEST_RETRY_MIN_SECONDS:
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._reload_locked()
        except Exception as exc:  # noqa: BLE001 — any failure only means "no cache yet"
            log.warning("[%s] public data cache failed to load: %s", self.app, type(exc).__name__)
            return None
        finally:
            self._lock.release()

    def _retry_until_loaded(self) -> None:
        while self._snapshot is None:
            try:
                self.reload()
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] public data cache failed to load, retrying in %s s: %s",
                            self.app, self._retry_seconds(), type(exc).__name__)
            time.sleep(self._retry_seconds())

    def start_background_loader(self) -> None:
        """Call from a before_request hook. Only the first call starts the thread."""
        if self._loader_started or not self._enabled():
            return
        with self._start_lock:
            if self._loader_started:
                return
            self._loader_started = True
        threading.Thread(target=self._retry_until_loaded, name=f"{self.app}-cache-loader", daemon=True).start()

    def reset_for_tests(self) -> None:
        self._snapshot = None
        self._last_attempt = 0.0
        self._loader_started = False
