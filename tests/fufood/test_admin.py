"""管理後台:登入、公用資料的編輯與發布、快取、管理員名單。"""

from __future__ import annotations

import pytest

from tests.fufood.conftest import admin_headers, bearer, load_seed



def _check(client, food_version):
    return client.post("/api/v1/check", headers=bearer(), json={
        "app_version": "1.0.0",
        "datasets": {"food_list": {"version": food_version, "formats": [1]}},
    }).get_json()


def test_non_admin_login_is_refused_and_nothing_is_stored(client, db):
    resp = client.post("/api/admin/login", json={"credential": "tok:x:someone@example.com"})
    assert resp.status_code == 403
    import psycopg
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_invalid_credential(client, db):
    assert client.post("/api/admin/login", json={"credential": "garbage"}).status_code == 401


def test_admin_routes_need_a_session(client, db):
    assert client.get("/api/admin/overview").status_code == 401
    assert client.get("/api/admin/overview", headers={"Authorization": "Bearer " + "0" * 64}).status_code == 401


def test_overview_after_seed(client, admin_token):
    data = client.get("/api/admin/overview", headers=admin_headers(admin_token)).get_json()
    assert len(data["draft"]["groups"]) == 11
    assert len(data["draft"]["foods"]) == 105
    assert data["releases"]["food_list"]["has_changes"] is False
    assert data["releases"]["units"]["version"] == load_seed("units")["version"]


def test_publish_flow_updates_the_app(client, admin_token):
    h = admin_headers(admin_token)
    seed_version = load_seed("food_list")["version"]
    resp = client.post("/api/admin/foods", headers=h, json={"group_id": 7, "name": "百香果", "emoji": "🍈"})
    assert resp.status_code == 201, resp.get_json()
    new_id = resp.get_json()["id"]
    assert new_id == 106, "新的 ID 要接在種子資料後面"

    # 還沒發布:App 拿到的還是舊的。
    assert _check(client, seed_version)["datasets"]["food_list"]["status"] == "up_to_date"

    resp = client.post("/api/admin/publish/food_list", headers=h)
    assert resp.status_code == 200, resp.get_json()
    version = resp.get_json()["version"]
    assert version > seed_version and resp.get_json()["cache_reloaded"] is True

    result = _check(client, seed_version)["datasets"]["food_list"]
    assert result["status"] == "updated" and result["version"] == version
    assert any(f["id"] == new_id and f["name"] == "百香果" for f in result["content"]["foods"])

    # 沒有修改就不能再發布。
    assert client.post("/api/admin/publish/food_list", headers=h).status_code == 400


def test_food_names_are_at_most_three_characters(client, admin_token):
    resp = client.post("/api/admin/foods", headers=admin_headers(admin_token),
                       json={"group_id": 1, "name": "四個字的", "emoji": "🍚"})
    assert resp.status_code == 400
    assert "3 個字" in resp.get_json()["error"]


def test_emoji_must_be_exactly_one(client, admin_token):
    resp = client.post("/api/admin/foods", headers=admin_headers(admin_token),
                       json={"group_id": 1, "name": "米", "emoji": "🍚🍚"})
    assert resp.status_code == 400


def test_published_items_cannot_be_deleted_only_disabled(client, admin_token):
    h = admin_headers(admin_token)
    assert client.delete("/api/admin/foods/1", headers=h).status_code == 400
    assert client.patch("/api/admin/foods/1", headers=h, json={"disabled": True}).status_code == 200
    draft = client.get("/api/admin/overview", headers=h).get_json()["draft"]
    assert next(f for f in draft["foods"] if f["id"] == 1)["disabled"] is True


def test_unpublished_draft_can_be_deleted_and_its_id_is_never_reused(client, admin_token):
    h = admin_headers(admin_token)
    first = client.post("/api/admin/foods", headers=h, json={"group_id": 1, "name": "米", "emoji": "🍚"}).get_json()
    assert client.delete(f"/api/admin/foods/{first['id']}", headers=h).status_code == 200
    second = client.post("/api/admin/foods", headers=h, json={"group_id": 1, "name": "米", "emoji": "🍚"}).get_json()
    assert second["id"] == first["id"] + 1


def test_patch_ignores_fields_outside_the_whitelist(client, admin_token):
    """寫入只取白名單欄位。"""
    h = admin_headers(admin_token)
    resp = client.patch("/api/admin/foods/2", headers=h, json={"id": 999, "published": False, "name": "粥"})
    assert resp.status_code == 200
    draft = client.get("/api/admin/overview", headers=h).get_json()["draft"]
    food = next(f for f in draft["foods"] if f["id"] == 2)
    assert food["name"] == "粥" and food["published"] is True
    assert not any(f["id"] == 999 for f in draft["foods"])


def test_units_always_have_exactly_one_enabled_default(client, admin_token):
    h = admin_headers(admin_token)
    assert client.patch("/api/admin/units/1", headers=h, json={"default": False}).status_code == 400
    assert client.patch("/api/admin/units/1", headers=h, json={"disabled": True}).status_code == 400
    assert client.patch("/api/admin/units/2", headers=h, json={"default": True}).status_code == 200
    units = client.get("/api/admin/overview", headers=h).get_json()["draft"]["units"]
    assert [u["id"] for u in units if u["default"]] == [2]
    assert client.delete("/api/admin/units/2", headers=h).status_code == 400
    assert client.post("/api/admin/publish/units", headers=h).status_code == 200


def test_cache_status_and_reload(client, admin_token):
    h = admin_headers(admin_token)
    assert client.post("/api/admin/cache/reload", headers=h).get_json()["loaded"] is True
    data = client.get("/api/admin/overview", headers=h).get_json()
    assert data["cache_consistent"] is True


def test_admin_whitelist(client, admin_token):
    h = admin_headers(admin_token)
    assert client.post("/api/admin/admins", headers=h, json={"email": "Second@Example.com", "note": "第二位管理員"}).status_code == 201
    second = client.post("/api/admin/login", json={"credential": "tok:s:second@example.com"})
    assert second.status_code == 200
    second_token = second.get_json()["token"]
    assert client.get("/api/admin/me", headers=admin_headers(second_token)).get_json()["email"] == "second@example.com"
    assert client.delete("/api/admin/admins/second@example.com", headers=h).status_code == 200
    assert client.get("/api/admin/me", headers=admin_headers(second_token)).status_code == 401
    assert client.delete("/api/admin/admins/admin@example.com", headers=h).status_code == 400


def test_formats_are_written_from_code(client, admin_token):
    rows = client.get("/api/admin/formats", headers=admin_headers(admin_token)).get_json()["formats"]
    by_version = {r["format_version"]: r for r in rows}
    assert set(by_version) == {0, 1}, "只有程式裡定義的格式;程式裡沒有的要清掉"
    assert by_version[1]["status"] == "current" and by_version[1]["min_app_version"] == "1.0.0"
    assert by_version[0]["status"] == "supported" and by_version[0]["min_app_version"] == "0.1.0"


def test_logout(client, admin_token):
    h = admin_headers(admin_token)
    assert client.post("/api/admin/logout", headers=h).status_code == 200
    assert client.get("/api/admin/me", headers=h).status_code == 401
