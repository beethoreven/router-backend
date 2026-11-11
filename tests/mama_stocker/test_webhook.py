"""經過 router 的 /mama-stocker：webhook 的順序與回應、走勢圖網址、/warm 與預抓的閘門。"""

import json

import pytest

from apps.mama_stocker import app as backend
from apps.mama_stocker.line_utils import images, signature


def _post(client, events, secret="test-secret", **headers):
    raw = json.dumps({"events": events}).encode()
    return client.post("/mama-stocker/line/webhook", data=raw, headers={
        "X-Line-Signature": signature.sign(raw, secret), **headers})


def _group_message(uid, text, *, tag_bot=False):
    msg = {"type": "text", "id": "1", "text": text}
    if tag_bot:
        msg["mention"] = {"mentionees": [{"index": 0, "length": 4, "isSelf": True}]}
    return {"type": "message", "replyToken": f"token-{uid}",
            "source": {"type": "group", "groupId": "G1", "userId": uid},
            "message": msg}


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(backend.line_client, "reply",
                        lambda token, message: log.append(("reply", token, message)))
    monkeypatch.setattr(backend.line_handler, "note_group_member",
                        lambda event: log.append(("note", event["source"].get("userId"))))
    monkeypatch.setattr(backend.line_handler, "handle_command",
                        lambda text, *, sender, private: "ok")
    return log


def test_replies_go_out_before_group_members_are_recorded(mama_stocker, calls):
    events = [_group_message("Ustranger", "隨便說說"),
              _group_message("Uamy", "@bot 台積電", tag_bot=True)]
    resp = _post(mama_stocker, events)
    assert resp.status_code == 200
    assert calls == [("reply", "token-Uamy", "ok"), ("note", "Ustranger"), ("note", "Uamy")]


def test_failed_recording_still_returns_200(mama_stocker, calls, monkeypatch):
    def boom(event):
        raise RuntimeError("資料庫連不上")
    monkeypatch.setattr(backend.line_handler, "note_group_member", boom)
    resp = _post(mama_stocker, [_group_message("Uamy", "@bot 台積電", tag_bot=True)])
    assert resp.status_code == 200
    assert calls == [("reply", "token-Uamy", "ok")]


def test_failed_event_still_returns_200(mama_stocker, calls, monkeypatch):
    def boom(text, *, sender, private):
        raise RuntimeError("壞了")
    monkeypatch.setattr(backend.line_handler, "handle_command", boom)
    resp = _post(mama_stocker, [_group_message("Uamy", "@bot 台積電", tag_bot=True)])
    assert resp.status_code == 200


def test_dry_run_does_not_record(mama_stocker, calls):
    resp = _post(mama_stocker, [_group_message("Uamy", "@bot 台積電", tag_bot=True)], **{"X-Dry-Run": "1"})
    assert resp.json["dry_run"][0]["replies"] == ["ok"]
    assert calls == []


def test_bad_signature_is_401(mama_stocker, calls):
    resp = _post(mama_stocker, [_group_message("Uamy", "@bot 台積電", tag_bot=True)], secret="wrong")
    assert resp.status_code == 401
    assert resp.json["code"] == "bad_signature"
    assert calls == []


def test_line_not_configured_is_503(mama_stocker_env):
    from router.root import build

    del mama_stocker_env["MAMA_STOCKER__LINE_CHANNEL_ACCESS_TOKEN"]
    client = build(environ=mama_stocker_env).test_client()
    resp = _post(client, [])
    assert resp.status_code == 503
    assert resp.json["code"] == "line_not_configured"


def test_settings_come_from_the_mama_stocker_prefix(mama_stocker_env):
    from apps.mama_stocker import config
    from router.root import build

    mama_stocker_env["MAMA_STOCKER__AUTH_CACHE_TTL_SECONDS"] = "17"
    mama_stocker_env["MAMA_STOCKER__DATABASE_URL"] = "postgresql://mama-stocker.invalid/db"
    client = build(environ=mama_stocker_env).test_client()
    client.get("/mama-stocker/warm")   # 第一個請求才載入 app
    cfg = config.get()
    assert cfg.auth_cache_ttl_seconds == 17
    assert cfg.database_url == "postgresql://mama-stocker.invalid/db"
    assert cfg.line_channel_secret == "test-secret"
    assert "test-secret" not in repr(cfg) and "mama-stocker.invalid" not in repr(cfg)


# ── 走勢圖網址 ─────────────────────────────────────────────────

def test_trend_reply_is_an_https_url_under_the_mama_stocker_prefix(mama_stocker, calls, monkeypatch):
    monkeypatch.setattr(backend.line_handler, "handle_command",
                        lambda text, *, sender, private: images.ImageReply.trend("2330"))
    _post(mama_stocker, [_group_message("Uamy", "@bot 台積電 走勢", tag_bot=True)])
    (_, _, message), = [c for c in calls if c[0] == "reply"]
    url = message[0]["originalContentUrl"]
    assert message[0]["previewImageUrl"] == url
    assert url.startswith("https://localhost/mama-stocker/line/img/t2330.")
    assert url.endswith(".png")


def test_signed_image_url_is_served(mama_stocker, monkeypatch):
    monkeypatch.setattr(images, "render", lambda spec: b"\x89PNG" if spec == "t2330" else None)
    token = images.sign("t2330", "test-secret")
    resp = mama_stocker.get(f"/mama-stocker/line/img/{token}.png")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == b"\x89PNG"


def test_forged_image_url_is_404(mama_stocker, monkeypatch):
    monkeypatch.setattr(images, "render", lambda spec: b"\x89PNG")
    token = images.sign("t2330", "someone-elses-secret")
    resp = mama_stocker.get(f"/mama-stocker/line/img/{token}.png")
    assert resp.status_code == 404
    assert resp.json["code"] == "bad_image_url"


# ── /warm 與預抓 ───────────────────────────────────────────────

def test_warm_touches_no_database_and_says_nothing_about_settings(mama_stocker, monkeypatch):
    def no_db():
        raise AssertionError("/warm 不該連資料庫")
    monkeypatch.setattr(backend.db_connection, "connection", no_db)
    resp = mama_stocker.get("/mama-stocker/warm")
    assert resp.status_code == 200
    assert set(resp.json) == {"ok", "warm_seconds_ago"}


@pytest.fixture
def warm_runs(monkeypatch, mama_stocker_env):
    """預抓打開，但 _warm_up 換成只記次數；執行緒同步跑完。"""
    from router.root import build

    runs = []
    monkeypatch.setattr(backend, "_warm_up", lambda: runs.append(1))
    monkeypatch.setattr(backend, "_warm_next", 0.0)

    class _Inline:
        def __init__(self, target, **_):
            self._target = target

        def start(self):
            self._target()
    monkeypatch.setattr(backend.threading, "Thread", _Inline)
    mama_stocker_env["MAMA_STOCKER__START_WARM_UP"] = "1"
    return build(environ=mama_stocker_env).test_client(), runs


def test_warm_up_runs_at_most_once_per_interval_whatever_the_path(warm_runs):
    """閘門只有「間隔＋鎖」：亂打的 404 也會觸發 before_request，不能靠「只有 cron 會打」。"""
    client, runs = warm_runs
    client.get("/mama-stocker/warm")
    client.get("/mama-stocker/no-such-page")
    client.get("/mama-stocker/warm")
    assert len(runs) == 1
    assert client.get("/mama-stocker/warm").json["warm_seconds_ago"] == 0


def test_warm_up_interval_is_shorter_than_the_cron_period():
    assert backend._WARM_EVERY < 600


def test_router_health_does_not_trigger_warm_up(warm_runs):
    client, runs = warm_runs
    client.get("/health")
    assert runs == []
