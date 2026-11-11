"""每人每天的次數(只在記憶體,台北時間午夜清空)。"""

from __future__ import annotations

from datetime import datetime, timezone

from shared import timeutil
from shared.quota import DailyCounter


def _at(monkeypatch, iso: str):
    moment = datetime.fromisoformat(iso).astimezone(timezone.utc)
    monkeypatch.setattr(timeutil, "now", lambda: moment)


def test_limit_and_give_back(monkeypatch):
    _at(monkeypatch, "2026-10-23T12:00:00+08:00")
    counter = DailyCounter(lambda: 3)
    assert [counter.try_take("a") for _ in range(4)] == [True, True, True, False]
    assert counter.try_take("b") is True
    counter.give_back("a")
    assert counter.try_take("a") is True
    assert counter.try_take("a") is False


def test_resets_at_taipei_midnight_not_utc_midnight(monkeypatch):
    counter = DailyCounter(lambda: 1)
    _at(monkeypatch, "2026-10-23T23:59:00+08:00")
    assert counter.try_take("a") is True
    assert counter.try_take("a") is False
    # UTC 已經是 16:00 了,但台北還是同一天以前都不清空;台北過了午夜就清空。
    _at(monkeypatch, "2026-10-24T00:00:01+08:00")
    assert counter.try_take("a") is True
    assert counter.size() == 1


def test_only_today_is_kept(monkeypatch):
    counter = DailyCounter(lambda: 10)
    _at(monkeypatch, "2026-10-23T10:00:00+08:00")
    for i in range(100):
        counter.try_take(f"user-{i}")
    _at(monkeypatch, "2026-10-24T10:00:00+08:00")
    assert counter.size() == 0
