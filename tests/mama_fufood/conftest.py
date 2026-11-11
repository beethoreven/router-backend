"""媽媽記副食品的測試設定。

- 要資料庫的測試用 `db` fixture(底下是 tests/conftest.py 的 empty_database:TEST_DATABASE_URL
  沒設就跳過)。**不要拿正式的資料庫來跑**:每次都會把整個 public schema 清空。
- Google 的 ID token 驗證換成假的:`tok:<sub>:<email>` 這種字串會被當成驗證通過。
- `client` 是整個服務(router)的測試用戶端,路徑自動加上 /mama-fufood,測試裡照舊寫 /api/...。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from tests.conftest import TEST_DATABASE_URL

APP_DIR = Path(__file__).resolve().parents[2] / "apps" / "mama_fufood"
PREFIX = "/mama-fufood"

MAMA_FUFOOD_ENV = {
    "MAMA_FUFOOD__GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
    "MAMA_FUFOOD__SESSION_SECRET": "test-session-secret",
    "MAMA_FUFOOD__FIRST_ADMIN_EMAIL": "admin@example.com",
    "MAMA_FUFOOD__ALLOWED_ORIGINS": "http://localhost:8003",
    "MAMA_FUFOOD__START_CACHE_LOADER": "0",
}


def fake_verify(token, client_id):
    assert client_id == MAMA_FUFOOD_ENV["MAMA_FUFOOD__GOOGLE_CLIENT_ID"]
    if not isinstance(token, str) or not token.startswith("tok:"):
        return None
    _, sub, email = token.split(":", 2)
    return {"sub": sub, "email": email.lower()}


def bearer(sub: str = "user-1", email: str = "mom@example.com") -> dict:
    return {"Authorization": f"Bearer tok:{sub}:{email}"}


class PrefixedClient:
    """路徑自動加上 /mama-fufood 的測試用戶端(其他行為照 Flask 的 test client)。"""

    def __init__(self, client):
        self.raw = client

    def __getattr__(self, name):
        method = getattr(self.raw, name)
        if name not in ("get", "post", "put", "patch", "delete", "options", "open"):
            return method

        def call(path, *args, **kwargs):
            return method(PREFIX + path, *args, **kwargs)

        return call


def _reset_memory():
    from apps.mama_fufood import admin_routes, auth, cache, db, routes

    db.database.reset_for_tests()
    cache.public.reset_for_tests()
    auth.admin.forget_all()
    routes.check_quota.clear()
    routes.convert_quota.clear()
    routes.feedback_attempts.clear()
    admin_routes.login_attempts.clear()


@pytest.fixture
def mama_fufood_env():
    env = dict(MAMA_FUFOOD_ENV)
    if TEST_DATABASE_URL:
        env["MAMA_FUFOOD__DATABASE_URL"] = TEST_DATABASE_URL
    return env


@pytest.fixture
def service(monkeypatch, mama_fufood_env):
    """整個服務(只掛 mama-fufood)。"""
    from router.dispatcher import Mount
    from router.root import build
    from shared import google_auth

    monkeypatch.setattr(google_auth, "verify", fake_verify)
    _reset_memory()
    yield build(mounts=(Mount("mama-fufood", "apps.mama_fufood.app:create_app"),), environ=mama_fufood_env)
    _reset_memory()


@pytest.fixture
def client(service):
    return PrefixedClient(service.test_client())


@pytest.fixture
def db(empty_database):
    """清空測試資料庫,下一次連線會重新建表並放入第一版公用資料。"""
    _reset_memory()
    yield empty_database
    _reset_memory()


@pytest.fixture
def admin_token(client, db):
    resp = client.post("/api/admin/login", json={"credential": "tok:admin-sub:admin@example.com"})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["token"]


def admin_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def pass_headers(sub: str = "user-1", email: str = "mom@example.com", now: float | None = None) -> dict:
    from apps.mama_fufood.auth import app_pass

    token, _ = app_pass.issue(MAMA_FUFOOD_ENV["MAMA_FUFOOD__SESSION_SECRET"], sub, email, now=now)
    return {"Authorization": f"Bearer {token}"}


def load_seed(name: str) -> dict:
    return json.loads((APP_DIR / "seed" / f"{name}.json").read_text(encoding="utf-8"))


def load_sample(version: int) -> dict:
    return json.loads((APP_DIR / "export_formats" / "samples" / f"v{version}.json").read_text(encoding="utf-8"))


def gz(data) -> bytes:
    return gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"))
