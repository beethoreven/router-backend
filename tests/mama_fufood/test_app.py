"""掛在 router 底下的 mama-fufood:/ready、CORS、登入通行證、連不上 Google 的情況。"""

from __future__ import annotations

import time

import pytest

from apps.mama_fufood import cache
from apps.mama_fufood import db as app_db
from apps.mama_fufood.auth import admin, app_pass
from shared import google_auth
from tests.mama_fufood.conftest import MAMA_FUFOOD_ENV, bearer, pass_headers

DAY = 24 * 60 * 60
TEST_SECRET = MAMA_FUFOOD_ENV["MAMA_FUFOOD__SESSION_SECRET"]


def _boom(*_a, **_k):
    raise AssertionError("這條路徑不應該碰資料庫")


# ── /ready 與 router ──────────────────────────────────────────

def test_router_health_never_loads_or_touches_the_database(service, monkeypatch):
    monkeypatch.setattr(app_db.database, "connection", _boom)
    resp = service.test_client().get("/health")
    assert resp.status_code == 200 and resp.get_json()["ok"] is True


def test_router_ready_reports_the_app_loaded(service):
    resp = service.test_client().get("/ready")
    assert resp.get_json()["apps"]["mama-fufood"]["loaded"] is True


def test_ready_never_touches_the_database(client, monkeypatch):
    """/mama-fufood/ready 不需要登入,所以不能碰資料庫(否則任何人都能讓 Neon 一直醒著)。"""
    monkeypatch.setattr(app_db.database, "connection", _boom)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert "database_configured" in resp.get_json()


def test_ready_after_cache_loads(client, db):
    assert client.get("/ready").status_code == 503
    cache.public.reload()
    resp = client.get("/ready")
    assert resp.status_code == 200 and resp.get_json()["database_last_ok"] is not None


def test_no_health_of_its_own(client):
    """保活打的是 router 的 /health,mama-fufood 自己不定義。"""
    assert client.get("/health").status_code == 404


def test_unknown_url_is_json(client):
    resp = client.get("/nope")
    assert resp.status_code == 404 and resp.get_json()["code"] == "http_404"


def test_cors_only_for_the_admin_site(client):
    resp = client.options("/api/admin/overview", headers={
        "Origin": "http://localhost:8003", "Access-Control-Request-Method": "GET"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:8003"
    resp = client.options("/api/admin/overview", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "Access-Control-Allow-Origin" not in resp.headers
    resp = client.options("/api/v1/check", headers={
        "Origin": "http://localhost:8003", "Access-Control-Request-Method": "POST"})
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_loader_starts_on_first_request_not_at_load(mama_fufood_env, monkeypatch):
    """背景載入要等第一個請求才開始,載入 app 時不能開執行緒。"""
    from router.dispatcher import Mount
    from router.root import build

    calls = []
    monkeypatch.setattr(cache.public, "start_background_loader", lambda: calls.append(1))
    env = {**mama_fufood_env, "MAMA_FUFOOD__START_CACHE_LOADER": "1"}
    service = build(mounts=(Mount("mama-fufood", "apps.mama_fufood.app:create_app"),), environ=env)
    client = service.test_client()
    client.get("/ready")  # 只載入 app,不經過 mama-fufood 的 before_request
    assert calls == []
    client.get("/mama-fufood/ready")
    assert calls == [1]


# ── 登入通行證 ────────────────────────────────────────────────

def test_pass_format_is_unchanged():
    """前綴與用途字串不能改,改了已經發出去的通行證會失效(已知值由 tests/shared/test_auth.py 釘住)。"""
    assert app_pass.prefix == "mff1."
    token, exp = app_pass.issue(TEST_SECRET, "user-1", "mom@example.com")
    assert exp - time.time() == pytest.approx(180 * DAY, abs=5)
    assert app_pass.verify(TEST_SECRET, token) == {"sub": "user-1", "email": "mom@example.com", "renew": False}
    # 後台的 session 和通行證不能互用。
    assert not admin.sessions.looks_valid(token)


def test_admin_session_keeps_its_old_table_and_purpose():
    assert admin.sessions.table == "sessions"
    assert admin.admins.table == "admins"


def test_session_endpoint_exchanges_a_google_token(client):
    resp = client.post("/api/v1/session", headers=bearer("user-9", "dad@example.com"))
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert app_pass.verify(TEST_SECRET, data["token"])["sub"] == "user-9"
    assert client.post("/api/v1/session", headers={"Authorization": "Bearer nope"}).status_code == 401
    # 通行證本身不能拿來換新的通行證(只有 Google 登入才能換)。
    assert client.post("/api/v1/session", headers={"Authorization": f"Bearer {data['token']}"}).status_code == 401


def test_daily_check_accepts_the_pass_and_renews_old_ones(client, db):
    body = {"app_version": "1.0.0", "datasets": {"food_list": {"version": None, "formats": [1]}}}
    resp = client.post("/api/v1/check", json=body, headers=pass_headers())
    assert resp.status_code == 200, resp.get_json()
    assert "session_token" not in resp.get_json()

    data = client.post("/api/v1/check", json=body,
                       headers=pass_headers(now=time.time() - 40 * DAY)).get_json()
    assert app_pass.verify(TEST_SECRET, data["session_token"]) == {
        "sub": "user-1", "email": "mom@example.com", "renew": False}
    assert data["session_expires_at"] > time.time()


def test_old_apps_sending_a_google_token_still_work(client, db):
    """通行證之前發出去的 App 每次都帶 Google ID Token,還要能用。"""
    body = {"app_version": "1.0.0", "datasets": {"food_list": {"version": None, "formats": [1]}}}
    assert client.post("/api/v1/check", json=body, headers=bearer()).status_code == 200


def test_google_unreachable_is_503_not_401(client, monkeypatch):
    """連不上 Google 不是使用者的錯:回 503,App 保持登入、晚點再試。"""
    def unavailable(_token, _client_id):
        raise google_auth.VerificationUnavailable()

    monkeypatch.setattr(google_auth, "verify", unavailable)
    resp = client.post("/api/v1/session", headers=bearer())
    assert resp.status_code == 503 and resp.get_json()["code"] == "auth_unavailable"
    resp = client.post("/api/admin/login", json={"credential": "tok:a:admin@example.com"})
    assert resp.status_code == 503
