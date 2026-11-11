"""全心日記's Flask app, mounted at /diary.

The phone keeps diaries on the phone and in the user's own Google Drive; this backend never stores
diary content. It serves:
- routes.py: sign-in exchange, daily check (minimum version, announcement, font list), feedback.
- admin_routes.py: the admin console (fonts, app config, feedback, admins).

Settings: DIARY__* environment variables (config.py).

★ Nothing here starts a thread or connects anywhere when the app is built: the cache loader starts on
  the first request, inside the worker (mama-fufood found work started at import never ran on Render).
★ gunicorn's access log is turned off by router/root.py (it records IPs; the privacy policy says the
  app doesn't keep them).
"""

from __future__ import annotations

import logging

import psycopg
from flask import Flask, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from router.settings import AppSettings

from . import admin_routes, cache, config, db, routes

log = logging.getLogger(__name__)

# The largest request is a font upload (≤ 120 MB decompressed, sent gzip-compressed, so smaller).
# Every JSON endpoint enforces its own much smaller limit (payload.read_raw).
MAX_CONTENT_LENGTH = 130 * 1024 * 1024


def create_app(settings: AppSettings) -> Flask:
    cfg = config.DiaryConfig.from_settings(settings)
    config.set_current(cfg)
    if not cfg.database_url:
        log.warning("DIARY__DATABASE_URL is not set: the daily check, feedback and admin console won't work")

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
    app.json.ensure_ascii = False

    if cfg.allowed_origins:
        # Only the admin console (GitHub Pages) is cross-origin; the phone's requests aren't from a browser.
        CORS(app, resources={r"/api/admin/*": {"origins": list(cfg.allowed_origins)}},
             allow_headers=["Authorization", "Content-Type"], methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
             max_age=600)

    app.register_blueprint(routes.bp)
    app.register_blueprint(admin_routes.bp)

    if cfg.start_cache_loader:
        @app.before_request
        def _start_cache_loader():
            # Only the first call starts the thread; later requests just read a flag.
            cache.start_background_loader()

    @app.errorhandler(db.DatabaseNotConfigured)
    def _db_not_configured(_exc):
        return jsonify({"error": "後端還沒設定資料庫", "code": "db_not_configured"}), 503

    @app.errorhandler(db.DatabaseBusy)
    def _db_busy(_exc):
        return jsonify({"error": "資料庫忙碌中，請稍後再試", "code": "db_busy"}), 503

    @app.errorhandler(psycopg.OperationalError)
    def _db_unavailable(_exc):
        return jsonify({"error": "資料庫暫時連不上，請稍後再試", "code": "db_unavailable"}), 503

    @app.errorhandler(HTTPException)
    def _http_error(exc):
        messages = {404: "找不到這個網址", 405: "不支援這個操作", 413: "內容太大"}
        return jsonify({"error": messages.get(exc.code, exc.name), "code": f"http_{exc.code}"}), exc.code

    @app.errorhandler(Exception)
    def _unexpected(exc):
        # Only the type and stack; never the request body (it may hold a user's text).
        log.exception("unexpected error: %s", type(exc).__name__)
        return jsonify({"error": "伺服器發生錯誤", "code": "server_error"}), 500

    return app
