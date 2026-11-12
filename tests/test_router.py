"""The shared service: /health, /ready, prefix dispatch, and one app failing without the others."""

from __future__ import annotations

import logging
import sys
import threading
import types

import pytest

from router import dispatcher
from router.dispatcher import Mount
from router.root import build, mounted


def _fake_app_module(monkeypatch, module_name: str, *, fails: bool = False):
    """Register an importable module whose create_app returns a tiny Flask app (or raises)."""
    from flask import Flask

    module = types.ModuleType(module_name)

    def create_app(settings):
        if fails:
            raise RuntimeError("broken on purpose")
        app = Flask(module_name)

        @app.get("/hello")
        def hello():
            return {"app": settings.app, "secret_present": bool(settings.text("SESSION_SECRET"))}

        return app

    module.create_app = create_app
    monkeypatch.setitem(sys.modules, module_name, module)


@pytest.fixture
def two_apps(monkeypatch):
    _fake_app_module(monkeypatch, "fake_ok")
    _fake_app_module(monkeypatch, "fake_broken", fails=True)
    mounts = (Mount("good", "fake_ok:create_app"), Mount("bad", "fake_broken:create_app"))
    return build(mounts, environ={"GOOD__SESSION_SECRET": "s1"})


def test_health_loads_no_app(two_apps):
    resp = two_apps.test_client().get("/health")
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert all(not app.status()["loaded"] for app in mounted(two_apps).values())


def test_requests_reach_the_app_under_its_prefix_with_its_own_settings(two_apps):
    resp = two_apps.test_client().get("/good/hello")
    assert resp.status_code == 200
    assert resp.get_json() == {"app": "good", "secret_present": True}


def test_a_broken_app_only_breaks_its_own_prefix(two_apps):
    client = two_apps.test_client()
    resp = client.get("/bad/anything")
    assert resp.status_code == 503 and resp.get_json()["code"] == "app_unavailable"
    assert client.get("/good/hello").status_code == 200
    assert client.get("/health").status_code == 200


def test_ready_reports_each_app(two_apps):
    resp = two_apps.test_client().get("/ready")
    body = resp.get_json()
    assert resp.status_code == 503
    assert body["apps"]["good"] == {"loaded": True, "error": None}
    assert body["apps"]["bad"] == {"loaded": False, "error": "RuntimeError"}


def test_failed_load_is_retried_only_after_the_wait(monkeypatch):
    calls = []
    module = types.ModuleType("fake_flaky")

    def create_app(settings):
        calls.append(1)
        raise RuntimeError("still broken")

    module.create_app = create_app
    monkeypatch.setitem(sys.modules, "fake_flaky", module)
    now = [1000.0]
    lazy = dispatcher.LazyApp(Mount("flaky", "fake_flaky:create_app"),
                              settings=None, clock=lambda: now[0])
    lazy.load()
    lazy.load()
    assert calls == [1]
    now[0] += dispatcher.RETRY_SECONDS
    lazy.load()
    assert calls == [1, 1]


def test_unknown_url_is_json(two_apps):
    resp = two_apps.test_client().get("/nope")
    assert resp.status_code == 404 and resp.get_json()["code"] == "http_404"


def test_building_starts_no_thread_and_imports_no_app(monkeypatch):
    before = threading.active_count()
    monkeypatch.delitem(sys.modules, "apps.all_mo_diary.app", raising=False)
    build(environ={})
    assert threading.active_count() == before
    assert "apps.all_mo_diary.app" not in sys.modules


def test_gunicorn_access_log_is_off():
    build(environ={})
    assert logging.getLogger("gunicorn.access").disabled is True


def test_the_real_diary_app_loads():
    service = build(environ={})
    resp = service.test_client().get("/ready")
    assert resp.status_code == 200
    assert resp.get_json()["apps"]["diary"] == {"loaded": True, "error": None}
    missing = service.test_client().get("/diary/no-such-page")
    assert missing.status_code == 404 and missing.get_json()["code"] == "http_404"
