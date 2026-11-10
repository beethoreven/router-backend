"""全心日記's Flask app.

The phone app keeps diaries on the phone and in the user's own Google Drive; this backend never
stores diary content. Its endpoints (login session, daily check with the font list, feedback)
arrive with the app features that need them; see all-mo-diary/docs/SPEC_DRAFT.md.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from router.settings import AppSettings

log = logging.getLogger(__name__)

# The largest request will be a feedback report with its diagnostic log (≤ 1 MiB compressed).
MAX_CONTENT_LENGTH = 2 * 1024 * 1024


def create_app(settings: AppSettings) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
    app.json.ensure_ascii = False
    app.extensions["diary.settings"] = settings

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
