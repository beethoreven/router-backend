"""Sign-in checks. fireless-war deliberately keeps no sign-in of its own: every request carries a
Google ID token and is verified again here.

- require_gm: a valid Google sign-in AND the email is in FIRELESS_WAR__PERMITTED_USERS.
- require_login: any valid Google sign-in (the public demo).

401 means "your sign-in is invalid" (the page signs out); 503 means "Google can't be reached right now"
(the page keeps the sign-in and retries). Both are caught here, not left to shared.flask_app, so each
route keeps its own wording (the demo is English) and the rate limits don't count them.
"""

from __future__ import annotations

from functools import wraps

from flask import g, jsonify, request

from shared import google_auth

from . import config


def is_gm(email: str) -> bool:
    return bool(email) and email.lower() in config.get().permitted_users


def verified_email() -> str | None:
    """The request's verified Google email, or None. Raises google_auth.VerificationUnavailable."""
    token = google_auth.extract_bearer_token(request)
    user = google_auth.verify(token, config.get().google_client_id)
    return user["email"] if user else None


def _gate(*, gm_only: bool, unavailable: str, rejected: str):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            try:
                email = verified_email()
            except google_auth.VerificationUnavailable:
                return jsonify({"error": unavailable, "code": "auth_unavailable"}), 503
            if email is None or (gm_only and not is_gm(email)):
                return jsonify({"error": rejected, "code": "not_authorized"}), 401
            g.email = email
            return view(*args, **kwargs)

        return wrapper

    return decorator


require_gm = _gate(gm_only=True, unavailable="目前無法驗證登入狀態,請稍後再試",
                   rejected="未登入或此帳號未獲授權")
require_login = _gate(gm_only=False, unavailable="Could not verify sign-in right now, please try again later",
                      rejected="Not signed in or sign-in credentials are invalid")
