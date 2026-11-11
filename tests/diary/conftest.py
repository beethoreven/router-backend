"""The diary's test fixtures.

Google ID token verification is faked: "tok:<sub>:<email>" counts as a valid token.
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DATABASE_URL

DIARY_ENV = {
    "DIARY__GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "DIARY__SESSION_SECRET": "test-session-secret",
    "DIARY__FIRST_ADMIN_EMAIL": "admin@example.com",
    "DIARY__ALLOWED_ORIGINS": "http://localhost:8010",
    "DIARY__START_CACHE_LOADER": "0",
    "DIARY__FONTS_REPO": "owner/fonts",
    "DIARY__GITHUB_TOKEN": "gh-test-token",
}


def fake_verify(token, client_id):
    if not isinstance(token, str) or not token.startswith("tok:"):
        return None
    _, sub, email = token.split(":", 2)
    return {"sub": sub, "email": email.lower()}


@pytest.fixture
def diary_env():
    env = dict(DIARY_ENV)
    if TEST_DATABASE_URL:
        env["DIARY__DATABASE_URL"] = TEST_DATABASE_URL
    return env


@pytest.fixture
def db_clean(empty_database):
    """An empty test database (schema recreated on first connection) and fresh in-memory state."""
    from apps.diary import admin_routes, auth, cache, db, routes

    db.database.reset_for_tests()
    cache.public.reset_for_tests()
    auth.admin.forget_all()
    routes.check_quota.clear()
    routes.feedback_attempts.clear()
    admin_routes.login_attempts.clear()
    yield


@pytest.fixture
def diary(monkeypatch, diary_env, db_clean):
    """A test client for the whole service with the diary mounted at /diary."""
    from router.root import build
    from shared import google_auth

    monkeypatch.setattr(google_auth, "verify", fake_verify)
    return build(environ=diary_env).test_client()


def pass_for(sub: str = "u1", email: str = "me@example.com", now: float | None = None) -> dict:
    from apps.diary.auth import app_pass

    token, _ = app_pass.issue(DIARY_ENV["DIARY__SESSION_SECRET"], sub, email, now=now)
    return {"Authorization": f"Bearer {token}"}


def admin_headers(client, email: str = "admin@example.com") -> dict:
    resp = client.post("/diary/api/admin/login", json={"credential": f"tok:{email}:{email}"})
    assert resp.status_code == 200, resp.get_json()
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}
