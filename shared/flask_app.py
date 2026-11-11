"""Building an app's Flask object the same way everywhere.

    app = new_app(__name__, max_content_length=9 * 1024 * 1024)
    allow_cors(app, cfg.allowed_origins, paths=r"/api/admin/*")

new_app gives every app:
- MAX_CONTENT_LENGTH (each endpoint should still read with its own smaller limit: shared.payload);
- JSON that keeps Chinese readable (ensure_ascii off);
- the same JSON error shape, {"error": message for people, "code": code}, for:
    PayloadError → its status · VerificationUnavailable → 503 auth_unavailable ·
    DatabaseNotConfigured / DatabaseBusy / psycopg.OperationalError → 503 ·
    HTTP errors (404, 405, 413…) → their status · anything else → 500 server_error.
- ★ The 500 handler logs the exception type and stack only, never the request body (it may hold what
  a user wrote).

Not here on purpose: /health and /ready (the router answers those for the whole service; an app's
own readiness belongs in its /ready only if it touches no database), logging setup and the gunicorn
access log (router/root.py turns that off: it records IPs).
"""

from __future__ import annotations

import logging

import psycopg
from flask import Flask, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from shared.database import DatabaseBusy, DatabaseNotConfigured
from shared.google_auth import VerificationUnavailable
from shared.payload import PayloadError

log = logging.getLogger(__name__)

_HTTP_MESSAGES = {400: "請求格式不對", 401: "需要登入", 403: "沒有權限", 404: "找不到這個網址",
                  405: "不支援這個操作", 413: "內容太大", 429: "太多請求，請稍後再試"}


def _error(status: int, code: str, message: str):
    return jsonify({"error": message, "code": code}), status


def new_app(import_name: str, *, max_content_length: int) -> Flask:
    app = Flask(import_name)
    app.config["MAX_CONTENT_LENGTH"] = max_content_length
    app.json.ensure_ascii = False

    @app.errorhandler(PayloadError)
    def _payload_error(exc):
        return _error(exc.status, exc.code, str(exc))

    @app.errorhandler(VerificationUnavailable)
    def _auth_unavailable(_exc):
        # Not "your sign-in is invalid": the client should keep the user signed in and retry later.
        return _error(503, "auth_unavailable", "暫時無法向 Google 確認登入，請稍後再試")

    @app.errorhandler(DatabaseNotConfigured)
    def _db_not_configured(_exc):
        return _error(503, "db_not_configured", "後端還沒設定資料庫")

    @app.errorhandler(DatabaseBusy)
    def _db_busy(_exc):
        return _error(503, "db_busy", "資料庫忙碌中，請稍後再試")

    @app.errorhandler(psycopg.OperationalError)
    def _db_unavailable(_exc):
        return _error(503, "db_unavailable", "資料庫暫時連不上，請稍後再試")

    @app.errorhandler(HTTPException)
    def _http_error(exc):
        return _error(exc.code, f"http_{exc.code}", _HTTP_MESSAGES.get(exc.code, exc.name))

    @app.errorhandler(Exception)
    def _unexpected(exc):
        log.exception("[%s] unexpected error: %s", import_name, type(exc).__name__)
        return _error(500, "server_error", "伺服器發生錯誤")

    return app


def allow_cors(app: Flask, origins: tuple[str, ...] | list[str], *, paths: str = r"/*",
               methods: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")) -> None:
    """Let these web origins call `paths` from a browser. Nothing else gets CORS headers.

    `origins` must be exact origins (AppSettings.origins() checks the shape) — no "*", no regex:
    a regex like r"http://localhost:\\d+" matches by prefix unless anchored.
    Calls from a phone app aren't from a browser and don't need CORS at all.
    """
    origins = list(origins)
    if not origins:
        return
    for origin in origins:
        if origin == "*" or any(ch in origin for ch in "*\\^$()[]"):
            raise ValueError(f"CORS origins must be exact origins, not patterns: {origin!r}")
    CORS(app, resources={paths: {"origins": origins}}, allow_headers=["Authorization", "Content-Type"],
         methods=list(methods), max_age=600)
