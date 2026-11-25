"""The admin console's sign-in: sessions, the admin list, and the require_admin gate.

    admin = AdminAuth(
        "all-mo-diary", database=db.database, purpose=b"all-mo-diary:admin-session:v1",
        secret=lambda: config.get().session_secret,
        first_admin=lambda: config.get().first_admin_email,
        idle_days=lambda: config.get().session_idle_days,
        cache_ttl=lambda: config.get().auth_cache_ttl_seconds,
    )

    @bp.get("/api/admin/things")
    @admin.require_admin                 # g.admin_email is set inside
    def things(): ...

Sessions (`admin.sessions`) — decoupled from Google's ID token:
- The token is opaque: 40 random hex characters plus a 24-character HMAC tag. A token whose tag doesn't
  match was never issued by us, and is rejected without touching the database.
- The table holds only the token's SHA-256 (column token_hash), so a copy of the database (a backup, a
  leaked dump, a read-only role) can't be used to sign in. Create the table with
  sessions_schema_sql(); upgrade_sessions_sql() moves a table that still stores plain tokens over to
  hashes, signing everyone out once.
- Sliding expiry: it expires only after `idle_days` days without any request, so nobody is signed out
  mid-task. last_seen_at is written at most once an hour.
- The tag key comes from the app's secret and `purpose`. Keep the purpose (and the sessions table name)
  the app used before moving here, and existing admin sessions survive the move.

The admin list (`admin.admins`) — a table of emails plus one fixed first admin from settings.

The gate (`admin.require_admin`) checks two things separately: the session is valid, and the email is an
admin *now*. The second isn't stored in the session; it is re-checked with a short cache, so removing an
admin takes effect within `cache_ttl` seconds (at once when done in the console, which calls forget_all).

★ The console's own lock is cosmetic; this is the gate. Every admin route must carry require_admin.
★ Each app has its own AdminAuth, so its cache can only answer for its own tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from functools import wraps
from typing import Callable

from flask import g, has_request_context, jsonify, request

from shared.database import Database
from shared.google_auth import extract_bearer_token

_RANDOM_HEX = 40
_TAG_HEX = 24
_TOKEN_SHAPE = re.compile(r"^[0-9a-f]{%d}$" % (_RANDOM_HEX + _TAG_HEX))
_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

NO_TOKEN = ({"error": "未提供登入憑證", "code": "no_token"}, 401)
BAD_TOKEN = ({"error": "登入憑證無效或已過期", "code": "bad_token"}, 401)
FORBIDDEN = ({"error": "這個帳號沒有權限", "code": "forbidden"}, 403)


def normalize_email(email: object) -> str | None:
    """Lower-cased, trimmed email, or None when it isn't one."""
    if not isinstance(email, str):
        return None
    email = email.strip().lower()
    return email if _EMAIL.match(email) and len(email) <= 254 else None


def _table(name: str) -> str:
    # Table names go into SQL text (they can't be parameters), so only fixed, plain names are allowed.
    if not _TABLE_NAME.match(name):
        raise ValueError(f"bad table name: {name!r}")
    return name


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def sessions_schema_sql(table: str = "admin_sessions") -> str:
    """CREATE statement for the app's own schema function (db.py)."""
    t = _table(table)
    return f"""
CREATE TABLE IF NOT EXISTS {t} (
    token_hash    TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def upgrade_sessions_sql(table: str = "admin_sessions") -> str:
    """For a table created before token_hash: its rows hold plain tokens, which can't be turned into the
    hashes of themselves usefully (that would keep them working from a leaked copy), so they are deleted
    and every admin signs in once. Runs inside the app's schema function, under its advisory lock, and
    does nothing once the column has been renamed."""
    t = _table(table)
    return f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = '{t}' AND column_name = 'token') THEN
        DELETE FROM {t};
        ALTER TABLE {t} RENAME COLUMN token TO token_hash;
    END IF;
END $$;
"""


class AdminSessions:
    """Rows: (token_hash TEXT PRIMARY KEY, email TEXT, created_at, last_seen_at TIMESTAMPTZ)."""

    def __init__(self, *, purpose: bytes, secret: Callable[[], str], idle_days: Callable[[], int],
                 table: str = "admin_sessions"):
        if not purpose:
            raise ValueError("purpose must be a non-empty byte string unique to this app")
        self._purpose = purpose
        self._secret = secret
        self._idle_days = idle_days
        self.table = _table(table)

    def enabled(self) -> bool:
        return bool(self._secret())

    def _tag(self, random_part: str) -> str:
        key = hmac.new(self._secret().encode("utf-8"), self._purpose, hashlib.sha256).digest()
        return hmac.new(key, random_part.encode("ascii"), hashlib.sha256).hexdigest()[:_TAG_HEX]

    def looks_valid(self, token: object) -> bool:
        """Right shape and right tag. **Touches no database.**"""
        if not self.enabled() or not isinstance(token, str) or not _TOKEN_SHAPE.match(token):
            return False
        return hmac.compare_digest(self._tag(token[:_RANDOM_HEX]), token[_RANDOM_HEX:])

    def create(self, cur, email: str) -> str:
        random_part = secrets.token_hex(_RANDOM_HEX // 2)
        token = random_part + self._tag(random_part)
        cur.execute(f"INSERT INTO {self.table} (token_hash, email) VALUES (%s, %s)",
                    (_hash(token), email.lower().strip()))
        return token

    def resolve(self, cur, token: object) -> str | None:
        """The email for a valid token; None if invalid or idle too long (an expired row is deleted)."""
        if not self.looks_valid(token):
            return None
        key = _hash(token)
        cur.execute(
            f"SELECT email, last_seen_at > now() - make_interval(days => %s) FROM {self.table}"
            " WHERE token_hash = %s", (self._idle_days(), key))
        row = cur.fetchone()
        if row is None:
            return None
        email, fresh = row
        if not fresh:
            cur.execute(f"DELETE FROM {self.table} WHERE token_hash = %s", (key,))
            return None
        cur.execute(
            f"UPDATE {self.table} SET last_seen_at = now()"
            f" WHERE token_hash = %s AND last_seen_at < now() - interval '1 hour'", (key,))
        return email

    def delete(self, cur, token: object) -> None:
        if self.looks_valid(token):
            cur.execute(f"DELETE FROM {self.table} WHERE token_hash = %s", (_hash(token),))

    def delete_for_email(self, cur, email: str) -> None:
        cur.execute(f"DELETE FROM {self.table} WHERE email = %s", (email.lower().strip(),))

    def purge_expired(self, cur) -> int:
        cur.execute(f"DELETE FROM {self.table} WHERE last_seen_at <= now() - make_interval(days => %s)",
                    (self._idle_days(),))
        return cur.rowcount


class AdminList:
    """Rows: (email TEXT PRIMARY KEY, note TEXT, created_at TIMESTAMPTZ, created_by TEXT)."""

    def __init__(self, *, first_admin: Callable[[], str], table: str = "admins"):
        self._first_admin = first_admin
        self.table = _table(table)

    def is_admin(self, cur, email: str | None) -> bool:
        if not email:
            return False
        first = self._first_admin()
        if first and email == first:
            return True
        cur.execute(f"SELECT 1 FROM {self.table} WHERE email = %s", (email,))
        return cur.fetchone() is not None

    def list_all(self, cur) -> list[dict]:
        cur.execute(f"SELECT email, note, created_at, created_by FROM {self.table} ORDER BY created_at")
        rows = [{"email": e, "note": n, "created_at": c.isoformat(), "created_by": b, "fixed": False}
                for e, n, c, b in cur.fetchall()]
        first = self._first_admin()
        if first and all(r["email"] != first for r in rows):
            rows.insert(0, {"email": first, "note": "第一位管理員（環境變數）",
                            "created_at": None, "created_by": "", "fixed": True})
        return rows

    def add(self, cur, email: str, note: str, by: str) -> None:
        cur.execute(
            f"INSERT INTO {self.table} (email, note, created_by) VALUES (%s, %s, %s)"
            " ON CONFLICT (email) DO UPDATE SET note = EXCLUDED.note",
            (email, note, by))

    def remove(self, cur, email: str) -> bool:
        cur.execute(f"DELETE FROM {self.table} WHERE email = %s", (email,))
        return cur.rowcount > 0


class AdminAuth:
    def __init__(self, app: str, *, database: Database, purpose: bytes, secret: Callable[[], str],
                 first_admin: Callable[[], str], idle_days: Callable[[], int], cache_ttl: Callable[[], int],
                 sessions_table: str = "admin_sessions", admins_table: str = "admins"):
        self.app = app
        self._database = database
        self._cache_ttl = cache_ttl
        self.sessions = AdminSessions(purpose=purpose, secret=secret, idle_days=idle_days, table=sessions_table)
        self.admins = AdminList(first_admin=first_admin, table=admins_table)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str]] = {}

    def enabled(self) -> bool:
        return self.sessions.enabled()

    # ── the short "is still an admin" cache ──

    def forget(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._cache.pop(token, None)

    def forget_all(self) -> None:
        with self._lock:
            self._cache.clear()

    def _peek(self, token: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(token)
            return hit[1] if hit is not None and hit[0] > now else None

    def _remember(self, token: str, email: str) -> None:
        ttl = self._cache_ttl()
        if ttl <= 0:
            return
        now = time.monotonic()
        with self._lock:
            for stale in [t for t, (expires, _e) in self._cache.items() if expires <= now]:
                del self._cache[stale]
            self._cache[token] = (now + ttl, email)

    # ── the gate ──

    def identify(self, token: str | None) -> tuple[str | None, tuple | None]:
        """(admin email, error): exactly one of them is None.

        ★ The connection is taken and given back here, so the route itself doesn't run while this
          request still holds a second connection (that could use up the app's slots).
        """
        if not token:
            return None, NO_TOKEN
        if not self.sessions.looks_valid(token):
            return None, BAD_TOKEN
        email = self._peek(token)
        if email is not None:
            return email, None
        with self._database.connection() as conn:
            with conn.cursor() as cur:
                email = self.sessions.resolve(cur, token)
                ok = self.admins.is_admin(cur, email) if email else False
        if email is None:
            return None, BAD_TOKEN
        if not ok:
            # Signed in but not an admin: the audit log (shared.audit_log) names who was turned away.
            if has_request_context():
                g.audit_actor = email
            return None, FORBIDDEN
        self._remember(token, email)
        return email, None

    def require_admin(self, fn):
        """Signed in and an admin now. Inside the route, g.admin_email is that admin's email."""

        @wraps(fn)
        def wrapper(*args, **kwargs):
            email, error = self.identify(extract_bearer_token(request))
            if error:
                body, status = error
                return jsonify(body), status
            g.admin_email = email
            return fn(*args, **kwargs)

        return wrapper
