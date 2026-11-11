"""Verifying a Google ID token.

An ID token only proves "this is that Google account, right now" (it expires in about an hour). Apps
that keep users signed in trade it once for their own pass (shared.app_pass) and never use it as an
ongoing credential. An app that deliberately keeps no sign-in verifies one on every request instead.

Defences, all before any network call:
1. A string that isn't JWT-shaped, or is longer than MAX_ID_TOKEN_LENGTH, is rejected without
   fetching Google's keys.
2. A token without `kid` in its header is rejected (google-auth would otherwise try every key: pure CPU).
3. Google's public keys are cached according to Google's Cache-Control header.
4. Fetching the keys gives up after FETCH_TIMEOUT_SECONDS. google-auth's own default is 120 s, which in
   a process shared by several apps would hold a thread for two minutes per stuck sign-in.
5. 10 seconds of clock skew allowed; email_verified is required.

"Invalid" and "can't check right now" are different answers: a token that fails
verification returns None (answer 401, sign the user out), but Google's keys being unreachable raises
VerificationUnavailable (shared.flask_app answers 503, and the client keeps its sign-in). Only a
transport error counts as unavailable; anything else, including a wrong issuer, is an invalid token.

The cached HTTP session is process-wide on purpose: Google's keys are the same for every app, so
sharing the cache is correct, and it holds nothing about any app or user.

Tests replace `verify`, so callers write `google_auth.verify(...)`, never `from ... import verify`.
"""

from __future__ import annotations

import base64
import json
import logging
import re

import cachecontrol
import requests as requests_lib
from google.auth import exceptions as google_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

log = logging.getLogger(__name__)

CLOCK_SKEW_SECONDS = 10
FETCH_TIMEOUT_SECONDS = 10
MAX_ID_TOKEN_LENGTH = 8192
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+$")


class VerificationUnavailable(Exception):
    """Google's signing keys couldn't be fetched. Not the user's fault: answer 503, keep them signed in."""


class _BoundedRequest(google_requests.Request):
    """google-auth's transport with a short timeout on every call (its default is 120 s)."""

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        return super().__call__(url, method=method, body=body, headers=headers,
                                timeout=FETCH_TIMEOUT_SECONDS, **kwargs)


_google_request = _BoundedRequest(session=cachecontrol.CacheControl(requests_lib.session()))


def _has_kid(token: str) -> bool:
    head = token.split(".", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
        return bool(json.loads(raw).get("kid"))
    except Exception:  # noqa: BLE001 — undecodable means wrong shape, same as no kid
        return False


def extract_bearer_token(request) -> str | None:
    """The token from `Authorization: Bearer <token>`, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    return token or None


def verify(token: object, client_id: str) -> dict | None:
    """{"sub": Google account id, "email": lower-case email} for a valid token, else None.

    Raises VerificationUnavailable when Google's keys can't be fetched right now.
    """
    if not isinstance(token, str) or not token or not client_id:
        return None
    if len(token) > MAX_ID_TOKEN_LENGTH or not _JWT_SHAPE.match(token) or not _has_kid(token):
        return None
    try:
        payload = google_id_token.verify_oauth2_token(
            token, _google_request, client_id, clock_skew_in_seconds=CLOCK_SKEW_SECONDS)
    except google_exceptions.TransportError as exc:
        log.warning("Google signing keys unavailable: %s", type(exc).__name__)
        raise VerificationUnavailable() from exc
    except Exception as exc:  # noqa: BLE001 — bad signature, expired, wrong audience or issuer…
        # Only the exception type is logged, never the token.
        log.warning("Google ID token rejected: %s", type(exc).__name__)
        return None
    if not payload.get("email_verified"):
        return None
    sub, email = payload.get("sub"), payload.get("email")
    if not sub or not email:
        return None
    return {"sub": str(sub), "email": email.lower().strip()}
