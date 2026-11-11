"""管理後台的 session,跟 Google ID Token 完全脫鉤(做法沿用 group-judge)。

- token 是不透明字串:前 40 個字是亂數,後 24 個字是 HMAC 簽章。簽章對不上就
  不是我們發的,連資料庫都不碰。
- 「滑動式」有效期:只要還有在用就不會過期,連續 SESSION_IDLE_DAYS 天沒有任何
  請求才過期,所以不會在使用中途被登出。
- 撤銷不靠這一層:auth_utils/access.py 每次都會確認「這個 Email 現在還是不是管理員」。
- 簽章金鑰從 SESSION_SECRET 衍生。換掉它,所有人要重新登入一次。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from . import config

_RANDOM_HEX = 40
_TAG_HEX = 24
_TOKEN_SHAPE = re.compile(r"^[0-9a-f]{%d}$" % (_RANDOM_HEX + _TAG_HEX))


def enabled() -> bool:
    return bool(config.get().session_secret)


def _key() -> bytes:
    return hmac.new(config.get().session_secret.encode("utf-8"), b"all-mo-diary:admin-session:v1",
                    hashlib.sha256).digest()


def _tag(random_part: str) -> str:
    return hmac.new(_key(), random_part.encode("ascii"), hashlib.sha256).hexdigest()[:_TAG_HEX]


def looks_valid(token: str | None) -> bool:
    """形狀對、簽章也對。**不碰資料庫**。"""
    if not enabled() or not token or not _TOKEN_SHAPE.match(token):
        return False
    return hmac.compare_digest(_tag(token[:_RANDOM_HEX]), token[_RANDOM_HEX:])


def create(cur, email: str) -> str:
    random_part = secrets.token_hex(_RANDOM_HEX // 2)
    token = random_part + _tag(random_part)
    cur.execute("INSERT INTO admin_sessions (token, email) VALUES (%s, %s)", (token, email.lower().strip()))
    return token


def resolve(cur, token: str | None) -> str | None:
    """token 有效就回傳 Email;無效或閒置過久回 None(過期的順便刪掉)。

    last_seen_at 一小時頂多寫一次,條件放在 UPDATE 的 WHERE 裡。
    """
    if not looks_valid(token):
        return None
    cur.execute(
        "SELECT email, last_seen_at > now() - make_interval(days => %s) FROM admin_sessions WHERE token = %s",
        (config.get().session_idle_days, token),
    )
    row = cur.fetchone()
    if row is None:
        return None
    email, fresh = row
    if not fresh:
        cur.execute("DELETE FROM admin_sessions WHERE token = %s", (token,))
        return None
    cur.execute(
        "UPDATE admin_sessions SET last_seen_at = now() WHERE token = %s AND last_seen_at < now() - interval '1 hour'",
        (token,),
    )
    return email


def delete(cur, token: str | None) -> None:
    if looks_valid(token):
        cur.execute("DELETE FROM admin_sessions WHERE token = %s", (token,))


def delete_for_email(cur, email: str) -> None:
    cur.execute("DELETE FROM admin_sessions WHERE email = %s", (email.lower().strip(),))


def purge_expired(cur) -> int:
    cur.execute("DELETE FROM admin_sessions WHERE last_seen_at <= now() - make_interval(days => %s)",
                (config.get().session_idle_days,))
    return cur.rowcount
