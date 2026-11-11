"""fireless-war's endpoints through the whole router (/fireless-war/...)."""

from __future__ import annotations

import pytest

from apps.fireless_war import google_access
from router.settings import ConfigError

from .conftest import ENV, GM, STRANGER

P = "/fireless-war"


# ---- sign-in ---------------------------------------------------------------------------------------

def test_auth_status_for_a_gm_matches_the_whitelist_ignoring_case(client):
    resp = client.get(f"{P}/auth/status", headers={"Authorization": "Bearer tok:Gm@EXAMPLE.com"})
    assert resp.status_code == 200
    assert resp.get_json() == {"authorized": True, "email": "gm@example.com"}


def test_auth_status_for_a_valid_non_gm_is_200_not_authorized(client):
    resp = client.get(f"{P}/auth/status", headers=STRANGER)
    assert resp.status_code == 200 and resp.get_json()["authorized"] is False


def test_auth_status_invalid_or_missing_token_is_401(client):
    for headers in ({"Authorization": "Bearer junk"}, {}):
        resp = client.get(f"{P}/auth/status", headers=headers)
        assert resp.status_code == 401
        assert resp.get_json()["authorized"] is False and resp.get_json()["code"] == "invalid_token"


def test_auth_status_google_unreachable_is_503_keeping_its_shape(client):
    resp = client.get(f"{P}/auth/status", headers={"Authorization": "Bearer down"})
    assert resp.status_code == 503
    assert resp.get_json()["authorized"] is False and resp.get_json()["code"] == "auth_unavailable"


@pytest.mark.parametrize("method,path", [("get", "/record"), ("post", "/record"), ("get", "/round")])
def test_gm_routes_reject_non_gms_and_unavailable_google(client, method, path):
    url = f"{P}{path}?datetime=2026_07_01_18_30&day=1st&type=Morning&spreadsheet_id=s"
    assert getattr(client, method)(url, headers=STRANGER).status_code == 401
    assert getattr(client, method)(url).status_code == 401
    resp = getattr(client, method)(url, headers={"Authorization": "Bearer down"})
    assert resp.status_code == 503 and resp.get_json()["code"] == "auth_unavailable"


def test_empty_whitelist_authorizes_nobody(make_client):
    client = make_client({**ENV, "FIRELESS_WAR__PERMITTED_USERS": ""})
    assert client.get(f"{P}/auth/status", headers=GM).get_json()["authorized"] is False


def test_demo_accepts_any_sign_in_and_answers_in_english(client, monkeypatch):
    monkeypatch.setattr(google_access, "find_file_by_name", lambda name, folder: None)
    resp = client.get(f"{P}/demo-record?datetime=2026_07_01_18_30", headers=STRANGER)
    assert resp.status_code == 404 and resp.get_json()["error"].startswith("No demo session")
    resp = client.get(f"{P}/demo-record?datetime=2026_07_01_18_30", headers={"Authorization": "Bearer down"})
    assert resp.status_code == 503 and resp.get_json()["error"].startswith("Could not verify")
    assert client.get(f"{P}/demo-record?datetime=2026_07_01_18_30").status_code == 401


# ---- /record ---------------------------------------------------------------------------------------

def test_get_record_finds_the_file_by_name_in_the_configured_folder(client, monkeypatch):
    seen = []
    monkeypatch.setattr(google_access, "find_file_by_name", lambda name, folder: seen.append((name, folder)) or "sid")
    resp = client.get(f"{P}/record?datetime=2026_07_01_18_30", headers=GM)
    assert resp.status_code == 200 and resp.get_json() == {"spreadsheet_id": "sid"}
    client.get(f"{P}/demo-record?datetime=2026_07_01_18_30", headers=GM)
    assert seen == [("FirelessWar_2026_07_01_18_30", "folder-1"), ("FirelessWar_Demo_2026_07_01_18_30", "folder-1")]


def test_get_record_not_found_and_bad_datetime(client, monkeypatch):
    monkeypatch.setattr(google_access, "find_file_by_name", lambda name, folder: None)
    assert client.get(f"{P}/record?datetime=2026_07_01_18_30", headers=GM).status_code == 404
    resp = client.get(f"{P}/record?datetime=2026-07-01", headers=GM)
    assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_param"
    resp = client.get(f"{P}/record", headers=GM)
    assert resp.status_code == 400 and resp.get_json()["code"] == "missing_param"


def test_a_value_error_deep_inside_google_is_500_not_a_bad_datetime(client, monkeypatch):
    """The old code turned any ValueError (e.g. a non-JSON reply) into "datetime 格式不正確"."""
    def broken(name, folder):
        raise ValueError("Expecting value: line 1 column 1")

    monkeypatch.setattr(google_access, "find_file_by_name", broken)
    resp = client.get(f"{P}/record?datetime=2026_07_01_18_30", headers=GM)
    assert resp.status_code == 500 and resp.get_json()["code"] == "google_error"


def test_library_error_text_is_not_echoed_back(client, monkeypatch):
    def leaky(name, folder):
        raise ConnectionError("for url: https://www.googleapis.com/drive/v3/files?q=secret-folder")

    monkeypatch.setattr(google_access, "find_file_by_name", leaky)
    body = client.get(f"{P}/record?datetime=2026_07_01_18_30", headers=GM).get_json()
    assert "secret-folder" not in body["error"] and "ConnectionError" in body["error"]


def test_create_record_sends_the_verified_email_and_returns_the_warning(client, monkeypatch):
    calls = []

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"statusCode": 200, "spreadsheet_id": "new-sid", "warning": "加編輯者失敗"}

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return Reply()

    monkeypatch.setattr(google_access.requests, "post", fake_post)
    resp = client.post(f"{P}/record?datetime=2026_07_01_18_30&editor_email=evil@example.com",
                       headers={"Authorization": "Bearer tok:GM@example.com"})
    assert resp.status_code == 201
    assert resp.get_json() == {"spreadsheet_id": "new-sid", "warning": "加編輯者失敗"}
    url, body, timeout = calls[0]
    assert url == "https://script.example/exec" and timeout == google_access.APPS_SCRIPT_TIMEOUT_SECONDS
    assert body == {"secret": "script-secret", "datetime": "2026_07_01_18_30", "editor_email": "gm@example.com"}

    client.post(f"{P}/demo-record?datetime=2026_07_01_18_30", headers=STRANGER)
    assert calls[1][1]["mode"] == "demo" and calls[1][1]["editor_email"] == "someone@example.com"


def test_apps_script_failure_message_reaches_the_gm(client, monkeypatch):
    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"statusCode": 403, "error": "密語不正確"}

    monkeypatch.setattr(google_access.requests, "post", lambda url, json, timeout: Reply())
    resp = client.post(f"{P}/record?datetime=2026_07_01_18_30", headers=GM)
    assert resp.status_code == 500 and "密語不正確" in resp.get_json()["error"]


# ---- /round ----------------------------------------------------------------------------------------

def _sheet(rows, *, flags=("否", "否", "否"), escape="否"):
    return [[["第一天 早晨"]], rows, [[flags[0]]], [[flags[1]]], [[flags[2]]], [["10"]], [["20"]], [["30"]], [[escape]]]


MORNING_ROWS = [
    ["鬼原響介", "鬼原商事", "1", "2", "0", "0", "0", "", "1,000", "5"],
    ["麥克・葛雷希爾", "M", "1", "0", "0", "3", "0", "", "500", "6"],
    ["鬼怒川新助", "K", "0", "0", "1", "0", "0", "", "0", "1"],
    ["河野麗一", "河野", "0", "0", "0", "0", "2", "", "0", "4"],
    ["PH-003", "PH", "2", "0", "0", "0", "0", "", "0", "7"],
]


def test_round_reads_one_batch_from_the_given_sheet(client, monkeypatch):
    seen = []
    monkeypatch.setattr(google_access, "sheet_batch_read",
                        lambda ranges, sid: seen.append((ranges[0], sid)) or _sheet(MORNING_ROWS, escape="是"))
    resp = client.get(f"{P}/round?day=1st&type=morning&spreadsheet_id=game-1", headers=GM)
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert seen == [("'1stDayMorning'!A1", "game-1")]
    assert body["type"] == "Morning" and body["round_label"] == "第一天 早晨"
    assert body["business_level"]["oniwara"]["owned_money"] == 1000
    # kouno_single is 否: 河野's points move onto PH-003.
    assert body["business_level"]["kouno"]["current_integral"] is None
    assert body["business_level"]["ph003"]["current_integral"] == 11
    assert body["legal_business"] == 10 + 4 and body["illegal_business"] == 20 + 2 + 3 + 1 + 2
    assert body["hot_business"] == "drug"


def test_round_without_a_sheet_id_uses_the_configured_one_or_fails_clearly(make_client, monkeypatch):
    seen = []
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: seen.append(sid) or _sheet([]))
    resp = make_client().get(f"{P}/round?day=1st&type=Morning", headers=GM)
    assert resp.status_code == 400 and resp.get_json()["code"] == "missing_param"
    client = make_client({**ENV, "FIRELESS_WAR__SPREADSHEET_ID": "fallback-sheet"})
    assert client.get(f"{P}/round?day=1st&type=Morning", headers=GM).status_code == 200
    assert seen == ["fallback-sheet"]


def test_round_bad_params_and_bad_cells(client, monkeypatch):
    assert client.get(f"{P}/round?day=9th&type=Morning&spreadsheet_id=s", headers=GM).status_code == 400
    assert client.get(f"{P}/round?day=Final&type=Morning&spreadsheet_id=s", headers=GM).status_code == 400
    bad = [["鬼原響介", "鬼原商事", "x", "0", "0", "0", "0", "", "0", "0"]]
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: _sheet(bad))
    resp = client.get(f"{P}/round?day=1st&type=Morning&spreadsheet_id=s", headers=GM)
    assert resp.status_code == 500 and "儲存格數值無法轉成數字" in resp.get_json()["error"]


def test_demo_round_uses_the_generic_names(client, monkeypatch):
    rows = [["Character A", "A", "1", "0", "0", "0", "0", "", "0", "0"], ["鬼原響介", "x", "9", "0", "0", "0", "0", "", "0", "0"]]
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: _sheet(rows))
    body = client.get(f"{P}/demo-round?day=1st&type=Morning&spreadsheet_id=s", headers=STRANGER).get_json()
    assert list(body["business_level"]) == ["oniwara"] and body["business_level"]["oniwara"]["general_business"] == 1


def test_round_limit_is_site_wide_and_rejected_sign_ins_dont_use_it(client, monkeypatch):
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: _sheet([]))
    url = f"{P}/round?day=1st&type=Morning&spreadsheet_id=s"
    for _ in range(40):
        assert client.get(url).status_code == 401
    for _ in range(30):
        assert client.get(url, headers=GM).status_code == 200
    resp = client.get(url, headers={"Authorization": "Bearer tok:other-gm@example.com"})
    assert resp.status_code == 429 and resp.get_json()["code"] == "http_429"
    # The demo has its own counter.
    assert client.get(f"{P}/demo-round?day=1st&type=Morning&spreadsheet_id=s", headers=GM).status_code == 200


def test_demo_record_limit_is_ten_an_hour_per_route(client, monkeypatch):
    monkeypatch.setattr(google_access, "find_file_by_name", lambda name, folder: "sid")
    url = f"{P}/demo-record?datetime=2026_07_01_18_30"
    for _ in range(10):
        assert client.get(url, headers=STRANGER).status_code == 200
    assert client.get(url, headers=STRANGER).status_code == 429


# ---- settings, CORS --------------------------------------------------------------------------------

def test_missing_settings_answer_503_and_ready_names_them(make_client):
    client = make_client({})
    service_ready = client.get("/ready")
    assert service_ready.status_code == 200
    assert service_ready.get_json()["apps"]["fireless-war"] == {"loaded": True, "error": None}
    resp = client.get(f"{P}/auth/status", headers=GM)
    assert resp.status_code == 503 and resp.get_json()["code"] == "not_configured"
    ready = client.get(f"{P}/ready")
    assert ready.status_code == 503
    assert "FIRELESS_WAR__SERVICE_ACCOUNT_JSON" in ready.get_json()["missing"]
    assert "FIRELESS_WAR__GOOGLE_CLIENT_ID" in ready.get_json()["missing"]


def test_ready_is_ok_when_everything_is_set(client):
    assert client.get(f"{P}/ready").get_json() == {"ok": True, "missing": []}


@pytest.mark.parametrize("value", [
    '{"type": "service_account", "private_key": "SECRET-KEY-MATERIAL',
    '{"type": "authorized_user", "client_email": "a", "private_key": "b"}',
    '{"type": "service_account", "private_key": "b"}',
    '{"type": "service_account", "client_email": "a"}',
    '["service_account"]',
])
def test_a_broken_service_account_key_stops_the_app_loading(make_client, value):
    client = make_client({**ENV, "FIRELESS_WAR__SERVICE_ACCOUNT_JSON": value})
    status = client.get("/ready").get_json()["apps"]["fireless-war"]
    assert status == {"loaded": False, "error": ConfigError.__name__}
    assert client.get(f"{P}/ready").status_code == 503
    assert client.get("/health").status_code == 200


def test_the_key_never_appears_in_the_config_error():
    from apps.fireless_war.config import FirelessWarConfig
    from router.settings import load_all

    settings = load_all(["fireless-war"], {"FIRELESS_WAR__SERVICE_ACCOUNT_JSON": '{"private_key": "SECRET-KEY-MATERIAL'})
    with pytest.raises(ConfigError) as caught:
        FirelessWarConfig.from_settings(settings["fireless-war"])
    err = caught.value
    assert "SECRET" not in str(err)
    assert err.__cause__ is None and err.__suppress_context__ is True


def test_bare_secrets_without_the_prefix_stop_the_service():
    from router.root import build

    for key in ("SERVICE_ACCOUNT_JSON", "APPS_SCRIPT_SECRET", "PERMITTED_USERS"):
        with pytest.raises(ConfigError):
            build(environ={key: "x"})


def test_cors_allows_only_the_listed_origins(client):
    ok = client.get(f"{P}/ready", headers={"Origin": "https://beethoreven.github.io"})
    assert ok.headers.get("Access-Control-Allow-Origin") == "https://beethoreven.github.io"
    local = client.get(f"{P}/ready", headers={"Origin": "http://localhost:8000"})
    assert local.headers.get("Access-Control-Allow-Origin") == "http://localhost:8000"
    for origin in ("http://localhost:8000.example.com", "http://localhost:9999", "https://evil.example"):
        resp = client.get(f"{P}/ready", headers={"Origin": origin})
        assert "Access-Control-Allow-Origin" not in resp.headers


def test_preflight_allows_the_authorization_header(client):
    resp = client.options(f"{P}/round", headers={
        "Origin": "https://beethoreven.github.io",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization",
    })
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://beethoreven.github.io"
    assert "authorization" in resp.headers.get("Access-Control-Allow-Headers", "").lower()
