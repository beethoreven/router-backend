"""資料來源快取的小狀態，以及「今天」一律用台北時間。"""

import traceback

import pytest

from apps.mama_stocker import app as backend
from apps.mama_stocker.stock_utils import sources


def test_refresh_ahead_restores_outer_state_when_nested():
    with sources.refresh_ahead():
        with sources.refresh_ahead():
            pass
        assert sources._ahead.on is True
    assert sources._ahead.on is False


def test_cooldown_reraise_does_not_grow_traceback(monkeypatch):
    key = ("test", "cooldown")
    monkeypatch.setattr(sources, "_failed", {})

    def load():
        raise ValueError("掛了")

    depths = []
    for _ in range(4):
        with pytest.raises(ValueError) as info:
            sources._cached(key, 3600, load)
        depths.append(len(traceback.extract_tb(info.value.__traceback__)))
    sources._locks.pop(key, None)
    sources._last.pop(key, None)
    # 第一次是真的去抓（堆疊比較深），之後三次都是冷卻期間直接拋，深度要一樣。
    assert len(set(depths[1:])) == 1


class _EmptyPage:
    content = b"<table></table>"

    def raise_for_status(self):
        pass


def test_new_year_in_taipei_while_server_is_still_in_last_year(at, monkeypatch):
    """台北元旦 03:00 = UTC 12/31 19:00。今年的表是空的是正常，去年的空表才是壞了。"""
    at(2027, 1, 1, 3, 0)
    monkeypatch.setattr(sources.requests, "post", lambda *a, **k: _EmptyPage())
    monkeypatch.setattr(sources, "_cache", {})
    monkeypatch.setattr(sources, "_failed", {})
    assert sources.mops_dividends("tse", 2027) == {}
    with pytest.raises(ValueError):
        sources.mops_dividends("tse", 2026)


def _mis(z="2400.0"):
    return {"msgArray": [{"d": "20261112", "z": z, "y": "2395.0"}]}


def test_realtime_is_fetched_once_per_quote_ttl(monkeypatch):
    """同一支股票被好幾個人同時問，只打一次證交所；過了 _QUOTE_TTL 才再打。"""
    asked = []
    monkeypatch.setattr(sources, "_get_json", lambda url, **p: asked.append(p["ex_ch"]) or _mis())
    now = [1_800_000_000.0]
    monkeypatch.setattr(sources.time, "time", lambda: now[0])
    first = sources.realtime("tse", "2330")
    assert first["last"] == 2400.0
    assert sources.realtime("tse", "2330") == first
    sources.realtime("tse", "2317")          # 別支股票各算各的
    assert asked == ["tse_2330.tw", "tse_2317.tw"]
    now[0] += sources._QUOTE_TTL
    sources.realtime("tse", "2330")
    assert asked == ["tse_2330.tw", "tse_2317.tw", "tse_2330.tw"]


def test_realtime_failure_is_not_retried_within_retry_after(monkeypatch):
    """證交所不回的時候，不要每一則訊息都再打一次（打太兇會被擋 IP）。"""
    tries = []

    def down(url, **params):
        tries.append(1)
        raise ConnectionError("Remote end closed connection without response")
    monkeypatch.setattr(sources, "_get_json", down)
    for _ in range(3):
        with pytest.raises(ConnectionError):
            sources.realtime("tse", "2330")
    # 第一次問：打一次、重試一次。之後 _RETRY_AFTER 之內直接回失敗。
    assert len(tries) == 2


def test_warm_up_uses_taipei_year(at, monkeypatch):
    at(2027, 1, 1, 3, 0)
    asked = []
    for name in ("twse_quotes", "tpex_quotes", "twse_yields", "tpex_yields",
                 "tpex_upcoming_dividends"):
        monkeypatch.setattr(sources, name, lambda: {})
    monkeypatch.setattr(sources, "mops_dividends", lambda m, y: asked.append(y) or {})
    monkeypatch.setattr(sources, "tpex_past_dividends", lambda today: asked.append(today) or {})
    backend._warm_up()
    years = sorted(y for y in asked if isinstance(y, int))
    assert years == [2026, 2026, 2027, 2027]
    assert [str(d) for d in asked if not isinstance(d, int)] == ["2027-01-01"]
