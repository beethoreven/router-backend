"""管理後台的「登出所有裝置」與操作紀錄(shared.audit_log 接在後台的 blueprint 上)。"""

from __future__ import annotations

import re
import uuid

import psycopg

from tests.mama_fufood.conftest import admin_headers, pass_headers


def _login(client, sub: str, email: str) -> str:
    resp = client.post("/api/admin/login", json={"credential": f"tok:{sub}:{email}"})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["token"]


def _rows(client, token: str, query: str = "") -> list[dict]:
    resp = client.get("/api/admin/audit" + query, headers=admin_headers(token))
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["items"]


def _brief(rows: list[dict]) -> list[tuple]:
    return [(r["actor"], r["action"], r["target"], r["result"]) for r in rows]


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
    assert ("admin@example.com", "admin.logout_all", "", "ok") in _brief(_rows(client, second))


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


def test_writes_and_logins_are_recorded_newest_first(client, admin_token):
    h = admin_headers(admin_token)
    food_id = client.post("/api/admin/foods", headers=h, json={"group_id": 7, "name": "百香果", "emoji": "🍈"}).get_json()["id"]
    assert client.patch(f"/api/admin/foods/{food_id}", headers=h, json={"name": "芭樂", "published": False}).status_code == 200
    assert client.post("/api/admin/foods", headers=h, json={"group_id": 1, "name": "四個字的", "emoji": "🍚"}).status_code == 400
    assert client.post("/api/admin/publish/food_list", headers=h).status_code == 200
    assert client.put("/api/admin/app-config", headers=h,
                      json={"min_app_version": "2.0.0", "force_reason": "修正閃退"}).status_code == 200
    assert client.post("/api/admin/admins", headers=h, json={"email": "second@example.com", "note": "家人"}).status_code == 201
    assert client.delete("/api/admin/admins/second@example.com", headers=h).status_code == 200
    assert client.get("/api/admin/overview", headers=h).status_code == 200  # 讀取不記

    rows = _rows(client, admin_token)
    assert _brief(rows) == [
        ("admin@example.com", "admin.remove_admin", "email=second@example.com", "ok"),
        ("admin@example.com", "admin.add_admin", "second@example.com", "ok"),
        ("admin@example.com", "admin.put_app_config", "", "ok"),
        ("admin@example.com", "admin.publish", "dataset=food_list", "ok"),
        ("admin@example.com", "admin.create_food", "", "failed"),
        ("admin@example.com", "admin.update_food", f"item_id={food_id}", "ok"),
        # 新增的項目編號不在網址裡,由路由補上。
        ("admin@example.com", "admin.create_food", f"item_id={food_id}", "ok"),
        ("admin@example.com", "admin.login", "", "ok"),
    ]
    # 請求的內容(名稱、原因、備註)一個字都不留。
    dump = str(rows)
    assert all(text not in dump for text in ("百香果", "芭樂", "修正閃退", "家人"))


def test_a_non_admin_sign_in_leaves_nothing_behind(client, admin_token, db):
    """隱私權政策寫明網站登入的憑證用完即丟、不寫進資料庫:非管理員被拒的那一次,操作紀錄也不能留下 Email。"""
    resp = client.post("/api/admin/login", json={"credential": "tok:u9:someone@example.com"})
    assert resp.status_code == 403
    assert [r for r in _rows(client, admin_token) if r["result"] != "ok"] == []
    with psycopg.connect(db) as conn:
        for table in ("admin_audit_log", "sessions", "admins"):
            dump = conn.execute(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t").fetchone()[0]
            assert "someone@example.com" not in dump, table


def test_a_former_admin_who_is_turned_away_is_recorded(client, admin_token, db):
    h = admin_headers(admin_token)
    assert client.post("/api/admin/admins", headers=h, json={"email": "second@example.com"}).status_code == 201
    second = _login(client, "s", "second@example.com")
    # 從名單拿掉了,session 卻還在(例如直接在資料庫刪了名單)。
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM admins WHERE email = 'second@example.com'")
    from apps.mama_fufood import auth
    auth.admin.forget_all()
    assert client.get("/api/admin/overview", headers=admin_headers(second)).status_code == 403
    # 完全沒有身分:不記(shared.rejections 只計數)。
    assert client.delete("/api/admin/foods/1").status_code == 401

    denied = [(r["actor"], r["action"]) for r in _rows(client, admin_token) if r["result"] == "denied"]
    assert denied == [("second@example.com", "admin.overview")]


def test_feedback_actions_keep_no_address_or_note(client, admin_token):
    h = admin_headers(admin_token)
    fid = str(uuid.uuid4())
    body = {"id": fid, "kind": "suggestion", "description": "好用", "device_time": "2026-11-21T10:00:00+08:00",
            "install_id": str(uuid.uuid4()), "app_version": "2.0.1", "build_number": "2001",
            "device_model": "SM-G991B", "os_version": "Android 14 (API 34)", "snapshot": {}}
    assert client.post("/api/v1/feedback", json=body, headers=pass_headers()).status_code == 201
    assert client.patch(f"/api/admin/feedback/{fid}", headers=h, json={"status": "done", "admin_note": "已回覆"}).status_code == 200
    assert client.delete(f"/api/admin/feedback/{fid}", headers=h).status_code == 200
    assert client.post("/api/admin/feedback/delete-by-email", headers=h, json={"email": "mom@example.com"}).status_code == 200

    rows = _rows(client, admin_token)
    assert _brief(rows)[:3] == [
        # 個資刪除請求:留下被刪的 Email,就違背了刪除請求。
        ("admin@example.com", "admin.delete_feedback_by_email", "", "ok"),
        ("admin@example.com", "admin.delete_feedback", f"feedback_id={fid}", "ok"),
        ("admin@example.com", "admin.update_feedback", f"feedback_id={fid}", "ok"),
    ]
    dump = str(rows)
    assert "mom@example.com" not in dump and "已回覆" not in dump


def test_pages_go_back_in_time(client, admin_token, db):
    with psycopg.connect(db) as conn:
        for i in range(55):
            conn.execute("INSERT INTO admin_audit_log (actor, action, result) VALUES ('a', %s, 'ok')", (f"x{i}",))
    first = _rows(client, admin_token)
    assert len(first) == 50
    older = _rows(client, admin_token, f"?before={first[-1]['id']}")
    assert len(older) == 6  # 其餘 5 筆加上登入那一筆
    assert older[0]["id"] < first[-1]["id"]


def test_no_ip_or_content_columns_and_rows_past_180_days_go(client, admin_token, db):
    with psycopg.connect(db) as conn:
        columns = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'admin_audit_log'")}
        conn.execute("INSERT INTO admin_audit_log (at, actor, action, result) VALUES "
                     "(now() - interval '181 days', 'old', 'x', 'ok'), (now() - interval '179 days', 'kept', 'x', 'ok')")
    assert columns == {"id", "at", "actor", "action", "target", "result"}
    # 下一筆寫入時順便清掉超過 180 天的紀錄。
    assert client.post("/api/admin/cache/reload", headers=admin_headers(admin_token)).status_code == 200
    actors = [r["actor"] for r in _rows(client, admin_token)]
    assert "old" not in actors and "kept" in actors


def test_a_failed_audit_write_does_not_fail_the_request(client, admin_token, monkeypatch):
    from apps.mama_fufood import auth

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(auth.audit, "record", boom)
    resp = client.put("/api/admin/app-config", headers=admin_headers(admin_token), json={"min_app_version": "2.0.0"})
    assert resp.status_code == 200


def test_requests_without_an_identity_do_not_touch_the_database(client, db, monkeypatch):
    """誰都能送、又沒有 IP 限流的請求,不能叫醒資料庫或讓紀錄表一直變大。"""
    from apps.mama_fufood import admin_routes
    from apps.mama_fufood import db as app_db

    opened = []
    real = app_db.database.connection

    def counting():
        opened.append(1)
        return real()

    monkeypatch.setattr(app_db.database, "connection", counting)
    monkeypatch.setattr(admin_routes.login_attempts, "_limit", lambda: 1)
    # 非管理員第一次登入要查名單(碰資料庫);之後超過次數就在記憶體擋下。
    assert client.post("/api/admin/login", json={"credential": "tok:u9:someone@example.com"}).status_code == 403
    opened.clear()
    assert client.post("/api/admin/login", json={"credential": "tok:u9:someone@example.com"}).status_code == 429
    assert client.post("/api/admin/logout").status_code == 200
    assert client.post("/api/admin/logout", headers={"Authorization": "Bearer " + "0" * 64}).status_code == 200
    assert client.post("/api/admin/login", data=b"{not json", content_type="application/json").status_code == 400
    assert opened == []
