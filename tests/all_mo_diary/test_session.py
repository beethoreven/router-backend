"""Sign-in exchange (POST /diary/api/v1/session) and the stateless app pass."""

from __future__ import annotations

import pytest

from apps.all_mo_diary.auth import app_pass
from router.root import build
from shared import google_auth

ENV = {"DIARY__GOOGLE_CLIENT_ID": "web-client.apps.googleusercontent.com", "DIARY__SESSION_SECRET": "s3cret"}


@pytest.fixture
def client(monkeypatch):
    def fake_verify(token, client_id):
        assert client_id == ENV["DIARY__GOOGLE_CLIENT_ID"]
        if token == "good-id-token":
            return {"sub": "1234", "email": "me@example.com"}
        return None

    monkeypatch.setattr(google_auth, "verify", fake_verify)
    return build(environ=ENV).test_client()


def test_good_id_token_gets_a_pass(client):
    resp = client.post("/diary/api/v1/session", headers={"Authorization": "Bearer good-id-token"})
    assert resp.status_code == 200
    body = resp.get_json()
    claims = app_pass.verify("s3cret", body["token"])
    assert claims == {"sub": "1234", "email": "me@example.com", "renew": False}
    assert body["expires_at"] > 0


def test_bad_or_missing_id_token_is_401_json(client):
    for headers in ({"Authorization": "Bearer nope"}, {}):
        resp = client.post("/diary/api/v1/session", headers=headers)
        assert resp.status_code == 401 and resp.get_json()["code"] == "invalid_token"


def test_not_configured_is_503(monkeypatch):
    resp = build(environ={}).test_client().post("/diary/api/v1/session", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503 and resp.get_json()["code"] == "not_configured"


def test_google_unreachable_is_503_not_401(monkeypatch):
    """Google's keys being unreachable must not read as "your sign-in is invalid"."""
    def unavailable(token, client_id):
        raise google_auth.VerificationUnavailable()

    monkeypatch.setattr(google_auth, "verify", unavailable)
    resp = build(environ=ENV).test_client().post(
        "/diary/api/v1/session", headers={"Authorization": "Bearer some-id-token"})
    assert resp.status_code == 503 and resp.get_json()["code"] == "auth_unavailable"


def test_authenticate_accepts_only_our_pass():
    from apps.all_mo_diary import routes

    app = build(environ=ENV)
    diary = None
    for prefix, lazy in app.extensions["router.mounts"].items():
        diary = lazy.load()
    token, _ = app_pass.issue("s3cret", "1", "a@b.c")
    with diary.test_request_context(headers={"Authorization": f"Bearer {token}"}):
        claims, err = routes.authenticate()
        assert err is None and claims["email"] == "a@b.c"
    with diary.test_request_context(headers={"Authorization": "Bearer good-id-token"}):
        claims, err = routes.authenticate()
        assert claims is None and err[1] == 401
