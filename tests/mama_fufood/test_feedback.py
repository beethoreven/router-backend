"""意見回饋。"""

from __future__ import annotations

import base64
import dataclasses
import gzip
import json
import uuid

import pytest

from apps.mama_fufood import config
from tests.mama_fufood.conftest import admin_headers, bearer



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
    resp = client.post("/api/v1/feedback", json=body, headers=bearer())
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["status"] == "received"
    again = client.post("/api/v1/feedback", json=body, headers=bearer())
    assert again.status_code == 200 and again.get_json()["status"] == "duplicate"
    # 別人拿同一個編號送:不行。
    assert client.post("/api/v1/feedback", json=body, headers=bearer("x", "x@example.com")).status_code == 409


def test_three_per_day_and_duplicates_do_not_count(client, db):
    first = _feedback()
    client.post("/api/v1/feedback", json=first, headers=bearer())
    client.post("/api/v1/feedback", json=first, headers=bearer())
    for _ in range(config.get().feedback_daily_limit - 1):
        assert client.post("/api/v1/feedback", json=_feedback(), headers=bearer()).status_code == 201
    resp = client.post("/api/v1/feedback", json=_feedback(), headers=bearer())
    assert resp.status_code == 429


def test_needs_login(client, db):
    assert client.post("/api/v1/feedback", json=_feedback()).status_code == 401


def test_validation(client, db):
    assert client.post("/api/v1/feedback", json=_feedback(description=" "), headers=bearer()).status_code == 400
    assert client.post("/api/v1/feedback", json=_feedback(description="字" * 1001), headers=bearer()).status_code == 400
    assert client.post("/api/v1/feedback", json=_feedback(kind="other"), headers=bearer()).status_code == 400
    assert client.post("/api/v1/feedback", json=_feedback(id="1"), headers=bearer()).status_code == 400
    # 「提供建議」不附記錄檔。
    resp = client.post("/api/v1/feedback", json=_feedback(attachment=_attachment({"logs": []})), headers=bearer())
    assert resp.status_code == 400


def test_problem_with_logs_and_admin_can_read_them(client, admin_token):
    logs = {"logs": [{"t": "2026-10-23T08:00:00Z", "level": "error", "code": "sync.upload_failed"}]}
    body = _feedback(kind="problem", attachment=_attachment(logs))
    assert client.post("/api/v1/feedback", json=body, headers=bearer()).status_code == 201
    h = admin_headers(admin_token)
    listing = client.get("/api/admin/feedback?kind=problem", headers=h).get_json()
    assert listing["total"] == 1 and listing["items"][0]["email"] == "mom@example.com"
    detail = client.get(f"/api/admin/feedback/{body['id']}", headers=h).get_json()
    assert detail["logs"] == logs
    assert client.patch(f"/api/admin/feedback/{body['id']}", headers=h,
                        json={"status": "done", "admin_note": "已修正"}).status_code == 200


def test_decompression_bomb_is_rejected(client, db):
    bomb = base64.b64encode(gzip.compress(b"0" * (5 * 1024 * 1024))).decode()
    resp = client.post("/api/v1/feedback", json=_feedback(kind="problem", attachment=bomb), headers=bearer())
    assert resp.status_code == 413


def test_attachment_dropped_when_table_is_full(client, db, monkeypatch):
    client.get("/ready")  # 先讓 app 載入(設定在載入時才設好)
    monkeypatch.setattr(config, "_current", dataclasses.replace(config.get(), feedback_table_limit_mb=0))
    resp = client.post("/api/v1/feedback", json=_feedback(kind="problem", attachment=_attachment({"logs": []})),
                       headers=bearer())
    assert resp.status_code == 201
    assert resp.get_json()["attachment_dropped"] is True


def test_delete_by_email(client, admin_token):
    for _ in range(2):
        client.post("/api/v1/feedback", json=_feedback(), headers=bearer())
    client.post("/api/v1/feedback", json=_feedback(), headers=bearer("o", "other@example.com"))
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
        client.post("/api/v1/feedback", json=_feedback(), headers=bearer())
    from apps.mama_fufood import db as app_db

    def boom(*_a, **_k):
        raise AssertionError("超過嘗試次數後不應該碰資料庫")

    monkeypatch.setattr(app_db.database, "connection", boom)
    assert client.post("/api/v1/feedback", json=_feedback(), headers=bearer()).status_code == 429
