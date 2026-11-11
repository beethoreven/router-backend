"""fireless-war's test fixtures.

Google is never called: ID token verification is faked ("tok:<email>" is valid, "down" means Google's
keys are unreachable), and the Drive / Sheets / Apps Script calls are replaced per test.
"""

from __future__ import annotations

import json

import pytest

SERVICE_ACCOUNT = {"type": "service_account", "client_email": "sa@example.iam.gserviceaccount.com",
                   "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"}

ENV = {
    "FIRELESS_WAR__GOOGLE_CLIENT_ID": "fw-client.apps.googleusercontent.com",
    "FIRELESS_WAR__PERMITTED_USERS": "GM@Example.com, other-gm@example.com",
    "FIRELESS_WAR__SERVICE_ACCOUNT_JSON": json.dumps(SERVICE_ACCOUNT),
    "FIRELESS_WAR__DRIVE_FOLDER_ID": "folder-1",
    "FIRELESS_WAR__APPS_SCRIPT_URL": "https://script.example/exec",
    "FIRELESS_WAR__APPS_SCRIPT_SECRET": "script-secret",
    "FIRELESS_WAR__ALLOWED_ORIGINS": "https://beethoreven.github.io,http://localhost:8000",
}

GM = {"Authorization": "Bearer tok:gm@example.com"}
STRANGER = {"Authorization": "Bearer tok:someone@example.com"}


def fake_verify(token, client_id):
    from shared import google_auth

    assert client_id == ENV["FIRELESS_WAR__GOOGLE_CLIENT_ID"]
    if token == "down":
        raise google_auth.VerificationUnavailable()
    if not isinstance(token, str) or not token.startswith("tok:"):
        return None
    email = token[len("tok:"):]
    return {"sub": email, "email": email.lower()}


@pytest.fixture
def make_client(monkeypatch):
    """make_client(env) -> a test client for the whole service; fireless-war is at /fireless-war."""
    from apps.fireless_war import google_access
    from router.root import build
    from shared import google_auth

    monkeypatch.setattr(google_auth, "verify", fake_verify)
    google_access.service_account.reset()

    def make(env=None):
        return build(environ=ENV if env is None else env).test_client()

    return make


@pytest.fixture
def client(make_client):
    return make_client()
