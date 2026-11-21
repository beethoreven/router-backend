"""管理後台的「登出所有裝置」與操作紀錄。"""

from __future__ import annotations

import re
import uuid

import psycopg

from tests.mama_fufood.conftest import admin_headers, bearer


def _login(client, sub: str, email: str) -> str:
    resp = client.post("/api/admin/login", json={"credential": f"tok:{sub}:{email}"})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["token"]


def _audit(client, token: str) -> list[dict]:
    resp = client.get("/api/admin/audit", headers=admin_headers(token))
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["items"]


def _actions(client, token: str) -> list[str]:
    return [item["action"] for item in _audit(client, token)]


def test_logout_all_ends_every_session_of_this_admin_only(client, admin_token):
    h = admin_headers(admin_token)
    other_device = _login(client, "admin-sub", "admin@example.com")
    assert client.post("/api/admin/admins", headers=h, json={"email": "second@example.com"}).status_code == 201
    second = _login(client, "s", "second@example.com")

    resp = client.post("/api/admin/logout-all", headers=admin_headers(other_device))
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["sessions"] == 2

    # 這個管理員的兩張 session 都失效(包含剛剛還在快取裡的那一張);別的管理員不受影響。
    assert client.get("/api/admin/me", headers=h).status_code == 401
    assert client.get("/api/admin/me", headers=admin_headers(other_device)).status_code == 401
    assert client.get("/api/admin/me", headers=admin_headers(second)).get_json()["email"] == "second@example.com"
    entry = next(i for i in _audit(client, second) if i["action"] == "logout_all")
    assert entry["email"] == "admin@example.com" and entry["detail"] == {"sessions": 2}


def test_logout_all_needs_a_session(client, db):
    assert client.post("/api/admin/logout-all").status_code == 401


def test_every_admin_route_needs_a_session(service, client, db):
    """後台網頁的鎖只是方便:真正的門是每一支 /api/admin/* 的 require_admin(登入、登出除外)。"""
    from router.root import mounted

    app = mounted(service)["/mama-fufood"].load()
    samples = {"item_id": "1", "feedback_id": str(uuid.uuid4()), "email": "x@example.com", "dataset": "food_list"}
    checked = 0
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/admin/") or rule.endpoint in ("admin.login", "admin.logout"):
            continue
        # <int:item_id>、<uuid:feedback_id>、<path:email>、<dataset> 換成範例值。
        path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", lambda m: samples[m.group(1)], rule.rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            resp = client.open(path, method=method, json={})
            assert resp.status_code == 401, f"{method} {rule.rule} 沒有要求管理員登入({resp.status_code})"
            checked += 1
    assert checked >= 26


def test_audit_needs_an_admin_session(client, db):
    assert client.get("/api/admin/audit").status_code == 401
    assert client.get("/api/admin/audit", headers={"Authorization": "Bearer " + "0" * 64}).status_code == 401


def test_admin_actions_are_recorded_newest_first(client, admin_token):
    h = admin_headers(admin_token)
    new_food = client.post("/api/admin/foods", headers=h, json={"group_id": 7, "name": "百香果", "emoji": "🍈"})
    food_id = new_food.get_json()["id"]
    assert client.patch(f"/api/admin/foods/{food_id}", headers=h, json={"name": "芭樂", "published": False}).status_code == 200
    publish = client.post("/api/admin/publish/food_list", headers=h).get_json()
    assert client.put("/api/admin/app-config", headers=h, json={"min_app_version": "2.0.0", "force_reason": "修正"}).status_code == 200
    assert client.post("/api/admin/admins", headers=h, json={"email": "second@example.com", "note": "家人"}).status_code == 201
    assert client.delete("/api/admin/admins/second@example.com", headers=h).status_code == 200

    items = _audit(client, admin_token)
    assert [i["action"] for i in items] == [
        "admin_remove", "admin_add", "app_config_update", "publish", "food_update", "food_create", "login"]
    by_action = {i["action"]: i for i in items}
    assert all(i["email"] == "admin@example.com" for i in items)
    assert by_action["food_create"]["detail"] == {"id": food_id, "group_id": 7, "name": "百香果", "emoji": "🍈"}
    # 只記白名單欄位,和真正寫進資料庫的一致。
    assert by_action["food_update"]["detail"] == {"id": food_id, "name": "芭樂"}
    assert by_action["publish"]["detail"] == {"dataset": "food_list", "version": publish["version"]}
    assert by_action["app_config_update"]["detail"] == {"min_app_version": "2.0.0", "force_reason": "修正"}
    assert by_action["admin_add"]["detail"] == {"email": "second@example.com"}
    assert by_action["admin_remove"]["detail"] == {"email": "second@example.com"}


def test_failed_changes_leave_no_record(client, admin_token):
    h = admin_headers(admin_token)
    assert client.post("/api/admin/foods", headers=h, json={"group_id": 1, "name": "四個字的", "emoji": "🍚"}).status_code == 400
    assert client.delete("/api/admin/foods/1", headers=h).status_code == 400  # 發布過的不能刪
    assert client.delete("/api/admin/admins/nobody@example.com", headers=h).status_code == 404
    assert client.delete(f"/api/admin/feedback/{uuid.uuid4()}", headers=h).status_code == 404
    assert _actions(client, admin_token) == ["login"]


def test_feedback_actions_are_recorded_without_the_deleted_email(client, admin_token, db):
    h = admin_headers(admin_token)
    fid = str(uuid.uuid4())
    body = {"id": fid, "kind": "suggestion", "description": "好用", "device_time": "2026-11-21T10:00:00+08:00",
            "install_id": str(uuid.uuid4()), "app_version": "2.0.1", "build_number": "2001",
            "device_model": "SM-G991B", "os_version": "Android 14 (API 34)", "snapshot": {}}
    assert client.post("/api/v1/feedback", json=body, headers=bearer()).status_code == 201
    assert client.patch(f"/api/admin/feedback/{fid}", headers=h, json={"status": "done", "admin_note": "已回覆"}).status_code == 200
    assert client.delete(f"/api/admin/feedback/{fid}", headers=h).status_code == 200
    assert client.post("/api/admin/feedback/delete-by-email", headers=h, json={"email": "mom@example.com"}).status_code == 200

    by_action = {i["action"]: i for i in _audit(client, admin_token)}
    assert by_action["feedback_update"]["detail"] == {"id": fid, "status": "done", "fields": ["admin_note", "status"]}
    assert by_action["feedback_delete"]["detail"] == {"id": fid}
    # 個資刪除請求:只記刪了幾則,不留下被刪的 Email。
    assert by_action["feedback_delete_by_email"]["detail"] == {"deleted": 0}
    with psycopg.connect(db) as conn:
        dump = conn.execute("SELECT string_agg(detail::text, ' ') FROM admin_audit").fetchone()[0]
    assert "mom@example.com" not in dump


def test_audit_keeps_no_ip_and_old_entries_are_purged(client, admin_token, db):
    with psycopg.connect(db) as conn:
        columns = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'admin_audit'")]
        assert set(columns) == {"id", "at", "admin_email", "action", "detail"}
        conn.execute("UPDATE admin_audit SET at = now() - interval '400 days'")
    # 下一筆寫入時順便清掉超過保留天數(預設 365 天)的紀錄。
    assert client.post("/api/admin/cache/reload", headers=admin_headers(admin_token)).status_code == 200
    assert _actions(client, admin_token) == ["cache_reload"]
