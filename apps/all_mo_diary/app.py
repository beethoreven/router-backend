"""全心日記's Flask app, mounted at /all-mo-diary.

The phone keeps diaries on the phone and in the user's own Google Drive; this backend never stores
diary content. It serves:
- routes.py: sign-in exchange, daily check (minimum version, announcement, font list), feedback.
- admin_routes.py: the admin console (fonts, app config, feedback, admins).

Settings: ALL_MO_DIARY__* environment variables (config.py). Sign-in, database, error handling and limits come
from shared/ (auth.py and db.py say which pieces).

★ Nothing here starts a thread or connects anywhere when the app is built: the cache loader starts on
  the first request, inside the worker (work started at import has been seen never to run on Render).
"""

from __future__ import annotations

import logging

from router.settings import AppSettings
from shared.flask_app import allow_cors, new_app

from . import admin_routes, cache, config, routes

log = logging.getLogger(__name__)

# The largest request is a font upload (≤ 120 MB decompressed, sent gzip-compressed, so smaller).
# Every JSON endpoint enforces its own much smaller limit (shared.payload.read_json).
MAX_CONTENT_LENGTH = 130 * 1024 * 1024


def create_app(settings: AppSettings):
    cfg = config.DiaryConfig.from_settings(settings)
    config.set_current(cfg)
    if not cfg.database_url:
        log.warning("ALL_MO_DIARY__DATABASE_URL is not set: the daily check, feedback and admin console won't work")

    app = new_app(__name__, max_content_length=MAX_CONTENT_LENGTH)
    # Only the admin console (GitHub Pages) is cross-origin; the phone's requests aren't from a browser.
    allow_cors(app, cfg.allowed_origins, paths=r"/api/admin/*")

    app.register_blueprint(routes.bp)
    app.register_blueprint(admin_routes.bp)

    if cfg.start_cache_loader:
        app.before_request(cache.public.start_background_loader)

    return app
