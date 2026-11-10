"""Sign-in exchange (POST /diary/api/v1/session) and the stateless app pass."""

from __future__ import annotations

import pytest

from apps.diary import app_tokens, google_auth
from router.root import build

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
    claims = app_tokens.verify("s3cret", body["token"])
    assert claims == {"sub": "1234", "email": "me@example.com", "renew": False}
    assert body["expires_at"] > 0


def test_bad_or_missing_id_token_is_401_json(client):
    for headers in ({"Authorization": "Bearer nope"}, {}):
        resp = client.post("/diary/api/v1/session", headers=headers)
        assert resp.status_code == 401 and resp.get_json()["code"] == "invalid_token"


def test_not_configured_is_503(monkeypatch):
    resp = build(environ={}).test_client().post("/diary/api/v1/session", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503 and resp.get_json()["code"] == "not_configured"


def test_pass_expiry_renewal_and_tampering():
    token, exp = app_tokens.issue("k", "1", "a@b.c", now=1_000_000)
    assert app_tokens.verify("k", token, now=1_000_000 + 10)["renew"] is False
    assert app_tokens.verify("k", token, now=1_000_000 + 31 * 86400)["renew"] is True
    assert app_tokens.verify("k", token, now=exp) is None  # expired
    assert app_tokens.verify("other-key", token, now=1_000_010) is None  # wrong secret
    body, sig = token[len(app_tokens.PREFIX):].split(".")
    forged = app_tokens.PREFIX + body[:-2] + "AA." + sig
    assert app_tokens.verify("k", forged, now=1_000_010) is None
    assert app_tokens.verify("k", "mff1." + body + "." + sig, now=1_000_010) is None  # mama-fufood prefix


def test_a_raw_google_id_token_is_not_a_pass():
    assert app_tokens.verify("k", "eyJhbGciOiJSUzI1NiIsImtpZCI6IngifQ.eyJ9.sig") is None


def test_shape_checks_reject_without_contacting_google(monkeypatch):
    """Garbage never reaches the network call (known-issue-check #33)."""
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_oauth2_token", lambda *a, **k: pytest.fail("should not verify"))
    assert google_auth.verify("not-a-jwt", "cid") is None
    assert google_auth.verify("a.b.c", "cid") is None  # JWT-shaped but no kid in the header
    assert google_auth.verify("x" * 9000, "cid") is None


def test_authenticate_accepts_only_our_pass():
    from apps.diary import routes

    app = build(environ=ENV)
    diary = None
    for prefix, lazy in app.extensions["router.mounts"].items():
        diary = lazy.load()
    token, _ = app_tokens.issue("s3cret", "1", "a@b.c")
    with diary.test_request_context(headers={"Authorization": f"Bearer {token}"}):
        claims, err = routes.authenticate()
        assert err is None and claims["email"] == "a@b.c"
    with diary.test_request_context(headers={"Authorization": "Bearer good-id-token"}):
        claims, err = routes.authenticate()
        assert claims is None and err[1] == 401
