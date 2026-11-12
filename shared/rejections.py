"""Rejected requests are counted, not logged one by one.

Under a flood of bad requests, a log line per request is itself the cost, and it can push real errors
out of the log. Each app keeps its own RejectionLog and writes at most one summary line a minute,
with counts only and no source information (the apps don't keep IPs).

    rejections = RejectionLog("all-mo-diary")
    rejections.note("invalid pass")
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter

log = logging.getLogger(__name__)


class RejectionLog:
    def __init__(self, app: str, window_seconds: float = 60):
        self.app = app
        self._window = window_seconds
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._counts: Counter = Counter()

    def note(self, reason: str) -> None:
        with self._lock:
            now = time.monotonic()
            if now - self._start >= self._window:
                if self._counts:
                    summary = "、".join(f"{k} {v} 次" for k, v in self._counts.most_common())
                    log.warning("[%s] 過去一分鐘拒絕的請求：%s", self.app, summary)
                    self._counts = Counter()
                self._start = now
            self._counts[reason] += 1

    def clear(self) -> None:
        with self._lock:
            self._counts = Counter()
            self._start = time.monotonic()
