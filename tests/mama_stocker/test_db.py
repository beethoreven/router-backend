"""資料庫那幾個有狀態的地方：建表、權限快取、群組名單的「剛加入」。"""

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import psycopg

from apps.mama_stocker import config
from apps.mama_stocker.db import connection, speakers, users
from apps.mama_stocker.line_utils import handler


def _ttl(seconds):
    config.set_current(replace(config.get(), auth_cache_ttl_seconds=seconds))


# ── 權限快取（不連資料庫）────────────────────────────────────────

def test_stale_list_is_used_and_logged_when_db_fails(monkeypatch, caplog):
    _ttl(0)
    monkeypatch.setattr(users, "_load", lambda: frozenset({"Uamy"}))
    assert users.is_allowed("Uamy")

    def down():
        raise ConnectionError("連不上")
    monkeypatch.setattr(users, "_load", down)
    with caplog.at_level(logging.WARNING, logger=users.log.name):
        assert users.is_allowed("Uamy")
        assert not users.is_allowed("Ustranger")
    assert "沿用" in caplog.text


def test_no_list_and_db_down_waits_before_retrying(monkeypatch):
    tries = []

    def down():
        tries.append(1)
        raise ConnectionError("連不上")
    monkeypatch.setattr(users, "_load", down)
    assert not users.is_allowed("Uamy")
    assert not users.is_allowed("Uamy")
    assert len(tries) == 1


def test_cache_ttl_comes_from_config(monkeypatch):
    loads = []
    monkeypatch.setattr(users, "_load", lambda: loads.append(1) or frozenset({"Uamy"}))
    _ttl(3600)
    users.is_allowed("Uamy")
    users.is_allowed("Uamy")
    assert len(loads) == 1


def test_no_database_url_means_nobody_is_allowed(monkeypatch):
    config.set_current(replace(config.get(), database_url=""))
    monkeypatch.setattr(users, "_load", lambda: frozenset({"Uamy"}))
    assert not users.is_allowed("Uamy")


# ── 群組名單的「剛加入」────────────────────────────────────────

def test_just_joined_label_expires_after_a_week(at, monkeypatch):
    now = at(2026, 11, 10, 12, 0)
    rows = [("新來的", "U1", "joined", now - timedelta(days=2)),
            ("早就來了", "U2", "joined", now - timedelta(days=8)),
            ("說過話", "U3", "message", now - timedelta(days=1))]
    monkeypatch.setattr(handler.speakers, "pending", lambda: rows)
    lines = handler._group_list().split("\n")
    assert "新來的（剛加入）" in lines
    assert "早就來了" in lines
    assert "說過話" in lines


def test_just_joined_label_handles_utc_timestamps(at, monkeypatch):
    """資料庫回的 seen_at 可能是 UTC 時區，跟台北時間相減要正確。"""
    at(2026, 11, 10, 12, 0)
    seen = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)   # 台北 11/9 12:00
    monkeypatch.setattr(handler.speakers, "pending", lambda: [("新來的", "U1", "joined", seen)])
    assert "新來的（剛加入）" in handler._group_list()


# ── 真的資料庫（要 TEST_DATABASE_URL）─────────────────────────────

def test_database_settings_match_the_webhook_budget():
    db = connection.database
    assert db.app == "mama-stocker"
    assert db.max_connections == 3
    assert db._schema_lock_key == "mama-stocker:schema"


def test_schema_is_created_on_first_connection(mama_stocker_db):
    assert users._load() == frozenset()
    with psycopg.connect(mama_stocker_db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"users", "group_speakers"} <= tables


def test_schema_is_safe_to_run_again_on_existing_tables(mama_stocker_db):
    users._load()
    connection.database.reset_for_tests()
    assert users._load() == frozenset()


def test_active_users_are_allowed_and_disabled_are_not(mama_stocker_db):
    users._load()   # 建表
    with psycopg.connect(mama_stocker_db) as conn:
        conn.execute("INSERT INTO users (name, line_user_id) VALUES ('小美', 'Uamy')")
        conn.execute("INSERT INTO users (name, line_user_id, status) VALUES ('停用', 'Uoff', 'disabled')")
    assert users._load() == frozenset({"Uamy"})


def test_group_list_shows_only_people_not_yet_allowed(mama_stocker_db):
    speakers.record("Uamy", "G1", "小美", "message")
    speakers.record("Unew", "G1", "新來的", "joined")
    speakers.record("Unew", "G1", None, "message")   # 再看到一次：名字不被蓋掉、via 不變
    with psycopg.connect(mama_stocker_db) as conn:
        conn.execute("INSERT INTO users (name, line_user_id) VALUES ('小美', 'Uamy')")
    rows = speakers.pending()
    assert [(r[0], r[1], r[2]) for r in rows] == [("新來的", "Unew", "joined")]
