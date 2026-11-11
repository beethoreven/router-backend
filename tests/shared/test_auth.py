"""shared.app_pass, shared.google_auth and shared.admin_auth's token checks (no database needed).

The known-answer vectors were produced by the implementations these apps used before this module.
If one of these fails, passes already on users' phones, or admin sessions already in a database,
stop working.
"""

from __future__ import annotations

import pytest
from google.auth import exceptions as google_exceptions

from shared import google_auth
from shared.admin_auth import AdminSessions, normalize_email
from shared.app_pass import AppPass

DIARY = AppPass(prefix="amd1.", purpose=b"all-mo-diary:app-token:v1")
FUFOOD = AppPass(prefix="mff1.", purpose=b"mama-fufood:app-token:v1")
VECTOR_NOW = 1_800_000_000
DIARY_PASS = ("amd1.eyJ2IjoxLCJzdWIiOiJzdWItMTIzIiwiZW1haWwiOiJtZUBleGFtcGxlLmNvbSIsImlhdCI6MTgwMDAwMDAwMCwiZXhw"
              "IjoxODE1NTUyMDAwfQ.WZNm9GfbaZ4L8bObCd8nb0Lp569E1PVcWKLn_JfLepE")
FUFOOD_PASS = ("mff1.eyJ2IjoxLCJzdWIiOiJzdWItMTIzIiwiZW1haWwiOiJtZUBleGFtcGxlLmNvbSIsImlhdCI6MTgwMDAwMDAwMCwiZXhw"
               "IjoxODE1NTUyMDAwfQ.HHLTmAjktJkBeePo3To8p80FpiS7NA8sny1hkwNfCZQ")
RANDOM_PART = "0123456789abcdef0123456789abcdef01234567"


# ── app pass ──

@pytest.mark.parametrize("fmt, expected", [(DIARY, DIARY_PASS), (FUFOOD, FUFOOD_PASS)])
def test_pass_is_byte_for_byte_what_the_apps_issued_before(fmt, expected):
    token, exp = fmt.issue("vector-secret", "sub-123", "me@example.com", now=VECTOR_NOW)
    assert token == expected and exp == 1_815_552_000
    assert fmt.verify("vector-secret", expected, now=VECTOR_NOW + 60) == {
        "sub": "sub-123", "email": "me@example.com", "renew": False}


def test_one_apps_pass_is_not_anothers_even_with_the_same_secret():
    assert FUFOOD.verify("vector-secret", DIARY_PASS, now=VECTOR_NOW) is None
    swapped = "mff1." + DIARY_PASS[len("amd1."):]
    assert FUFOOD.verify("vector-secret", swapped, now=VECTOR_NOW) is None  # purpose differs


def test_pass_expiry_renewal_and_tampering():
    token, exp = DIARY.issue("k", "1", "a@b.c", now=1_000_000)
    assert DIARY.verify("k", token, now=1_000_000 + 10)["renew"] is False
    assert DIARY.verify("k", token, now=1_000_000 + 31 * 86400)["renew"] is True
    assert DIARY.verify("k", token, now=exp) is None  # expired
    assert DIARY.verify("other-key", token, now=1_000_010) is None  # wrong secret
    assert DIARY.verify("", token, now=1_000_010) is None  # no secret configured
    body, sig = token[len("amd1."):].split(".")
    assert DIARY.verify("k", "amd1." + body[:-2] + "AA." + sig, now=1_000_010) is None
    assert DIARY.verify("k", "amd1." + body, now=1_000_010) is None  # no signature
    assert DIARY.verify("k", None) is None and DIARY.verify("k", 12345) is None
    assert DIARY.verify("k", "eyJhbGciOiJSUzI1NiIsImtpZCI6IngifQ.eyJ9.sig") is None  # a raw Google ID token


def test_pass_needs_a_secret_and_a_sane_format():
    with pytest.raises(ValueError):
        DIARY.issue("", "1", "a@b.c")
    with pytest.raises(ValueError):
        AppPass(prefix="amd1", purpose=b"x")
    with pytest.raises(ValueError):
        AppPass(prefix="amd1.", purpose=b"")


# ── admin sessions ──

@pytest.mark.parametrize("purpose, tag", [
    (b"all-mo-diary:admin-session:v1", "603c1e27b36befd39d7adc13"),
    (b"mama-fufood:admin-session:v1", "a75a656c1e9ae892b0eeceed"),
])
def test_admin_session_tags_match_what_the_apps_issued_before(purpose, tag):
    sessions = AdminSessions(purpose=purpose, secret=lambda: "vector-secret", idle_days=lambda: 90)
    assert sessions.looks_valid(RANDOM_PART + tag)
    assert not sessions.looks_valid(RANDOM_PART + "0" * 24)
    assert not sessions.looks_valid((RANDOM_PART + tag).upper())


def test_admin_sessions_off_without_a_secret_and_table_names_are_checked():
    off = AdminSessions(purpose=b"x", secret=lambda: "", idle_days=lambda: 90)
    assert not off.enabled() and not off.looks_valid(RANDOM_PART + "0" * 24)
    with pytest.raises(ValueError):
        AdminSessions(purpose=b"x", secret=lambda: "s", idle_days=lambda: 90, table="sessions; DROP TABLE x")


def test_normalize_email():
    assert normalize_email("  Me@Example.COM ") == "me@example.com"
    assert normalize_email("not-an-email") is None and normalize_email(None) is None


# ── Google ID token ──

GOOD_SHAPE = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiIxIn0.c2ln"  # header has kid


def test_shape_checks_reject_without_contacting_google(monkeypatch):
    """Garbage never reaches the network call."""
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_oauth2_token", lambda *a, **k: pytest.fail("should not verify"))
    assert google_auth.verify("not-a-jwt", "cid") is None
    assert google_auth.verify("a.b.c", "cid") is None  # JWT-shaped but no kid in the header
    assert google_auth.verify("x" * 9000, "cid") is None
    assert google_auth.verify({"not": "a string"}, "cid") is None
    assert google_auth.verify(GOOD_SHAPE, "") is None  # no client id configured


def test_unreachable_google_raises_but_a_bad_token_is_just_none(monkeypatch):
    from google.oauth2 import id_token

    def transport_error(*a, **k):
        raise google_exceptions.TransportError("connection reset")

    monkeypatch.setattr(id_token, "verify_oauth2_token", transport_error)
    with pytest.raises(google_auth.VerificationUnavailable):
        google_auth.verify(GOOD_SHAPE, "cid")

    # A wrong issuer is GoogleAuthError (not ValueError) — it must still count as invalid, not "unavailable".
    for exc in (ValueError("Token expired"), google_exceptions.GoogleAuthError("Wrong issuer")):
        def raises(*a, _exc=exc, **k):
            raise _exc
        monkeypatch.setattr(id_token, "verify_oauth2_token", raises)
        assert google_auth.verify(GOOD_SHAPE, "cid") is None


def test_unverified_email_is_rejected_and_email_is_lowercased(monkeypatch):
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_oauth2_token",
                        lambda *a, **k: {"sub": "1", "email": "A@B.C", "email_verified": False})
    assert google_auth.verify(GOOD_SHAPE, "cid") is None
    monkeypatch.setattr(id_token, "verify_oauth2_token",
                        lambda *a, **k: {"sub": 1, "email": " A@B.C ", "email_verified": True})
    assert google_auth.verify(GOOD_SHAPE, "cid") == {"sub": "1", "email": "a@b.c"}


def test_fetching_googles_keys_has_a_short_timeout(monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise google_exceptions.TransportError("stop here")

    monkeypatch.setattr(google_auth._google_request.session, "request", fake_request)
    with pytest.raises(google_exceptions.TransportError):
        google_auth._google_request("https://www.googleapis.com/oauth2/v1/certs")
    assert seen["timeout"] == google_auth.FETCH_TIMEOUT_SECONDS
