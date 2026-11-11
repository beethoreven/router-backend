"""每人每天的次數上限,只放在記憶體。

每個 app 為每一種上限各建一個 DailyCounter,計數不會跨 app。

- 以 Google 帳號的固定編號(sub)計數,不會因為改 Email 而變。
- 「一天」以台北時間計,午夜清空、只留當天,不寫資料庫也不寫檔案。
- 後端重啟或部署時會歸零,最多讓人多出一天的額度,可接受。
- 後端維持單一 worker,記憶體裡的計數才會一致。
- 不做黑名單:超過上限只拒絕當天的請求,隔天自動恢復。
"""

from __future__ import annotations

import threading
from datetime import date
from typing import Callable

from shared import timeutil


class DailyCounter:
    def __init__(self, limit: Callable[[], int]):
        self._limit = limit
        self._lock = threading.Lock()
        self._day: date | None = None
        self._counts: dict[str, int] = {}

    def _roll(self) -> None:
        today = timeutil.taipei_today()
        if self._day != today:
            self._day = today
            self._counts = {}

    def try_take(self, key: str) -> bool:
        """還有額度就扣一次並回傳 True;用完了回傳 False(不扣)。"""
        with self._lock:
            self._roll()
            used = self._counts.get(key, 0)
            if used >= self._limit():
                return False
            self._counts[key] = used + 1
            return True

    def give_back(self, key: str) -> None:
        """扣了額度之後才發現這次不該算(例如後端自己出錯)時,把額度還回去。"""
        with self._lock:
            self._roll()
            if self._counts.get(key, 0) > 0:
                self._counts[key] -= 1

    def used(self, key: str) -> int:
        with self._lock:
            self._roll()
            return self._counts.get(key, 0)

    def size(self) -> int:
        with self._lock:
            self._roll()
            return len(self._counts)

    def clear(self) -> None:
        with self._lock:
            self._day = None
            self._counts = {}
