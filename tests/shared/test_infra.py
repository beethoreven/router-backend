"""shared.database caps, shared.payload, shared.quota, shared.rejections, shared.snapshot_cache,
shared.flask_app — the parts that need no real database."""

from __future__ import annotations

import gzip
import threading

import pytest
from flask import abort, request

from shared import database as database_module
from shared import payload, timeutil
from shared.database import Database, DatabaseBusy, DatabaseNotConfigured
from shared.flask_app import allow_cors, new_app
from shared.google_auth import VerificationUnavailable
from shared.quota import DailyCounter
from shared.rejections import RejectionLog
from shared.snapshot_cache import SnapshotCache


# ── database caps (no connection is ever made: slots are checked first) ──

def test_no_url_is_not_configured():
    db = Database("x", url=lambda: "")
    assert not db.configured()
    with pytest.raises(DatabaseNotConfigured):
        with db.connection():
            pass


def test_app_cap_and_process_cap(monkeypatch):
    connected = []

    class FakeConn:
        def execute(self, *a, **k): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(database_module.psycopg, "connect", lambda *a, **k: connected.append(1) or FakeConn())
    monkeypatch.setattr(database_module, "_process_slots", threading.BoundedSemaphore(2))

    a = Database("a", url=lambda: "postgresql://a", max_connections=1, slot_wait=0.05)
    b = Database("b", url=lambda: "postgresql://b", max_connections=2, slot_wait=0.05)

    with a.connection():
        with pytest.raises(DatabaseBusy, match="this app's limit"):  # a's own single slot is taken
            with a.connection():
                pass
        with b.connection():  # the second process slot
            with pytest.raises(DatabaseBusy, match="process limit"):  # b has room, the process doesn't
                with b.connection():
                    pass
    # Everything was given back: both apps can connect again.
    with a.connection(), b.connection():
        pass
    assert len(connected) == 4 and a.last_ok is not None


def test_per_app_cap_cannot_exceed_process_cap():
    with pytest.raises(ValueError):
        Database("x", url=lambda: "u", max_connections=database_module.PROCESS_MAX_CONNECTIONS + 1)


# ── payload ──

def test_gzip_bomb_is_stopped_at_the_limit():
    bomb = gzip.compress(b"\0" * (5 * 1024 * 1024))
    with pytest.raises(payload.PayloadError) as err:
        payload.gunzip_limited(bomb, 1024 * 1024)
    assert err.value.status == 413
    assert payload.gunzip_limited(gzip.compress(b"hello"), 10) == b"hello"
    with pytest.raises(payload.PayloadError):
        payload.gunzip_limited(b"not gzip", 10)


def test_json_body_limits():
    app = new_app("t", max_content_length=1024 * 1024)

    @app.post("/echo")
    def echo():
        return {"got": payload.read_json(request, 64, max_decompressed=256)}

    client = app.test_client()
    assert client.post("/echo", json={"a": 1}).get_json() == {"got": {"a": 1}}
    big = client.post("/echo", json={"a": "x" * 100})
    assert big.status_code == 413 and big.get_json()["code"] == "too_large"
    zipped = client.post("/echo", data=gzip.compress(b'{"a": 2}'), headers={"Content-Encoding": "gzip"})
    assert zipped.get_json() == {"got": {"a": 2}}
    assert client.post("/echo", data=b"{nope", content_type="application/json").status_code == 400


# ── quota ──

def test_daily_counter_resets_at_taipei_midnight(monkeypatch):
    from datetime import datetime, timezone

    clock = {"now": datetime(2026, 11, 11, 15, 59, tzinfo=timezone.utc)}  # 23:59 Taipei
    monkeypatch.setattr(timeutil, "now", lambda: clock["now"])
    counter = DailyCounter(lambda: 2)
    assert counter.try_take("u") and counter.try_take("u") and not counter.try_take("u")
    counter.give_back("u")
    assert counter.try_take("u") and counter.used("u") == 2
    clock["now"] = datetime(2026, 11, 11, 16, 0, tzinfo=timezone.utc)  # 00:00 Taipei, next day
    assert counter.used("u") == 0 and counter.try_take("u")


# ── rejections ──

def test_rejections_are_summarised_per_app(caplog, monkeypatch):
    diary, other = RejectionLog("diary", window_seconds=0), RejectionLog("other", window_seconds=1000)
    diary.note("bad body")
    other.note("bad body")
    with caplog.at_level("WARNING", logger="shared.rejections"):
        diary.note("bad body")  # the window (0 s) has passed: summary of the first one
        other.note("bad body")  # still inside its window: nothing logged
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "[diary]" in lines[0] and "bad body 1 次" in lines[0]


# ── snapshot cache ──

def test_snapshot_cache_never_waits_behind_a_load():
    started, release = threading.Event(), threading.Event()

    def slow_load():
        started.set()
        release.wait(5)
        return {"v": 1}

    cache = SnapshotCache("t", load=slow_load, retry_seconds=lambda: 1)
    t = threading.Thread(target=cache.reload)
    t.start()
    started.wait(5)
    assert cache.ensure_loaded() is None  # loading elsewhere: answer at once, don't queue
    release.set()
    t.join(5)
    assert cache.ensure_loaded() == {"v": 1}


def test_snapshot_cache_failure_keeps_old_copy_and_waits_between_attempts():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db asleep")
        return {"v": calls["n"]}

    cache = SnapshotCache("t", load=flaky, retry_seconds=lambda: 1)
    assert cache.ensure_loaded() is None
    assert cache.ensure_loaded() is None and calls["n"] == 1  # within REQUEST_RETRY_MIN_SECONDS: no retry
    assert cache.reload() == {"v": 2}
    cache.reset_for_tests()
    assert cache.snapshot() is None


def test_snapshot_loader_starts_once_and_only_when_enabled(monkeypatch):
    starts = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: starts.append(self.name))
    off = SnapshotCache("off", load=dict, retry_seconds=lambda: 1, enabled=lambda: False)
    off.start_background_loader()
    on = SnapshotCache("on", load=dict, retry_seconds=lambda: 1)
    on.start_background_loader()
    on.start_background_loader()
    assert starts == ["on-cache-loader"]


# ── flask app ──

def test_error_shapes():
    app = new_app("t", max_content_length=10)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret user text")

    @app.get("/busy")
    def busy():
        raise DatabaseBusy("x")

    @app.get("/google")
    def google():
        raise VerificationUnavailable()

    @app.get("/forbidden")
    def forbidden():
        abort(403)

    client = app.test_client()
    assert client.get("/boom").get_json() == {"error": "伺服器發生錯誤", "code": "server_error"}
    assert client.get("/busy").status_code == 503 and client.get("/busy").get_json()["code"] == "db_busy"
    assert client.get("/google").status_code == 503
    assert client.get("/google").get_json()["code"] == "auth_unavailable"
    assert client.get("/forbidden").get_json()["code"] == "http_403"
    assert client.get("/nope").get_json()["code"] == "http_404"
    assert client.post("/boom", data=b"x" * 100).status_code in (405, 413)


def test_cors_takes_exact_origins_only():
    app = new_app("t", max_content_length=10)
    with pytest.raises(ValueError):
        allow_cors(app, ["*"])
    with pytest.raises(ValueError):
        allow_cors(app, [r"http://localhost:\d+"])
    allow_cors(app, [], paths=r"/api/*")  # nothing configured: no CORS at all

    @app.get("/api/x")
    def x():
        return {}

    resp = app.test_client().get("/api/x", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in resp.headers
