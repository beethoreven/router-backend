"""管理後台:登入、公用資料的編輯與發布、快取、管理員名單。"""

from __future__ import annotations

import pytest

from tests.mama_fufood.conftest import admin_headers, load_seed, pass_headers



def _check(client, food_version):
    return client.post("/api/v1/check", headers=pass_headers(), json={
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


@pytest.mark.parametrize("method, path, body", [
    ("post", "/api/admin/groups", {"name": "分\u0000組", "emoji": "🍚"}),
    ("post", "/api/admin/foods", {"group_id": 1, "name": "米\u0000", "emoji": "🍚"}),
    ("patch", "/api/admin/foods/2", {"name": "粥\u0000"}),
    ("post", "/api/admin/units", {"name": "\u0000"}),
    ("put", "/api/admin/app-config", {"force_reason": "原因\u0000"}),
    ("post", "/api/admin/admins", {"email": "a\u0000b@example.com"}),
    ("post", "/api/admin/admins", {"email": "c@example.com", "note": "家\u0000人"}),
    ("post", "/api/admin/feedback/delete-by-email", {"email": "a\u0000b@example.com"}),
])
def test_admin_text_the_database_cannot_store_is_a_bad_request(client, admin_token, method, path, body):
    """後台的文字欄位帶 NUL:回 400,不是 500。"""
    resp = getattr(client, method)(path, headers=admin_headers(admin_token), json=body)
    assert resp.status_code == 400, resp.get_json()


def test_admin_lone_surrogates_note_path_and_filters_are_bad_requests(client, admin_token):
    import json
    import uuid
    h = admin_headers(admin_token)
    # 落單的代理字元要用標準的 json.dumps 才送得出去。
    raw = json.dumps({"group_id": 1, "name": "米\ud800", "emoji": "🍚"}).encode("ascii")
    assert client.post("/api/admin/foods", headers=h, data=raw, content_type="application/json").status_code == 400
    # 意見回饋的處理備註。
    fid = str(uuid.uuid4())
    assert client.post("/api/v1/feedback", headers=pass_headers(), json={
        "id": fid, "kind": "suggestion", "description": "好用", "device_time": "2026-11-21T10:00:00+08:00",
        "install_id": str(uuid.uuid4()), "app_version": "2.0.1", "build_number": "2001",
        "device_model": "SM-G991B", "os_version": "Android 14 (API 34)", "snapshot": {}}).status_code == 201
    assert client.patch(f"/api/admin/feedback/{fid}", headers=h, json={"admin_note": "已\u0000回覆"}).status_code == 400
    # 網址裡的 Email 與意見回饋的篩選條件。
    assert client.delete("/api/admin/admins/a%00b@example.com", headers=h).status_code == 400
    assert client.get("/api/admin/feedback?device_model=SM%00", headers=h).status_code == 400
    # 正常的值照常可以用。
    assert client.get("/api/admin/feedback?device_model=SM-G991B", headers=h).get_json()["total"] == 1


def test_logout(client, admin_token):
    h = admin_headers(admin_token)
    assert client.post("/api/admin/logout", headers=h).status_code == 200
    assert client.get("/api/admin/me", headers=h).status_code == 401
