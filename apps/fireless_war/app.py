"""願浮沉2:無火戰爭's Flask app, mounted at /fireless-war.

The web page (GitHub Pages) reads each game's Google Sheet through this API; it stores nothing itself.
Settings: FIRELESS_WAR__* (config.py). Endpoints: routes.py. Sign-in: auth.py (a Google ID token on
every request; this app deliberately keeps no sign-in of its own).

★ A missing required setting doesn't stop the app from loading (that would mark the whole router not
  ready); every request answers 503 not_configured instead, and /fireless-war/ready names what's missing.
★ create_app connects nowhere: Google credentials are built on the first request that needs them.
"""

from __future__ import annotations

import logging

from flask import request
from flask_limiter import Limiter

from router.settings import AppSettings
from shared.flask_app import allow_cors, new_app

from . import config, routes

log = logging.getLogger(__name__)

# Every endpoint takes only query parameters.
MAX_CONTENT_LENGTH = 16 * 1024


def create_app(settings: AppSettings):
    cfg = config.FirelessWarConfig.from_settings(settings)
    config.set_current(cfg)
    missing = cfg.missing()
    if missing:
        log.warning("fireless-war is missing settings, every request will answer 503: %s", ", ".join(missing))

    app = new_app(__name__, max_content_length=MAX_CONTENT_LENGTH)
    allow_cors(app, cfg.allowed_origins, methods=("GET", "POST"))
    # One shared counter per route for the whole site (see routes.py); memory storage, single worker.
    # ★ An exception to "no threads in create_app", acceptable because limits' MemoryStorage starts only
    #   a 0.01 s threading.Timer that expires in-memory counters (no network, no locks beyond its own),
    #   and create_app runs lazily inside the worker, not in gunicorn's master process. Keep it here,
    #   bound to this app, never at module level.
    limiter = Limiter(key_func=lambda: "global", app=app, storage_uri="memory://")

    @app.before_request
    def _require_settings():
        if missing and request.endpoint != "ready" and request.method != "OPTIONS":
            return {"error": "後端設定不完整,請聯絡管理員", "code": "not_configured"}, 503

    routes.register(app, limiter)
    return app
