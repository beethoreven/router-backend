"""走勢圖快取在換小時的時候不能互相清掉（images._store）。"""

import pytest

from apps.mama_stocker.line_utils import draw, images
from apps.mama_stocker.stock_utils import market


@pytest.fixture(autouse=True)
def empty_cache():
    images._drawn.clear()
    images._drawing.clear()
    yield
    images._drawn.clear()
    images._drawing.clear()


@pytest.fixture
def fake_chart(monkeypatch):
    sym = market.Symbol("2330", "台積電", "tse")
    monkeypatch.setattr(market, "by_code", lambda code: sym)
    monkeypatch.setattr(market, "five_year_monthly", lambda s: [])
    drawn = []

    def trend_png(name, code, months, *, now):
        drawn.append(now.hour)
        return f"png@{now.hour}".encode()
    monkeypatch.setattr(draw, "trend_png", trend_png)
    return drawn


def test_same_hour_is_drawn_once(at, fake_chart):
    at(2026, 11, 10, 10, 5)
    assert images.render("t2330") == b"png@10"
    assert images.render("t2330") == b"png@10"
    assert fake_chart == [10]


def test_new_hour_clears_older_hours(at, fake_chart):
    at(2026, 11, 10, 10, 5)
    images.render("t2330")
    at(2026, 11, 10, 11, 5)
    images.render("t2330")
    assert [k[2] for k in images._drawn] == [11]
    assert all(k[2] == 11 for k in images._drawing)


def test_late_finisher_from_previous_hour_does_not_evict_newer(at, fake_chart):
    """10:59 開始畫、11 點的圖已經有人畫好之後才畫完的那張。"""
    at(2026, 11, 10, 11, 1)
    images.render("t2330")
    newer_lock = images._drawing[("2330", market.now().date(), 11)]

    with images._guard:
        images._store(("2330", market.now().date(), 10), b"png@10")

    assert images._drawn == {("2330", market.now().date(), 11): b"png@11"}
    assert images._drawing[("2330", market.now().date(), 11)] is newer_lock


def test_next_day_counts_as_newer(at, fake_chart):
    at(2026, 11, 10, 23, 30)
    images.render("t2330")
    at(2026, 11, 11, 0, 5)
    images.render("t2330")
    assert [k[1:] for k in images._drawn] == [(market.now().date(), 0)]
