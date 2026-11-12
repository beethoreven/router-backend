"""mama-stocker 的測試設定。

- 不連 LINE、不連證交所：會對外連線的地方各測試自己換掉。
- 要資料庫的測試用 `mama_stocker_db`（底下是 tests/conftest.py 的 empty_database，沒設 TEST_DATABASE_URL 就跳過）。
- `mama_stocker` 是整台服務的 test client，mama-stocker 掛在 /mama-stocker，跟線上一樣經過 router。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.conftest import TEST_DATABASE_URL

MAMA_STOCKER_ENV = {
    "MAMA_STOCKER__LINE_CHANNEL_SECRET": "test-secret",
    "MAMA_STOCKER__LINE_CHANNEL_ACCESS_TOKEN": "test-token",
    "MAMA_STOCKER__START_WARM_UP": "0",
}


@pytest.fixture(autouse=True)
def mama_stocker_config():
    """每個測試都從乾淨的設定與記憶體狀態開始（不需要 router 的測試也讀得到 config.get()）。"""
    from apps.mama_stocker import config
    from apps.mama_stocker.db import connection, speakers, users

    config.set_current(config.StockerConfig(
        line_channel_secret="test-secret", line_channel_access_token="test-token",
        database_url="postgresql://test.invalid/test", start_warm_up=False))
    users._allowed, users._expires_at, users._loaded_at = None, 0.0, 0.0
    speakers._recorded.clear()
    connection.database.reset_for_tests()
    yield
    config.set_current(config.StockerConfig())


@pytest.fixture
def mama_stocker_env():
    return dict(MAMA_STOCKER_ENV)


@pytest.fixture
def mama_stocker(mama_stocker_env):
    """整台服務的 test client，mama-stocker 掛在 /mama-stocker。"""
    from router.root import build

    return build(environ=mama_stocker_env).test_client()


@pytest.fixture
def mama_stocker_db(empty_database):
    """清空的測試資料庫，並讓 mama-stocker 連到它（第一次連線時建表）。"""
    from apps.mama_stocker import config
    from apps.mama_stocker.db import connection

    config.set_current(config.StockerConfig(
        line_channel_secret="test-secret", line_channel_access_token="test-token",
        database_url=TEST_DATABASE_URL, start_warm_up=False))
    connection.database.reset_for_tests()
    return TEST_DATABASE_URL


@pytest.fixture
def at(monkeypatch):
    """把 mama-stocker 的「現在」定在台北時間的某一刻：at(2026, 11, 10) 或 at(2026, 11, 10, 9, 30)。"""
    from apps.mama_stocker.stock_utils import clock

    def set_now(*args):
        fixed = datetime(*args, tzinfo=clock.TAIPEI)
        monkeypatch.setattr(clock, "now", lambda: fixed)
        return fixed
    return set_now
