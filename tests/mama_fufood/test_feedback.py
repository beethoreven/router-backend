"""意見回饋。"""

from __future__ import annotations

import base64
import dataclasses
import gzip
import json
import uuid

import pytest

from apps.mama_fufood import config
from tests.mama_fufood.conftest import admin_headers, pass_headers



def _feedback(kind="suggestion", attachment=None, **extra):
    body = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "description": "月曆可以放大一點嗎",
        "device_time": "2026-10-23T16:30:00+08:00",
        "install_id": str(uuid.uuid4()),
        "app_version": "1.0.0",
        "build_number": "1000",
        "device_model": "SM-G991B",
        "os_version": "Android 14 (API 34)",
        "snapshot": {"babies": 1, "records": 12},
        "attachment": attachment,
    }
    body.update(extra)
    return body


def _attachment(data: dict) -> str:
    return base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()


def test_submit_and_duplicate(client, db):
    body = _feedback()
    resp = client.post("/api/v1/feedback", json=body, headers=pass_headers())
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["status"] == "received"
    again = client.post("/api/v1/feedback", json=body, headers=pass_headers())
    assert again.status_code == 200 and again.get_json()["status"] == "duplicate"
    # 別人拿同一個編號送:不行。
    assert client.post("/api/v1/feedback", json=body, headers=pass_headers("x", "x@example.com")).status_code == 409


def test_three_per_day_and_duplicates_do_not_count(client, db):
    first = _feedback()
    client.post("/api/v1/feedback", json=first, headers=pass_headers())
    client.post("/api/v1/feedback", json=first, headers=pass_headers())
    for _ in range(config.get().feedback_daily_limit - 1):
        assert client.post("/api/v1/feedback", json=_feedback(), headers=pass_headers()).status_code == 201
    resp = client.post("/api/v1/feedback", json=_feedback(), headers=pass_headers())
    assert resp.status_code == 429


def test_needs_login(client, db):
    assert client.post("/api/v1/feedback", json=_feedback()).status_code == 401


def test_login_is_checked_before_the_body_is_read(client, db, monkeypatch):
    """沒有有效憑證的請求不讀內容(最多約 1.6 MB):回 401,不是「內容太大」。"""
    from apps.mama_fufood import routes as public

    def must_not_read(*_a, **_k):
        raise AssertionError("驗證登入憑證之前不應該讀內容")

    monkeypatch.setattr(public.payload, "read_json", must_not_read)
    resp = client.post("/api/v1/feedback", json=_feedback(), headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "invalid_token"


def test_oversized_body_without_login_is_refused_as_unauthorized(client, db):
    too_big = b"x" * 2_000_000
    resp = client.post("/api/v1/feedback", data=too_big, content_type="application/json",
                       headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401
    # 登入了才看內容:太大照樣是 413。
    resp = client.post("/api/v1/feedback", data=too_big, content_type="application/json", headers=pass_headers())
    assert resp.status_code == 413


def _raw(body: dict) -> bytes:
    """用標準的 json.dumps(非 ASCII 寫成 \\uXXXX):落單的代理字元才送得出去。"""
    return json.dumps(body).encode("ascii")


@pytest.mark.parametrize("change", [
    {"description": "月曆\u0000放大"},
    {"device_model": "SM-G991B\u0000"},
    {"snapshot": {"babies": 1, "note": "a\u0000b"}},
    {"snapshot": {"a\u0000b": 1}},
    {"snapshot": {"list": [{"deep": ["x", "y\u0000"]}]}},
    {"description": "月曆\ud800放大"},
    {"snapshot": {"text": "\udfff"}},
])
def test_text_the_database_cannot_store_is_a_bad_request(client, db, change):
    """NUL 與落單的代理字元存不進 Postgres:回 400,不是 500,也不留下任何東西。"""
    resp = client.post("/api/v1/feedback", data=_raw(_feedback(**change)), content_type="application/json",
                       headers=pass_headers())
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["code"] == "bad_request"
    import psycopg
    with psycopg.connect(db) as conn:
        # 在碰資料庫之前就擋下:空的測試資料庫連表都還沒建。
        if conn.execute("SELECT to_regclass('public.feedback')").fetchone()[0] is not None:
            assert conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0


def test_ordinary_text_with_emoji_and_symbols_is_still_accepted(client, db):
    body = _feedback(description="寶寶吃了🥕胡蘿蔔 😊 —「好吃」\t第二行\n第三行",
                     snapshot={"babies": 1, "emoji": "👶🏻", "quote": "\"'<>&"})
    resp = client.post("/api/v1/feedback", data=_raw(body), content_type="application/json", headers=pass_headers())
    assert resp.status_code == 201, resp.get_json()


def test_validation(client, db):
    assert client.post("/api/v1/feedback", json=_feedback(description=" "), headers=pass_headers()).status_code == 400
    assert client.post("/api/v1/feedback", json=_feedback(description="字" * 1001), headers=pass_headers()).status_code == 400
    assert client.post("/api/v1/feedback", json=_feedback(kind="other"), headers=pass_headers()).status_code == 400
    assert client.post("/api/v1/feedback", json=_feedback(id="1"), headers=pass_headers()).status_code == 400
    # 「提供建議」不附記錄檔。
    resp = client.post("/api/v1/feedback", json=_feedback(attachment=_attachment({"logs": []})), headers=pass_headers())
    assert resp.status_code == 400


def test_problem_with_logs_and_admin_can_read_them(client, admin_token):
    logs = {"logs": [{"t": "2026-10-23T08:00:00Z", "level": "error", "code": "sync.upload_failed"}]}
    body = _feedback(kind="problem", attachment=_attachment(logs))
    assert client.post("/api/v1/feedback", json=body, headers=pass_headers()).status_code == 201
    h = admin_headers(admin_token)
    listing = client.get("/api/admin/feedback?kind=problem", headers=h).get_json()
    assert listing["total"] == 1 and listing["items"][0]["email"] == "mom@example.com"
    detail = client.get(f"/api/admin/feedback/{body['id']}", headers=h).get_json()
    assert detail["logs"] == logs
    assert client.patch(f"/api/admin/feedback/{body['id']}", headers=h,
                        json={"status": "done", "admin_note": "已修正"}).status_code == 200


def test_decompression_bomb_is_rejected(client, db):
    bomb = base64.b64encode(gzip.compress(b"0" * (5 * 1024 * 1024))).decode()
    resp = client.post("/api/v1/feedback", json=_feedback(kind="problem", attachment=bomb), headers=pass_headers())
    assert resp.status_code == 413


def test_attachment_dropped_when_table_is_full(client, db, monkeypatch):
    client.get("/ready")  # 先讓 app 載入(設定在載入時才設好)
    monkeypatch.setattr(config, "_current", dataclasses.replace(config.get(), feedback_table_limit_mb=0))
    resp = client.post("/api/v1/feedback", json=_feedback(kind="problem", attachment=_attachment({"logs": []})),
                       headers=pass_headers())
    assert resp.status_code == 201
    assert resp.get_json()["attachment_dropped"] is True


def test_delete_by_email(client, admin_token):
    for _ in range(2):
        client.post("/api/v1/feedback", json=_feedback(), headers=pass_headers())
    client.post("/api/v1/feedback", json=_feedback(), headers=pass_headers("o", "other@example.com"))
    h = admin_headers(admin_token)
    resp = client.post("/api/admin/feedback/delete-by-email", headers=h, json={"email": "MOM@example.com"})
    assert resp.get_json()["deleted"] == 2
    assert client.get("/api/admin/feedback", headers=h).get_json()["total"] == 1


def test_attempts_are_capped_in_memory_before_the_database(client, db, monkeypatch):
    """登入了也不能無限次叫醒資料庫:超過嘗試次數就在記憶體擋下。"""
    from apps.mama_fufood import routes as public
    monkeypatch.setattr(public, "FEEDBACK_ATTEMPTS_PER_DAY", 2)
    public.feedback_attempts.clear()
    public.feedback_attempts._limit = lambda: public.FEEDBACK_ATTEMPTS_PER_DAY
    for _ in range(2):
        client.post("/api/v1/feedback", json=_feedback(), headers=pass_headers())
    from apps.mama_fufood import db as app_db

    def boom(*_a, **_k):
        raise AssertionError("超過嘗試次數後不應該碰資料庫")

    monkeypatch.setattr(app_db.database, "connection", boom)
    assert client.post("/api/v1/feedback", json=_feedback(), headers=pass_headers()).status_code == 429
