"""The service as a whole: /health, /ready, and every app mounted under its prefix.

    Local:       .venv/bin/python -m router.root           (http://localhost:5010)
    Production:  gunicorn wsgi:app --workers 1 --threads 16 --timeout 120

★ Exactly one worker. Apps keep per-day counters and caches in process memory (daily-check
  quotas, for one); with two workers each would count separately.
★ More threads than a single app would need: the apps share them, and one slow request
  (a large upload, a slow outside call) must not leave the others waiting.
★ gunicorn's access log is turned off in code. It records every client IP, and the apps promise
  not to keep IPs; Render adds its own flags, so start-up arguments can't be relied on.
"""

from __future__ import annotations

import logging
import os

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from router import settings as settings_module
from router.dispatcher import LazyApp, Mount, build_mounts

# Every app this service hosts. Adding one: put its package under apps/<name>/ with a
# create_app(settings) function, then add a line here. The name is also its URL prefix and its
# environment-variable prefix (see router/settings.py).
MOUNTS: tuple[Mount, ...] = (
    Mount("all-mo-diary", "apps.all_mo_diary.app:create_app"),
    Mount("mama-fufood", "apps.mama_fufood.app:create_app"),
    Mount("fireless-war", "apps.fireless_war.app:create_app"),
    Mount("mama-stocker", "apps.mama_stocker.app:create_app"),
)


def build(mounts: tuple[Mount, ...] = MOUNTS, environ=None):
    """Check every app's settings (raises ConfigError on a mistake), then assemble the WSGI app."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("gunicorn.access").disabled = True

    all_settings = settings_module.load_all([m.name for m in mounts], environ)
    lazy = build_mounts(mounts, all_settings)
    root = Flask("router")
    root.json.ensure_ascii = False
    commit = (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]

    @root.get("/health")
    def health():
        """Keep-alive and liveness. Loads nothing and never touches a database."""
        return jsonify({"ok": True, "commit": commit})

    @root.get("/ready")
    def ready():
        """Whether every app loads. Loads apps that haven't been used yet; touches no database
        (this route needs no login, so it must not be a way to keep a database awake)."""
        apps = {}
        for prefix, app in lazy.items():
            app.load()
            apps[prefix.lstrip("/")] = app.status()
        ok = all(a["loaded"] for a in apps.values())
        return jsonify({"ok": ok, "apps": apps, "commit": commit}), 200 if ok else 503

    @root.errorhandler(HTTPException)
    def _http_error(exc):
        messages = {404: "找不到這個網址", 405: "不支援這個操作"}
        return jsonify({"error": messages.get(exc.code, exc.name), "code": f"http_{exc.code}"}), exc.code

    root.wsgi_app = DispatcherMiddleware(root.wsgi_app, lazy)
    root.extensions["router.mounts"] = lazy
    return root


def mounted(app: Flask) -> dict[str, LazyApp]:
    """The lazy apps behind a built service (for tests and diagnostics)."""
    return app.extensions["router.mounts"]


if __name__ == "__main__":
    build().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5010")), debug=False)
