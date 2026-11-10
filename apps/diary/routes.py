"""The diary app's API (all under /diary). Errors are {"error": message for people, "code": code}."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from . import app_tokens, google_auth

bp = Blueprint("diary_api", __name__)


def _settings():
    return current_app.extensions["diary.settings"]


def _error(status: int, code: str, message: str):
    return jsonify({"error": message, "code": code}), status


def authenticate():
    """For endpoints that need a signed-in user: (claims, None) or (None, error response).

    Only our own pass is accepted — never a raw Google ID token. There are no older app versions
    sending ID tokens, so unlike mama-fufood there is no compatibility path to carry.
    """
    claims = app_tokens.verify(_settings().text("SESSION_SECRET"), google_auth.extract_bearer_token(request))
    if claims is None:
        return None, _error(401, "invalid_token", "登入已過期，請重新登入")
    return claims, None


@bp.post("/api/v1/session")
def session():
    """Called once when the user signs in on the phone: verify the Google ID token, issue a pass.

    Nothing is stored: the pass carries the account id, email and expiry, signed against forgery.
    """
    settings = _settings()
    secret = settings.text("SESSION_SECRET")
    client_id = settings.text("GOOGLE_CLIENT_ID")
    if not secret or not client_id:
        return _error(503, "not_configured", "後端還沒設定登入")
    claims = google_auth.verify(google_auth.extract_bearer_token(request), client_id)
    if claims is None:
        return _error(401, "invalid_token", "Google 登入憑證無效或已過期，請重新登入")
    token, exp = app_tokens.issue(secret, claims["sub"], claims["email"])
    return jsonify({"token": token, "expires_at": exp})
