"""每日檢查(SPEC 第 3 節第 2~4 項)。"""

from __future__ import annotations

import dataclasses

import pytest

from apps.fufood import config
from apps.fufood import db as fufood_db
from apps.fufood import routes as public
from apps.fufood import cache
from tests.fufood.conftest import admin_headers, bearer, load_seed



def _body(food_version=None, unit_version=None, app_version="1.0.0", formats=(1,)):
    return {
        "app_version": app_version,
        "datasets": {
            "food_list": {"version": food_version, "formats": list(formats)},
            "units": {"version": unit_version, "formats": list(formats)},
        },
    }


def test_seed_versions_are_up_to_date(client, db):
    food, units = load_seed("food_list"), load_seed("units")
    resp = client.post("/api/v1/check", json=_body(food["version"], units["version"]), headers=bearer())
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["force_update"] is False
    assert data["datasets"]["food_list"] == {"status": "up_to_date", "version": food["version"], "format_version": 1}
    assert data["datasets"]["units"]["status"] == "up_to_date"
    assert data["checks_left"] == config.get().daily_check_limit - 1


def test_old_version_gets_the_content(client, db):
    resp = client.post("/api/v1/check", json=_body("20200101000000", None), headers=bearer())
    data = resp.get_json()
    assert data["datasets"]["food_list"]["status"] == "updated"
    content = data["datasets"]["food_list"]["content"]
    assert len(content["groups"]) == 11 and len(content["foods"]) == 105
    assert content == load_seed("food_list")["content"]
    assert data["datasets"]["units"]["content"]["units"][0] == {
        "id": 1, "name": "克", "sort": 1, "default": True, "disabled": False}


def test_unknown_data_format_needs_a_newer_app(client, db):
    resp = client.post("/api/v1/check", json=_body(formats=(2,)), headers=bearer())
    data = resp.get_json()
    assert data["datasets"]["food_list"]["status"] == "app_update_required"
    assert "content" not in data["datasets"]["food_list"]
    assert data["notice"]


def test_invalid_token_is_rejected_without_counting(client, db):
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}):
        resp = client.post("/api/v1/check", json=_body(), headers=headers)
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "invalid_token"
    assert public.check_quota.size() == 0


def test_daily_limit_is_per_account(client, db):
    for _ in range(config.get().daily_check_limit):
        assert client.post("/api/v1/check", json=_body(), headers=bearer("a")).status_code == 200
    resp = client.post("/api/v1/check", json=_body(), headers=bearer("a"))
    assert resp.status_code == 429
    assert resp.get_json()["code"] == "daily_limit"
    # 另一個帳號不受影響;同一個人改了 Email(sub 不變)也還是同一個額度。
    assert client.post("/api/v1/check", json=_body(), headers=bearer("b")).status_code == 200
    assert client.post("/api/v1/check", json=_body(), headers=bearer("a", "new@example.com")).status_code == 429


def test_bad_request_does_not_count(client, db):
    resp = client.post("/api/v1/check", json={"app_version": "v1"}, headers=bearer())
    assert resp.status_code == 400
    assert public.check_quota.used("user-1") == 0


def test_not_ready_does_not_count(client, monkeypatch):
    """快取還沒載入、資料庫也連不上:回 503,不扣額度。"""
    client.get("/ready")  # 先讓 app 載入(設定在載入時才設好)
    monkeypatch.setattr(config, "_current",
                        dataclasses.replace(config.get(), database_url="postgresql://127.0.0.1:1/nothing"))
    fufood_db.database.reset_for_tests()
    cache.public.reset_for_tests()
    resp = client.post("/api/v1/check", json=_body(), headers=bearer())
    assert resp.status_code == 503
    assert resp.get_json()["code"] == "not_ready"
    assert public.check_quota.used("user-1") == 0


def test_cache_rebuilds_from_database_after_restart(client, db):
    """部署或重啟後第一次檢查就拿到正確資料(快取是空的,從資料庫載入)。"""
    cache.public.reset_for_tests()
    resp = client.post("/api/v1/check", json=_body(), headers=bearer())
    assert resp.status_code == 200
    assert cache.status()["loaded"] is True


def test_check_never_touches_the_database_once_cached(client, db, monkeypatch):
    client.post("/api/v1/check", json=_body(), headers=bearer())  # 載入快取

    def boom(*_a, **_k):
        raise AssertionError("每日檢查不應該碰資料庫")

    monkeypatch.setattr(cache.public, "_load", boom)
    monkeypatch.setattr(fufood_db.database, "connection", boom)
    assert client.post("/api/v1/check", json=_body(), headers=bearer()).status_code == 200


def test_force_update_shows_the_reason(client, admin_token):
    resp = client.put("/api/admin/app-config", headers=admin_headers(admin_token),
                      json={"min_app_version": "1.2.0", "force_reason": "修正判斷規則"})
    assert resp.status_code == 200, resp.get_json()
    old = client.post("/api/v1/check", json=_body(app_version="1.1.9"), headers=bearer()).get_json()
    assert old["force_update"] is True and old["force_reason"] == "修正判斷規則"
    new = client.post("/api/v1/check", json=_body(app_version="1.2.0"), headers=bearer()).get_json()
    assert new["force_update"] is False and new["force_reason"] == ""


def test_oversized_body_is_rejected(client, db):
    resp = client.post("/api/v1/check", data=b"{" + b" " * 20000 + b"}", headers={
        **bearer(), "Content-Type": "application/json"})
    assert resp.status_code == 413


def test_platform_is_checked(client, db):
    body = _body()
    assert client.post("/api/v1/check", json={**body, "platform": "android"}, headers=bearer()).status_code == 200
    assert client.post("/api/v1/check", json={**body, "platform": "windows"}, headers=bearer()).status_code == 400
