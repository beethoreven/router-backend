"""Mount each app under its own URL prefix and load it lazily, one failure at a time.

    /diary/...   → apps.all_mo_diary.app.create_app(settings)

Why lazily: an app that fails to import (a bug, a missing package) must take down only its own
prefix, never the whole service. Loading happens on the first request to that prefix, or on
/ready, inside the worker. Nothing here runs at import time and nothing starts a thread
(work started at import time has been seen to silently never run on Render).

A failed load is retried at most every RETRY_SECONDS, so a broken app doesn't re-import on
every request but also recovers without a redeploy if the cause was temporary.

★ An app's create_app(settings) must not do network I/O (connect to its database, call an API).
  It runs under this app's load lock, so a slow or hanging connection would hold every request to
  that app in the queue. Connect lazily on the first request that needs it (shared.snapshot_cache
  shows how).
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from flask import Flask

from router.settings import AppSettings

log = logging.getLogger(__name__)

RETRY_SECONDS = 60


@dataclass(frozen=True)
class Mount:
    """One app: its name (also the URL prefix) and where its factory lives ("module:function")."""

    name: str
    factory: str

    @property
    def prefix(self) -> str:
        return f"/{self.name}"


class LazyApp:
    """A WSGI app that imports and builds the real app the first time it is needed."""

    def __init__(self, mount: Mount, settings: AppSettings, clock: Callable[[], float] = time.monotonic):
        self.mount = mount
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._app: Flask | None = None
        self._error: str | None = None
        self._failed_at: float | None = None

    def load(self) -> Flask | None:
        """Build the app if needed. Returns None while it is failing (see status())."""
        if self._app is not None:
            return self._app
        with self._lock:
            if self._app is not None:
                return self._app
            if self._failed_at is not None and self._clock() - self._failed_at < RETRY_SECONDS:
                return None
            module_name, _, func_name = self.mount.factory.partition(":")
            try:
                factory = getattr(importlib.import_module(module_name), func_name)
                self._app = factory(self._settings)
                self._error = None
                self._failed_at = None
                log.info("app %s loaded", self.mount.name)
            except Exception as exc:  # noqa: BLE001 — any failure must stay inside this prefix
                # Only the exception type goes into the status; the traceback goes to the log.
                self._error = type(exc).__name__
                self._failed_at = self._clock()
                log.exception("app %s failed to load", self.mount.name)
            return self._app

    def status(self) -> dict:
        return {"loaded": self._app is not None, "error": self._error}

    def __call__(self, environ, start_response):
        app = self.load()
        if app is not None:
            return app(environ, start_response)
        body = json.dumps(
            {"error": "這項服務暫時無法使用，請稍後再試", "code": "app_unavailable"},
            ensure_ascii=False).encode()
        start_response("503 SERVICE UNAVAILABLE", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Retry-After", str(RETRY_SECONDS)),
        ])
        return [body]


def build_mounts(mounts: Iterable[Mount], settings: dict[str, AppSettings]) -> dict[str, LazyApp]:
    return {m.prefix: LazyApp(m, settings[m.name]) for m in mounts}
