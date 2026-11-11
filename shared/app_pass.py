"""An app's own sign-in pass: what the phone or page keeps instead of a Google ID token.

At sign-in the client trades one Google ID token for a pass and stores it; every later call carries
the pass. Restoring the sign-in after the app or page is reopened never asks Google for a silent
sign-in (that fails on real phones and shows an account picker — global CLAUDE.md, google-auth skill).

- **Stateless**: {"v", "sub", "email", "iat", "exp"} plus an HMAC-SHA256 signature. The server stores
  nothing about users and only checks the signature and expiry, so a forged or expired pass is
  rejected without touching any database (known-issue-check #33).
- Valid `days` days. A pass older than `renew_days` comes back with renew=True, and the app hands out a
  fresh one (the diary and mama-fufood do it in the daily check), so active users never reach expiry.
- No per-user revocation (nothing is stored). Rotating the app's SESSION_SECRET invalidates every pass.
- The signing key is derived from the app's secret with a purpose string, so a pass can't be replayed
  as any other token signed with the same secret (the admin session uses another purpose).

The format is byte-for-byte what mama-fufood (prefix "mff1.") and the diary ("amd1.") issued before
they shared this code: passes already on phones stay valid as long as the app keeps its prefix,
purpose and secret. tests/test_shared_app_pass.py pins both with known-answer vectors.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time

_DAY = 24 * 60 * 60
_MAX_LEN = 1024
_PREFIX_SHAPE = re.compile(r"^[a-z]{2,6}[0-9]\.$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AppPass:
    """One app's pass format. Create it once per app:

        app_pass = AppPass(prefix="amd1.", purpose=b"all-mo-diary:app-token:v1")
        token, expires_at = app_pass.issue(secret, sub, email)
        claims = app_pass.verify(secret, token)      # {"sub", "email", "renew"} or None
    """

    def __init__(self, *, prefix: str, purpose: bytes, days: int = 180, renew_days: int = 30):
        if not _PREFIX_SHAPE.match(prefix):
            raise ValueError("prefix must look like 'abc1.' (lowercase letters, a digit, a dot)")
        if not purpose:
            raise ValueError("purpose must be a non-empty byte string unique to this app and use")
        self.prefix = prefix
        self._purpose = purpose
        self.days = days
        self.renew_days = renew_days

    def _key(self, secret: str) -> bytes:
        return hmac.new(secret.encode("utf-8"), self._purpose, hashlib.sha256).digest()

    def looks_like(self, token: object) -> bool:
        """Carries this app's prefix (says nothing about whether it is genuine)."""
        return isinstance(token, str) and token.startswith(self.prefix)

    def issue(self, secret: str, sub: str, email: str, now: float | None = None) -> tuple[str, int]:
        """Returns (pass, expiry as Unix seconds)."""
        if not secret:
            raise ValueError("no secret: the app's SESSION_SECRET is not set")
        now = int(time.time() if now is None else now)
        exp = now + self.days * _DAY
        body = _b64(json.dumps({"v": 1, "sub": sub, "email": email, "iat": now, "exp": exp},
                               separators=(",", ":")).encode("utf-8"))
        sig = _b64(hmac.new(self._key(secret), body.encode("ascii"), hashlib.sha256).digest())
        return f"{self.prefix}{body}.{sig}", exp

    def verify(self, secret: str, token: object, now: float | None = None) -> dict | None:
        """{"sub", "email", "renew"} for a genuine, unexpired pass; otherwise None. No database, no network."""
        if not secret or not self.looks_like(token) or len(token) > _MAX_LEN:
            return None
        body, dot, sig = token[len(self.prefix):].partition(".")
        if not dot:
            return None
        expected = _b64(hmac.new(self._key(secret), body.encode("ascii", errors="replace"),
                                 hashlib.sha256).digest())
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
                "renew": now - claims["iat"] > self.renew_days * _DAY}
