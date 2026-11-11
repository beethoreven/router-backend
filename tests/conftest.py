"""Shared test setup.

- Tests marked `db` need a throwaway Postgres: TEST_DATABASE_URL (locally: `tools/local_db.sh start`).
  Without it those tests are skipped. **Never point it at a real database**: every db test empties
  the public schema first.
- Google ID token verification is faked: "tok:<sub>:<email>" counts as a valid token.
"""

from __future__ import annotations

import os

import psycopg
import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

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
def db_clean():
    """An empty test database (schema recreated on first connection)."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set (tools/local_db.sh start)")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    from apps.diary import access, cache, db, routes, admin_routes

    db.reset_for_tests()
    cache.reset_for_tests()
    access.forget_all()
    routes.check_quota.clear()
    routes.feedback_attempts.clear()
    admin_routes.login_attempts.clear()
    yield


@pytest.fixture
def diary(monkeypatch, diary_env, db_clean):
    """A test client for the whole service with the diary mounted at /diary."""
    from apps.diary import google_auth
    from router.root import build

    monkeypatch.setattr(google_auth, "verify", fake_verify)
    return build(environ=diary_env).test_client()


def pass_for(sub: str = "u1", email: str = "me@example.com", now: float | None = None) -> dict:
    from apps.diary import app_tokens

    token, _ = app_tokens.issue(DIARY_ENV["DIARY__SESSION_SECRET"], sub, email, now=now)
    return {"Authorization": f"Bearer {token}"}


def admin_headers(client, email: str = "admin@example.com") -> dict:
    resp = client.post("/diary/api/admin/login", json={"credential": f"tok:{email}:{email}"})
    assert resp.status_code == 200, resp.get_json()
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}
