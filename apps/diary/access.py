"""後台路由的那道門:登入了,而且現在是管理員(做法沿用 group-judge)。

★ 前端的鎖是化妝品,真正的門在這裡:每一支後台路由都必須掛 require_admin。

兩件事分開查:session 有效(auth_utils/sessions.py),以及現在是管理員(admins 表,
或環境變數 FIRST_ADMIN_EMAIL)。第二件事不寫進 session,每次都確認(帶短快取),
拿掉某人的管理員身分後,最慢 AUTH_CACHE_TTL_SECONDS 秒就失效;在後台改的話立刻失效。
"""

from __future__ import annotations

import threading
import time
from functools import wraps

from flask import g, jsonify, request

from . import config
from . import sessions
from .google_auth import extract_bearer_token
from . import admins
from . import db

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}

NO_TOKEN = ({"error": "未提供登入憑證", "code": "no_token"}, 401)
BAD_TOKEN = ({"error": "登入憑證無效或已過期", "code": "bad_token"}, 401)
FORBIDDEN = ({"error": "這個帳號沒有權限", "code": "forbidden"}, 403)


def forget(token: str | None) -> None:
    if token:
        with _lock:
            _cache.pop(token, None)


def forget_all() -> None:
    with _lock:
        _cache.clear()


def _peek(token: str) -> str | None:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(token)
        return hit[1] if hit is not None and hit[0] > now else None


def _remember(token: str, email: str) -> None:
    if config.get().auth_cache_ttl_seconds <= 0:
        return
    now = time.monotonic()
    with _lock:
        for stale in [t for t, (expires, _e) in _cache.items() if expires <= now]:
            del _cache[stale]
        _cache[token] = (now + config.get().auth_cache_ttl_seconds, email)


def identify(token: str | None) -> tuple[str | None, tuple | None]:
    """回傳(管理員 Email, 錯誤),恰有一個是 None。

    ★ 連線的取得與歸還都在這裡完成,路由執行時這條連線已經關了(避免一個請求同時
      握兩條連線,把名額卡死)。
    """
    if not token:
        return None, NO_TOKEN
    if not sessions.looks_valid(token):
        return None, BAD_TOKEN
    email = _peek(token)
    if email is not None:
        return email, None
    with db.connection() as conn:
        with conn.cursor() as cur:
            email = sessions.resolve(cur, token)
            ok = admins.is_admin(cur, email) if email else False
    if email is None:
        return None, BAD_TOKEN
    if not ok:
        return None, FORBIDDEN
    _remember(token, email)
    return email, None


def require_admin(fn):
    """需要登入,而且是管理員。通過的話 g.admin_email 是那個管理員的 Email。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        email, error = identify(extract_bearer_token(request))
        if error:
            body, status = error
            return jsonify(body), status
        g.admin_email = email
        return fn(*args, **kwargs)

    return wrapper
