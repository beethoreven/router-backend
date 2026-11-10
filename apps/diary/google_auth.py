"""Verifying a Google ID token — done exactly once per sign-in, at POST /diary/api/v1/session.

The ID token only proves "this is that Google account, right now" (it expires in about an hour).
It is exchanged for our own app pass (app_tokens.py) and never used as an ongoing credential.

Defences, all before any network call (same as mama-fufood / group-judge):
1. A string that isn't JWT-shaped is rejected without fetching Google's keys.
2. A token without `kid` in its header is rejected (google-auth would otherwise try every key: pure CPU).
3. Google's public keys are cached according to Google's Cache-Control header.
4. 10 seconds of clock skew allowed.
5. email_verified is required.

The cached HTTP session below is process-wide on purpose: Google's signing keys are the same for
every app in this process, so sharing the cache is correct (it holds no per-app data).
"""

from __future__ import annotations

import base64
import json
import logging
import re

import cachecontrol
import requests as requests_lib
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

log = logging.getLogger(__name__)

_CLOCK_SKEW_SECONDS = 10
_google_request = google_requests.Request(session=cachecontrol.CacheControl(requests_lib.session()))
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+$")
_MAX_ID_TOKEN_LENGTH = 8192


def _has_kid(token: str) -> bool:
    head = token.split(".", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
        return bool(json.loads(raw).get("kid"))
    except Exception:  # noqa: BLE001 — undecodable means wrong shape, same as no kid
        return False


def extract_bearer_token(request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    return token or None


def verify(token: str | None, client_id: str) -> dict | None:
    """Returns {"sub": Google account id, "email": lower-case email} when valid, else None.

    Tests replace this function, so callers write `google_auth.verify(...)`.
    """
    if not token or not client_id:
        return None
    if len(token) > _MAX_ID_TOKEN_LENGTH or not _JWT_SHAPE.match(token) or not _has_kid(token):
        return None
    try:
        payload = google_id_token.verify_oauth2_token(
            token, _google_request, client_id, clock_skew_in_seconds=_CLOCK_SKEW_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fetching keys is real network I/O and raises other types
        # Only the exception type is logged, never the token.
        log.warning("Google ID token rejected: %s", type(exc).__name__)
        return None
    if not payload.get("email_verified"):
        return None
    sub, email = payload.get("sub"), payload.get("email")
    if not sub or not email:
        return None
    return {"sub": str(sub), "email": email.lower().strip()}
