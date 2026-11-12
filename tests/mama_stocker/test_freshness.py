"""什麼時候該重抓：收盤價表、殖利率表看表上的日期；配息公告平日早晚各一次。"""

import time
from datetime import date, datetime, timedelta

import pytest

from apps.mama_stocker.stock_utils import clock, sources

T = clock.TAIPEI


def _ts(*args) -> float:
    return datetime(*args, tzinfo=T).timestamp()


# ── 交易時間 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("now, expected", [
    ((2026, 11, 12, 8, 0), date(2026, 11, 11)),    # 週四開盤前：昨天
    ((2026, 11, 12, 10, 0), date(2026, 11, 11)),   # 盤中：今天還沒收盤
    ((2026, 11, 12, 13, 30), date(2026, 11, 12)),  # 收盤那一刻起：今天
    ((2026, 11, 14, 12, 0), date(2026, 11, 13)),   # 週六：週五
    ((2026, 11, 16, 8, 0), date(2026, 11, 13)),    # 週一開盤前：週五
])
def test_last_close(now, expected):
    assert clock.last_close(datetime(*now, tzinfo=T)) == expected


@pytest.mark.parametrize("now, expected", [
    ((2026, 11, 12, 8, 29), (2026, 11, 11, 20, 0)),
    ((2026, 11, 12, 8, 30), (2026, 11, 12, 8, 30)),
    ((2026, 11, 12, 19, 59), (2026, 11, 12, 8, 30)),
    ((2026, 11, 12, 23, 0), (2026, 11, 12, 20, 0)),
    ((2026, 11, 15, 12, 0), (2026, 11, 13, 20, 0)),   # 週日：週五晚上
    ((2026, 11, 16, 8, 0), (2026, 11, 13, 20, 0)),    # 週一 08:30 以前：還是週五晚上
])
def test_last_slot(now, expected):
    assert clock.last_slot(datetime(*now, tzinfo=T), sources._ANNOUNCE_AT) == \
        datetime(*expected, tzinfo=T)


# ── 收盤價表、殖利率表 ───────────────────────────────────────────

def _table(day):
    return sources._dated({"2330": 1.0}, [day])


def test_latest_close_table_is_kept_until_next_close(at):
    """昨天傍晚抓到昨天的表：今天盤中官方也不會變，抓得再久以前都不重抓。"""
    at(2026, 11, 12, 13, 29)
    two_days_ago = time.time() - 2 * 86400
    assert sources._dated_fresh(two_days_ago, _table(date(2026, 11, 11)), 0)
    assert sources._dated_fresh(two_days_ago, _table(date(2026, 11, 11)), sources._REFRESH_AHEAD)


def test_after_close_yesterdays_table_falls_back_to_ttl(at):
    at(2026, 11, 12, 14, 0)
    old = _table(date(2026, 11, 11))
    assert not sources._dated_fresh(time.time() - sources._TTL - 1, old, 0)
    assert sources._dated_fresh(time.time() - 60, old, 0)
    # 預抓提前重抓，新表出來時使用者不必等。
    assert not sources._dated_fresh(time.time() - sources._TTL + 600, old, sources._REFRESH_AHEAD)


def test_weekend_keeps_fridays_table(at):
    at(2026, 11, 15, 22, 0)
    assert sources._dated_fresh(_ts(2026, 11, 13, 18, 0), _table(date(2026, 11, 13)), 0)


def test_holiday_weekday_falls_back_to_ttl(at):
    """國定假日被當成交易日：表停在前一天，照 _TTL 重抓，不會一直用舊表。"""
    at(2026, 10, 9, 18, 0)
    old = _table(date(2026, 10, 8))
    assert not sources._dated_fresh(time.time() - sources._TTL - 1, old, 0)


def test_table_without_date_falls_back_to_ttl(at):
    at(2026, 11, 12, 10, 0)
    assert not sources._dated_fresh(time.time() - sources._TTL - 1, _table(None), 0)


def test_yield_tables_carry_the_date(monkeypatch):
    monkeypatch.setattr(sources, "_cache", {})
    monkeypatch.setattr(sources, "_failed", {})
    monkeypatch.setattr(sources, "_get_json", lambda url, **p: [
        {"Date": "1151112", "Code": "2330", "DividendYield": "0.92"},
        {"Date": "1151112", "Code": "9999", "DividendYield": "-"}])
    got = sources.twse_yields()
    assert got == {"2330": 0.92} and got.day == date(2026, 11, 12)


def test_cached_uses_the_table_date_not_its_age(at, monkeypatch):
    """收盤後抓到今天的表，晚上、週末都不再下載。"""
    monkeypatch.setattr(sources, "_cache", {})
    monkeypatch.setattr(sources, "_failed", {})
    calls = []
    monkeypatch.setattr(sources, "_get_json", lambda url, **p: calls.append(url) or [
        {"Date": "1151113", "Code": "2330", "Name": "台積電", "ClosingPrice": "2,390"}])
    at(2026, 11, 13, 18, 0)
    sources.twse_quotes()
    key = ("twse_quotes",)
    sources._cache[key] = (time.time() - 3 * 86400, sources._cache[key][1])
    at(2026, 11, 16, 8, 50)           # 週一開盤前，抓的是三天前
    sources.twse_quotes()
    with sources.refresh_ahead():
        sources.twse_quotes()
    assert len(calls) == 1
    at(2026, 11, 16, 13, 30)          # 週一收盤：可能有新表了
    sources.twse_quotes()
    assert len(calls) == 2


# ── 配息公告 ─────────────────────────────────────────────────────

def test_announcements_refresh_after_each_slot(at):
    fetched = _ts(2026, 11, 12, 9, 0)
    at(2026, 11, 12, 19, 59)
    assert sources._announce_fresh(fetched, {}, 0)
    at(2026, 11, 12, 20, 0)
    assert not sources._announce_fresh(fetched, {}, 0)


def test_announcements_fetched_friday_night_last_the_weekend(at):
    fetched = _ts(2026, 11, 13, 20, 5)
    at(2026, 11, 16, 8, 29)
    assert sources._announce_fresh(fetched, {}, 0)
    at(2026, 11, 16, 8, 30)
    assert not sources._announce_fresh(fetched, {}, 0)


def test_warm_up_fetch_just_before_a_slot_counts_for_that_slot(at):
    """預抓 08:20 先抓，08:30 一過不能又算過期；預抓 08:30 也不必再抓一次。"""
    yesterday_night = _ts(2026, 11, 11, 20, 5)
    at(2026, 11, 12, 8, 20)
    assert sources._announce_fresh(yesterday_night, {}, 0)                         # 使用者：還沒到
    assert not sources._announce_fresh(yesterday_night, {}, sources._REFRESH_AHEAD)  # 預抓：該抓了
    early = _ts(2026, 11, 12, 8, 20)
    at(2026, 11, 12, 8, 31)
    assert sources._announce_fresh(early, {}, 0)
    assert sources._announce_fresh(early, {}, sources._REFRESH_AHEAD)


def test_announcement_sources_use_the_slots(at, monkeypatch):
    monkeypatch.setattr(sources, "_cache", {})
    monkeypatch.setattr(sources, "_failed", {})
    calls = []
    monkeypatch.setattr(sources, "_get_json", lambda url, **p: calls.append(url) or [])
    at(2026, 11, 12, 9, 0)
    sources.tpex_upcoming_dividends()
    key = ("tpex_upcoming",)
    sources._cache[key] = (_ts(2026, 11, 12, 9, 0), sources._cache[key][1])
    at(2026, 11, 12, 19, 0)
    sources.tpex_upcoming_dividends()
    assert len(calls) == 1
    at(2026, 11, 12, 20, 1)
    sources.tpex_upcoming_dividends()
    assert len(calls) == 2
