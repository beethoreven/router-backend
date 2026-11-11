"""被拒絕的請求只做統計,不逐筆寫日誌(SPEC 第 3 節第 3 項)。

大量無效請求時,逐筆寫日誌本身就是成本,也可能把日誌洗掉。這裡每分鐘最多寫
一行彙總,而且只有次數,沒有任何來源資訊(本專案不使用 IP)。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter

log = logging.getLogger(__name__)

_lock = threading.Lock()
_window_start = time.monotonic()
_counts: Counter = Counter()


def note(reason: str) -> None:
    global _window_start, _counts
    with _lock:
        now = time.monotonic()
        if now - _window_start >= 60 and _counts:
            summary = "、".join(f"{k} {v} 次" for k, v in _counts.most_common())
            log.warning("過去一分鐘拒絕的請求：%s", summary)
            _counts = Counter()
            _window_start = now
        elif now - _window_start >= 60:
            _window_start = now
        _counts[reason] += 1
