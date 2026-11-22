"""群組名單：誰會被記、什麼時候刪；LINE ID 的形狀。"""

import logging

import psycopg
import pytest

from apps.mama_stocker.db import speakers, users
from apps.mama_stocker.line_utils import client, handler, ids

AMY = "U" + "a" * 32        # 已開通
NEW = "U" + "1" * 32        # 還沒開通
OTHER = "U" + "2" * 32      # 還沒開通
GROUP = "C" + "c" * 32
GROUP_B = "C" + "d" * 32


# ── LINE ID 的形狀 ───────────────────────────────────────────────

@pytest.mark.parametrize("value, ok", [
    ("U" + "0123456789abcdef" * 2, True),
    ("U" + "a" * 31, False),                 # 少一碼
    ("U" + "A" * 32, False),                 # 大寫
    ("C" + "a" * 32, False),                 # 這是群組的形狀
    ("U" + "a" * 32 + "\n", False),          # 尾巴多一個換行
    ("U" + "a" * 32 + "/../x", False),
    (None, False), ("", False), (12345, False),
])
def test_user_id_shape(value, ok):
    assert ids.user_id(value) == (value if ok else None)


@pytest.mark.parametrize("value, ok", [
    ("C" + "0123456789abcdef" * 2, True),
    ("U" + "a" * 32, False),
    ("C" + "a" * 32 + "/../../v2/bot/message/push", False),
    ("G1", False), (None, False),
])
def test_group_id_shape(value, ok):
    assert ids.group_id(value) == (value if ok else None)


def test_malformed_id_is_logged_without_its_value(caplog):
    """沒有是正常的（電腦版不附 userId），不記；形狀不對才記，而且不記值。"""
    with caplog.at_level(logging.WARNING, logger=ids.log.name):
        assert ids.user_id("Usecret-looking-value") is None
        assert ids.user_id(None) is None
        assert ids.group_id("") is None
    assert len(caplog.records) == 1
    assert "secret-looking" not in caplog.text


def test_name_lookup_refuses_ids_that_would_change_the_url(monkeypatch):
    # 記下來而不是拋錯：group_member_name 自己會接住所有例外，拋錯的話測試看不出有沒有打。
    asked = []
    monkeypatch.setattr(client.requests, "get", lambda url, **kwargs: asked.append(url))
    assert client.group_member_name(GROUP + "/../../message/push", NEW) is None
    assert client.group_member_name(GROUP, "U../profile") is None
    assert asked == []


# ── 哪一種事件做什麼（不連資料庫）──────────────────────────────────

@pytest.fixture
def db_calls(monkeypatch):
    """speakers 的讀寫換成只記下呼叫；LINE 查名字一律回「某人」；只有 AMY 開通。"""
    log = []
    monkeypatch.setattr(speakers, "record",
                        lambda uid, gid, name, via: log.append(("record", uid, gid, name, via)))
    monkeypatch.setattr(speakers, "forget",
                        lambda uids, group_id=None: log.append(("forget", list(uids), group_id)))
    monkeypatch.setattr(speakers, "forget_group", lambda gid: log.append(("forget_group", gid)))
    monkeypatch.setattr(client, "group_member_name", lambda gid, uid: "某人")
    monkeypatch.setattr(users, "_load", lambda: frozenset({AMY}))
    return log


def _in_group(uid=None, group=GROUP):
    source = {"type": "group", "groupId": group}
    if uid is not None:
        source["userId"] = uid
    return source


def test_speakers_and_new_members_are_recorded_but_allowed_people_are_not(db_calls):
    handler.track_group_members({"type": "message", "source": _in_group(NEW)})
    handler.track_group_members({"type": "message", "source": _in_group(AMY)})
    handler.track_group_members({"type": "memberJoined", "source": _in_group(),
                                 "joined": {"members": [{"type": "user", "userId": OTHER}]}})
    assert db_calls == [("record", NEW, GROUP, "某人", "message"),
                        ("record", OTHER, GROUP, "某人", "joined")]


def test_member_left_forgets_them_for_that_group_only(db_calls):
    handler.track_group_members({"type": "memberLeft", "source": _in_group(),
                                 "left": {"members": [{"type": "user", "userId": NEW},
                                                      {"type": "user", "userId": "Ubad"}]}})
    assert db_calls == [("forget", [NEW], GROUP)]


def test_bot_removed_from_a_group_forgets_that_group(db_calls):
    handler.track_group_members({"type": "leave", "source": _in_group()})
    assert db_calls == [("forget_group", GROUP)]


def test_blocking_the_bot_forgets_them(db_calls):
    handler.track_group_members({"type": "unfollow", "source": {"type": "user", "userId": NEW}})
    assert db_calls == [("forget", [NEW], None)]


@pytest.mark.parametrize("event", [
    {"type": "message", "source": _in_group("Ubad")},                       # userId 形狀不對
    {"type": "message", "source": _in_group()},                             # 電腦版發的：沒有 userId
    {"type": "leave", "source": _in_group(group="C/../../v2/bot/message/push")},
    {"type": "memberLeft", "source": _in_group(group="Cbad"),
     "left": {"members": [{"type": "user", "userId": NEW}]}},
    {"type": "unfollow", "source": {"type": "user", "userId": "U" + "a" * 32 + "x"}},
    {"type": "leave", "source": {"type": "room", "roomId": "R" + "a" * 32}},  # 多人聊天室本來就不記
    {"type": "message", "source": {"type": "user", "userId": NEW}},           # 私訊不記
])
def test_malformed_or_unrelated_events_touch_nothing(db_calls, event):
    handler.track_group_members(event)
    assert db_calls == []


def test_malformed_group_id_still_records_the_speaker_without_asking_line(db_calls, monkeypatch):
    def must_not_ask(gid, uid):
        raise AssertionError("groupId 形狀不對，不該拿去組網址")
    monkeypatch.setattr(client, "group_member_name", must_not_ask)
    handler.track_group_members({"type": "message", "source": _in_group(NEW, group="Cbad")})
    assert db_calls == [("record", NEW, None, None, "message")]


# ── 同一個人隔多久再記（不連資料庫）────────────────────────────────

@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(speakers.time, "monotonic", lambda: now[0])
    return now


def test_same_person_is_recorded_again_after_a_day(clock):
    """一個行程只記一次的話，跑了幾個月的服務會讓 seen_at 停住，人還在說話卻被 90 天的規則刪掉。"""
    speakers._remember(NEW)
    clock[0] += speakers._RECORD_EVERY - 1
    assert speakers.already_recorded(NEW)
    clock[0] += 1
    assert not speakers.already_recorded(NEW)


def test_memory_keeps_only_people_seen_within_a_day(clock):
    speakers._remember(NEW)
    clock[0] += speakers._RECORD_EVERY
    speakers._remember(OTHER)
    assert set(speakers._recorded) == {OTHER}


def test_repeated_blocking_reaches_the_database_once_a_day(db_calls, clock):
    """封鎖、解除封鎖可以一直重複：每一次都連資料庫的話，一個人就能讓資料庫一直醒著。"""
    event = {"type": "unfollow", "source": {"type": "user", "userId": NEW}}
    for _ in range(3):
        handler.track_group_members(event)
    assert db_calls == [("forget", [NEW], None)]
    clock[0] += speakers._RECORD_EVERY
    handler.track_group_members(event)
    assert db_calls == [("forget", [NEW], None)] * 2


def test_failed_block_deletion_is_tried_again(monkeypatch, clock):
    tries = []

    def down(uids, group_id=None):
        tries.append(1)
        raise ConnectionError("連不上")
    monkeypatch.setattr(speakers, "forget", down)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            speakers.forget_blocker(NEW)
    assert len(tries) == 2


# ── 真的資料庫（要 TEST_DATABASE_URL）─────────────────────────────

def _rows(url):
    with psycopg.connect(url) as conn:
        return sorted(r[0] for r in conn.execute("SELECT line_user_id FROM group_speakers"))


def _add_user(url, uid, status="active"):
    with psycopg.connect(url) as conn:
        conn.execute("INSERT INTO users (name, line_user_id, status) VALUES ('x', %s, %s)", (uid, status))


def _last_seen_days_ago(url, uid, days):
    with psycopg.connect(url) as conn:
        conn.execute("UPDATE group_speakers SET seen_at = now() - %s * interval '1 day' "
                     "WHERE line_user_id = %s", (days, uid))


def test_people_in_users_are_deleted_whether_active_or_disabled(mama_stocker_db):
    for uid in (AMY, NEW, OTHER):
        speakers.record(uid, GROUP, None, "message")
    _add_user(mama_stocker_db, AMY)
    _add_user(mama_stocker_db, OTHER, "disabled")
    assert [r[1] for r in speakers.pending()] == [NEW]
    assert _rows(mama_stocker_db) == [NEW]


def test_recording_someone_already_in_users_keeps_nothing(mama_stocker_db):
    users._load()   # 建表
    _add_user(mama_stocker_db, OTHER, "disabled")
    speakers.record(OTHER, GROUP, None, "message")
    assert _rows(mama_stocker_db) == []


def test_people_not_seen_for_keep_days_are_deleted(mama_stocker_db):
    speakers.record(NEW, GROUP, None, "message")
    speakers.record(OTHER, GROUP, None, "message")
    _last_seen_days_ago(mama_stocker_db, NEW, speakers.KEEP_DAYS + 1)
    _last_seen_days_ago(mama_stocker_db, OTHER, speakers.KEEP_DAYS - 1)
    assert [r[1] for r in speakers.pending()] == [OTHER]
    assert _rows(mama_stocker_db) == [OTHER]


def test_leaving_a_group_deletes_only_rows_recorded_for_that_group(mama_stocker_db):
    speakers.record(NEW, GROUP, None, "message")
    speakers.record(OTHER, GROUP_B, None, "message")
    speakers.forget([NEW, OTHER], group_id=GROUP)
    assert _rows(mama_stocker_db) == [OTHER]


def test_blocking_the_bot_deletes_the_row_and_the_memory(mama_stocker_db):
    speakers.record(NEW, GROUP_B, None, "message")
    assert speakers.already_recorded(NEW)
    speakers.forget([NEW])
    assert _rows(mama_stocker_db) == []
    # 這個行程也忘了他：他之後在群組裡說話會再被記。
    assert not speakers.already_recorded(NEW)


def test_bot_removed_from_a_group_deletes_that_group(mama_stocker_db):
    speakers.record(NEW, GROUP, None, "message")
    speakers.record(OTHER, GROUP_B, None, "message")
    speakers.forget_group(GROUP)
    assert _rows(mama_stocker_db) == [OTHER]
    assert not speakers.already_recorded(NEW)
    assert speakers.already_recorded(OTHER)
