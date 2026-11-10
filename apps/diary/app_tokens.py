"""The diary app's pass: what the phone sends instead of a Google ID token.

At sign-in the phone trades one Google ID token for this pass and keeps it; every later call
(daily check, feedback) carries the pass. Restoring the sign-in after restarting the app never
asks Google for a silent sign-in (that fails on real phones and shows an account picker).

- **Stateless**: {"v", "sub", "email", "iat", "exp"} plus an HMAC signature. The server stores
  nothing about users (privacy policy §2) and only checks the signature and expiry, so a forged or
  expired pass is rejected without touching any database (known-issue-check #33).
- Valid APP_TOKEN_DAYS days. A pass older than APP_TOKEN_RENEW_DAYS is reported as `renew`, and the
  daily check hands back a fresh one, so active users never reach expiry.
- No per-user revocation (nothing is stored). Rotating DIARY__SESSION_SECRET invalidates every pass.
- The key is derived from SESSION_SECRET with a purpose string, so a pass can't be used as any other
  kind of token signed with the same secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time

PREFIX = "amd1."
APP_TOKEN_DAYS = 180
APP_TOKEN_RENEW_DAYS = 30
_DAY = 24 * 60 * 60
_MAX_LEN = 1024
_PURPOSE = b"all-mo-diary:app-token:v1"


def _key(secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), _PURPOSE, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(secret: str, sub: str, email: str, now: float | None = None) -> tuple[str, int]:
    """Returns (pass, expiry as Unix seconds)."""
    now = int(time.time() if now is None else now)
    exp = now + APP_TOKEN_DAYS * _DAY
    body = _b64(json.dumps({"v": 1, "sub": sub, "email": email, "iat": now, "exp": exp},
                           separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_key(secret), body.encode("ascii"), hashlib.sha256).digest())
    return f"{PREFIX}{body}.{sig}", exp


def verify(secret: str, token: str | None, now: float | None = None) -> dict | None:
    """{"sub", "email", "renew"} for a genuine, unexpired pass; otherwise None. No database, no network."""
    if not secret or not token or not token.startswith(PREFIX) or len(token) > _MAX_LEN:
        return None
    body, dot, sig = token[len(PREFIX):].partition(".")
    if not dot:
        return None
    expected = _b64(hmac.new(_key(secret), body.encode("ascii", errors="replace"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        claims = json.loads(_unb64(body))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    now = time.time() if now is None else now
    if (not isinstance(claims, dict) or claims.get("v") != 1 or not isinstance(claims.get("sub"), str)
            or not isinstance(claims.get("email"), str) or not isinstance(claims.get("exp"), int)
            or not isinstance(claims.get("iat"), int) or claims["exp"] <= now):
        return None
    return {"sub": claims["sub"], "email": claims["email"],
            "renew": now - claims["iat"] > APP_TOKEN_RENEW_DAYS * _DAY}
