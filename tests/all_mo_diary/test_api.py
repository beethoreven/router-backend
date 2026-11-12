"""Daily check, feedback and the admin console, against a real (local) Postgres."""

from __future__ import annotations

import base64
import gzip
import time
import uuid

import pytest

from tests.all_mo_diary.conftest import admin_headers, pass_for

CHECK = "/all-mo-diary/api/v1/check"
FEEDBACK = "/all-mo-diary/api/v1/feedback"
ADMIN = "/all-mo-diary/api/admin"


def _check(client, headers=None, **body):
    data = {"app_version": "0.1.0", "platform": "android", "fonts_version": None, **body}
    return client.post(CHECK, json=data, headers=headers if headers is not None else pass_for())


def _feedback(fid=None, **extra):
    return {"id": fid or str(uuid.uuid4()), "kind": "suggestion", "description": "很好用",
            "device_time": "2026-11-11T12:00:00+08:00", "install_id": str(uuid.uuid4()), "app_version": "0.1.0",
            "build_number": "10", "device_model": "Pixel", "os_version": "Android 15", "snapshot": {"diaries": 3},
            **extra}


# ── Daily check ───────────────────────────────────────────────

def test_check_needs_our_pass(diary):
    assert _check(diary, headers={}).status_code == 401
    assert _check(diary, headers={"Authorization": "Bearer tok:u1:me@example.com"}).status_code == 401


def test_check_reports_versions_fonts_and_forced_update(diary):
    body = _check(diary).get_json()
    assert body["force_update"] is False and body["fonts"] == {"status": "none"}
    admin = admin_headers(diary)
    assert diary.put(f"{ADMIN}/app-config", json={"min_app_version": "0.2.0", "force_reason": "修正嚴重問題"},
                     headers=admin).status_code == 200
    body = _check(diary).get_json()
    assert body["force_update"] is True and body["force_reason"] == "修正嚴重問題"
    assert _check(diary, app_version="0.2.0").get_json()["force_update"] is False


def test_check_bad_body_is_400(diary):
    assert _check(diary, platform="windows").status_code == 400
    assert _check(diary, app_version="1.0").status_code == 400


def test_check_daily_limit(diary):
    for _ in range(10):
        assert _check(diary).status_code == 200
    resp = _check(diary)
    assert resp.status_code == 429 and resp.get_json()["code"] == "daily_limit"
    assert _check(diary, headers=pass_for("other")).status_code == 200


def test_old_pass_is_renewed(diary):
    old = pass_for(now=time.time() - 40 * 86400)
    body = _check(diary, headers=old).get_json()
    assert body["session_token"].startswith("amd1.") and body["session_expires_at"] > time.time()
    assert "session_token" not in _check(diary).get_json()


# ── Fonts (admin) → check ─────────────────────────────────────

def _font_file_info(name="font-1-abcd.otf.gz"):
    return {"file_name": name, "download_url": f"https://github.com/owner/fonts/releases/download/fonts/{name}",
            "download_bytes": 100, "download_sha256": "a" * 64, "font_bytes": 200, "font_sha256": "b" * 64}


def test_fonts_publish_flow_and_check(diary, monkeypatch):
    from apps.all_mo_diary import github_release

    admin = admin_headers(diary)
    font_id = diary.post(f"{ADMIN}/fonts", json={"name": "霞鶩文楷 TC", "sort": 1, "licensed": True},
                         headers=admin).get_json()["id"]
    resp = diary.post(f"{ADMIN}/fonts/publish", headers=admin)
    assert resp.status_code == 400  # nothing uploaded yet
    monkeypatch.setattr(github_release, "upload", lambda stream, length, fid, repo, token: _font_file_info())
    resp = diary.post(f"{ADMIN}/fonts/{font_id}/file", data=b"x" * 10, headers={**admin, "Content-Type": "application/gzip"})
    assert resp.status_code == 200, resp.get_json()
    published = diary.post(f"{ADMIN}/fonts/publish", headers=admin).get_json()
    assert published["count"] == 1 and published["cache_reloaded"] is True

    body = _check(diary).get_json()
    assert body["fonts"]["status"] == "updated"
    font = body["fonts"]["content"]["fonts"][0]
    assert font["name"] == "霞鶩文楷 TC" and font["licensed"] is True and font["font_sha256"] == "b" * 64
    again = _check(diary, fonts_version=body["fonts"]["version"]).get_json()
    assert again["fonts"] == {"status": "up_to_date", "version": body["fonts"]["version"]}

    # Published fonts can't be deleted, only disabled.
    assert diary.delete(f"{ADMIN}/fonts/{font_id}", headers=admin).status_code == 400
    assert diary.patch(f"{ADMIN}/fonts/{font_id}", json={"disabled": True}, headers=admin).status_code == 200


def test_font_name_validation(diary):
    admin = admin_headers(diary)
    assert diary.post(f"{ADMIN}/fonts", json={"name": "  "}, headers=admin).status_code == 400
    assert diary.post(f"{ADMIN}/fonts", json={"name": "字" * 41}, headers=admin).status_code == 400


# ── Feedback ──────────────────────────────────────────────────

def test_feedback_received_duplicate_and_limit(diary):
    fb = _feedback()
    assert diary.post(FEEDBACK, json=fb, headers=pass_for()).status_code == 201
    dup = diary.post(FEEDBACK, json=fb, headers=pass_for())
    assert dup.status_code == 200 and dup.get_json()["status"] == "duplicate"
    assert diary.post(FEEDBACK, json=fb, headers=pass_for("x", "x@example.com")).status_code == 409
    assert diary.post(FEEDBACK, json=_feedback(), headers=pass_for()).status_code == 201
    assert diary.post(FEEDBACK, json=_feedback(), headers=pass_for()).status_code == 201
    assert diary.post(FEEDBACK, json=_feedback(), headers=pass_for()).status_code == 429


def test_feedback_attachment_rules(diary):
    log = base64.b64encode(gzip.compress(b'{"lines": []}')).decode()
    assert diary.post(FEEDBACK, json=_feedback(attachment=log), headers=pass_for()).status_code == 400  # suggestion
    resp = diary.post(FEEDBACK, json=_feedback(kind="problem", attachment=log), headers=pass_for())
    assert resp.status_code == 201
    bomb = base64.b64encode(gzip.compress(b"0" * (5 * 1024 * 1024))).decode()
    assert diary.post(FEEDBACK, json=_feedback(kind="problem", attachment=bomb), headers=pass_for()).status_code == 413


def test_admin_feedback_management(diary):
    log = base64.b64encode(gzip.compress(b'{"lines": ["auth.signed_in"]}')).decode()
    fid = _feedback(kind="problem", attachment=log)["id"]
    diary.post(FEEDBACK, json=_feedback(fid, kind="problem", attachment=log), headers=pass_for())
    admin = admin_headers(diary)
    listing = diary.get(f"{ADMIN}/feedback", headers=admin).get_json()
    assert listing["total"] == 1 and listing["items"][0]["email"] == "me@example.com"
    item = diary.get(f"{ADMIN}/feedback/{fid}", headers=admin).get_json()
    assert item["logs"] == {"lines": ["auth.signed_in"]}
    assert diary.patch(f"{ADMIN}/feedback/{fid}", json={"status": "done"}, headers=admin).status_code == 200
    assert diary.patch(f"{ADMIN}/feedback/{fid}", json={"status": "??"}, headers=admin).status_code == 400
    assert diary.post(f"{ADMIN}/feedback/delete-by-email", json={"email": "me@example.com"},
                      headers=admin).get_json() == {"deleted": 1}


# ── Admin sign-in and admins ──────────────────────────────────

def test_admin_routes_need_an_admin_session(diary):
    assert diary.get(f"{ADMIN}/overview").status_code == 401
    assert diary.get(f"{ADMIN}/overview", headers={"Authorization": "Bearer " + "0" * 64}).status_code == 401
    resp = diary.post(f"{ADMIN}/login", json={"credential": "tok:u9:someone@example.com"})
    assert resp.status_code == 403


def test_admins_add_remove_and_revocation(diary):
    admin = admin_headers(diary)
    assert diary.post(f"{ADMIN}/admins", json={"email": "Second@Example.com", "note": "第二位管理員"},
                      headers=admin).status_code == 201
    second = admin_headers(diary, "second@example.com")
    assert diary.get(f"{ADMIN}/me", headers=second).get_json() == {"email": "second@example.com"}
    assert diary.delete(f"{ADMIN}/admins/second@example.com", headers=admin).status_code == 200
    assert diary.get(f"{ADMIN}/me", headers=second).status_code == 401
    assert diary.delete(f"{ADMIN}/admins/admin@example.com", headers=admin).status_code == 400


def test_app_config_validation(diary):
    admin = admin_headers(diary)
    assert diary.put(f"{ADMIN}/app-config", json={"min_app_version": "1"}, headers=admin).status_code == 400
    assert diary.put(f"{ADMIN}/app-config", json={"update_url": "http://x"}, headers=admin).status_code == 400
    ok = diary.put(f"{ADMIN}/app-config", json={"announcement": "週末維護"}, headers=admin)
    assert ok.status_code == 200 and ok.get_json()["announcement"] == "週末維護"


def test_cors_only_for_the_admin_console(diary):
    resp = diary.options(f"{ADMIN}/overview", headers={"Origin": "http://localhost:8010",
                                                       "Access-Control-Request-Method": "GET"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:8010"
    resp = diary.options(f"{ADMIN}/overview", headers={"Origin": "https://evil.example",
                                                       "Access-Control-Request-Method": "GET"})
    assert "Access-Control-Allow-Origin" not in resp.headers


@pytest.mark.parametrize("path", ["/all-mo-diary/api/v1/check", "/all-mo-diary/api/v1/feedback"])
def test_forged_pass_never_touches_the_database(path, monkeypatch, diary_env, db_clean):
    """A well-formed but forged pass is rejected before any database connection."""
    from apps.all_mo_diary import db
    from router.root import build

    monkeypatch.setattr(db, "connection", lambda: pytest.fail("touched the database"))
    monkeypatch.setattr(db.database, "connection", lambda: pytest.fail("touched the database"))
    client = build(environ=diary_env).test_client()
    resp = client.post(path, json={}, headers={"Authorization": "Bearer amd1.eyJ2IjoxfQ.forged"})
    assert resp.status_code in (400, 401)
