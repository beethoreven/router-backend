"""The admin console's audit log (shared.audit_log wired into the diary's admin blueprint)."""

from __future__ import annotations

from tests.all_mo_diary.conftest import admin_headers

ADMIN = "/all-mo-diary/api/admin"


def _rows(client, headers):
    resp = client.get(f"{ADMIN}/audit", headers=headers)
    assert resp.status_code == 200
    return resp.get_json()["items"]


def _sql(sql, params=()):
    from apps.all_mo_diary import db

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def test_writes_and_logins_are_recorded_newest_first(diary):
    admin = admin_headers(diary)
    assert diary.put(f"{ADMIN}/app-config", json={"announcement": "週末維護"}, headers=admin).status_code == 200
    assert diary.put(f"{ADMIN}/app-config", json={"update_url": "http://x"}, headers=admin).status_code == 400
    assert diary.post(f"{ADMIN}/admins", json={"email": "second@example.com"}, headers=admin).status_code == 201
    assert diary.delete(f"{ADMIN}/admins/second@example.com", headers=admin).status_code == 200
    diary.get(f"{ADMIN}/overview", headers=admin)  # a read: not recorded

    rows = _rows(diary, admin)
    got = [(r["actor"], r["action"], r["target"], r["result"]) for r in rows]
    assert got == [
        ("admin@example.com", "diary_admin.remove_admin", "email=second@example.com", "ok"),
        ("admin@example.com", "diary_admin.add_admin", "second@example.com", "ok"),
        ("admin@example.com", "diary_admin.put_app_config", "", "failed"),
        ("admin@example.com", "diary_admin.put_app_config", "", "ok"),
        ("admin@example.com", "diary_admin.login", "", "ok"),
    ]
    # nothing from the request body is kept
    assert all("週末維護" not in str(r) for r in rows)


def test_denied_requests_name_who_was_turned_away(diary):
    admin = admin_headers(diary)
    # a non-admin's sign-in is refused and leaves no row: their email isn't kept (privacy policy)
    assert diary.post(f"{ADMIN}/login", json={"credential": "tok:u9:someone@example.com"}).status_code == 403
    # a session whose account stopped being an admin (removed from the list, session left behind)
    assert diary.post(f"{ADMIN}/admins", json={"email": "second@example.com"}, headers=admin).status_code == 201
    second = admin_headers(diary, "second@example.com")
    _sql("DELETE FROM admins WHERE email = 'second@example.com'")
    from apps.all_mo_diary import auth
    auth.admin.forget_all()
    assert diary.get(f"{ADMIN}/overview", headers=second).status_code == 403
    # no identity at all: not recorded (rejections.py counts it)
    assert diary.delete(f"{ADMIN}/fonts/1").status_code == 401

    rows = _rows(diary, admin)
    denied = [(r["actor"], r["action"]) for r in rows if r["result"] == "denied"]
    assert denied == [("second@example.com", "diary_admin.overview")]
    assert "someone@example.com" not in str(rows)


def test_requests_without_an_identity_are_never_recorded(diary):
    admin = admin_headers(diary)
    before = len(_rows(diary, admin))
    assert diary.post(f"{ADMIN}/logout").status_code == 200           # no token, still 200
    assert diary.post(f"{ADMIN}/login", json={"x": 1}).status_code in (400, 401)
    assert diary.post(f"{ADMIN}/login", data="not json",
                      headers={"Content-Type": "application/json"}).status_code >= 400
    assert len(_rows(diary, admin)) == before


def test_only_admins_can_read_it_and_pages_go_back_in_time(diary):
    admin = admin_headers(diary)
    assert diary.get(f"{ADMIN}/audit").status_code == 401
    for i in range(55):
        _sql("INSERT INTO admin_audit_log (actor, action, result) VALUES ('a', %s, 'ok')", (f"x{i}",))
    first = _rows(diary, admin)
    assert len(first) == 50
    older = diary.get(f"{ADMIN}/audit?before={first[-1]['id']}", headers=admin).get_json()["items"]
    assert len(older) == 6  # the other 5 inserts plus the login row
    assert older[0]["id"] < first[-1]["id"]


def test_deletion_requests_do_not_keep_the_address(diary):
    admin = admin_headers(diary)
    assert diary.post(f"{ADMIN}/feedback/delete-by-email", json={"email": "user@example.com"},
                      headers=admin).status_code == 200
    row = _rows(diary, admin)[0]
    assert row["action"] == "diary_admin.delete_feedback_by_email" and row["target"] == ""
    assert "user@example.com" not in str(_rows(diary, admin))


def test_rows_past_180_days_are_dropped_on_the_next_write(diary):
    admin = admin_headers(diary)
    _sql("INSERT INTO admin_audit_log (at, actor, action, result) "
         "VALUES (now() - interval '181 days', 'old', 'x', 'ok'), (now() - interval '179 days', 'kept', 'x', 'ok')")
    diary.post(f"{ADMIN}/cache/reload", headers=admin)
    actors = [r["actor"] for r in _rows(diary, admin)]
    assert "old" not in actors and "kept" in actors


def test_a_failed_audit_write_does_not_fail_the_request(diary, monkeypatch):
    from apps.all_mo_diary import auth

    admin = admin_headers(diary)

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(auth.audit, "record", boom)
    assert diary.put(f"{ADMIN}/app-config", json={"announcement": "x"}, headers=admin).status_code == 200
