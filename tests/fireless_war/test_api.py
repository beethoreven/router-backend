"""fireless-war's endpoints through the whole router (/fireless-war/...)."""

from __future__ import annotations

import types

import limits.storage.memory
import pytest

from apps.fireless_war import google_access, routes
from router.settings import ConfigError

from .conftest import ENV, GM, STRANGER

P = "/fireless-war"
DEMO_TITLE = "FirelessWar_Demo_2026_07_01_18_30"
PREFLIGHT = {"Origin": ENV["FIRELESS_WAR__ALLOWED_ORIGINS"].split(",")[0], "Access-Control-Request-Method": "GET",
             "Access-Control-Request-Headers": "Authorization"}


@pytest.fixture
def clock(monkeypatch):
    """Rate-limit time travel: clock.advance(seconds) moves the counters' clock forward."""
    now = [1_000_000.0]
    monkeypatch.setattr(limits.storage.memory, "time", types.SimpleNamespace(time=lambda: now[0]))
    return types.SimpleNamespace(advance=lambda seconds: now.__setitem__(0, now[0] + seconds))


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


@pytest.mark.parametrize("value", [
    "2026_07_01_18_30\n",                 # "$" would accept a trailing newline
    "２０２６_０７_０１_１８_３０",          # full-width digits
    "٢٠٢٦_٠٧_٠١_١٨_٣٠",                    # Arabic-Indic digits ("\d" accepts both)
    "2026_07_01_18_301",
    " 2026_07_01_18_30",
])
def test_datetime_must_be_exactly_ascii_digits(client, monkeypatch, value):
    calls = []
    monkeypatch.setattr(google_access, "find_file_by_name", lambda name, folder: calls.append(name))
    monkeypatch.setattr(google_access.requests, "post", lambda *a, **k: calls.append("post"))
    for method in ("get", "post"):
        resp = getattr(client, method)(f"{P}/record", query_string={"datetime": value}, headers=GM)
        assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_param"
    assert calls == []


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

    demo = client.post(f"{P}/demo-record?datetime=2026_07_01_18_30", headers=STRANGER)
    assert calls[1][1]["mode"] == "demo" and calls[1][1]["editor_email"] == "someone@example.com"
    # Apps Script's warning text is for GMs only.
    assert demo.status_code == 201 and demo.get_json() == {"spreadsheet_id": "new-sid"}


def test_demo_failures_are_fixed_text(client, monkeypatch):
    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"statusCode": 500, "error": "Exception: owner-folder-detail"}

    def broken(*args, **kwargs):
        raise RuntimeError("inner-detail")

    monkeypatch.setattr(google_access.requests, "post", lambda url, json, timeout: Reply())
    monkeypatch.setattr(google_access, "find_file_by_name", broken)
    monkeypatch.setattr(google_access, "spreadsheet_title", broken)
    answers = [
        client.post(f"{P}/demo-record?datetime=2026_07_01_18_30", headers=STRANGER),
        client.get(f"{P}/demo-record?datetime=2026_07_01_18_30", headers=STRANGER),
        client.get(f"{P}/demo-round?day=1st&type=Morning&spreadsheet_id=abc", headers=STRANGER),
    ]
    for resp, key in zip(answers, ("create_failed", "find_failed", "round_failed")):
        assert resp.status_code == 500 and resp.get_json()["code"] == "google_error"
        assert resp.get_json()["error"] == routes.DEMO_MESSAGES[key]

    # A GM still sees Apps Script's own message.
    gm = client.post(f"{P}/record?datetime=2026_07_01_18_30", headers=GM).get_json()
    assert "owner-folder-detail" in gm["error"]


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
    monkeypatch.setattr(google_access, "spreadsheet_title", lambda sid: DEMO_TITLE)
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: _sheet(rows))
    body = client.get(f"{P}/demo-round?day=1st&type=Morning&spreadsheet_id=s", headers=STRANGER).get_json()
    assert list(body["business_level"]) == ["oniwara"] and body["business_level"]["oniwara"]["general_business"] == 1


@pytest.mark.parametrize("title,status", [
    (DEMO_TITLE, 200),
    ("FirelessWar_2026_07_01_18_30", 403),           # a real game's session file
    ("FirelessWar_Demo", 403),                       # the demo template itself
    ("FirelessWar_Demo_2026_07_01_18_30 (1)", 403),
    ("Budget 2026", 403),
])
def test_demo_round_reads_only_demo_session_files(client, monkeypatch, title, status):
    reads = []
    monkeypatch.setattr(google_access, "spreadsheet_title", lambda sid: title)
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: reads.append(sid) or _sheet([]))
    resp = client.get(f"{P}/demo-round?day=1st&type=Morning&spreadsheet_id=other-sheet", headers=STRANGER)
    assert resp.status_code == status, resp.get_json()
    if status == 403:
        assert resp.get_json()["code"] == "not_demo_file" and reads == []
    # A GM's /round is unchanged: it reads whatever session file it is given.
    assert client.get(f"{P}/round?day=1st&type=Morning&spreadsheet_id=other-sheet", headers=GM).status_code == 200


def test_demo_round_never_falls_back_to_the_configured_sheet(make_client, monkeypatch):
    touched = []
    monkeypatch.setattr(google_access, "spreadsheet_title", lambda sid: touched.append(sid) or DEMO_TITLE)
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: touched.append(sid) or _sheet([]))
    client = make_client({**ENV, "FIRELESS_WAR__SPREADSHEET_ID": "production-sheet"})
    resp = client.get(f"{P}/demo-round?day=1st&type=Morning", headers=STRANGER)
    assert resp.status_code == 400 and resp.get_json()["code"] == "missing_param"
    assert touched == []


@pytest.mark.parametrize("sheet_id", ["a/b", "../x", "a?alt=media", "a b", "a%2Fb", "x" * 129, "abc\n"])
def test_spreadsheet_ids_outside_the_safe_characters_never_reach_google(client, monkeypatch, sheet_id):
    touched = []
    monkeypatch.setattr(google_access, "spreadsheet_title", lambda sid: touched.append(sid) or DEMO_TITLE)
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: touched.append(sid) or _sheet([]))
    for path, headers in (("/round", GM), ("/demo-round", STRANGER)):
        resp = client.get(f"{P}{path}", query_string={"day": "1st", "type": "Morning", "spreadsheet_id": sheet_id},
                          headers=headers)
        assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_param", (path, resp.get_json())
    assert touched == []
    ok = client.get(f"{P}/round?day=1st&type=Morning&spreadsheet_id=1bM_n6-Ab9", headers=GM)
    assert ok.status_code == 200 and touched == ["1bM_n6-Ab9"]


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
    monkeypatch.setattr(google_access, "spreadsheet_title", lambda sid: DEMO_TITLE)
    assert client.get(f"{P}/demo-round?day=1st&type=Morning&spreadsheet_id=s", headers=GM).status_code == 200


def test_demo_record_limit_is_ten_an_hour_per_route(client, monkeypatch):
    monkeypatch.setattr(google_access, "find_file_by_name", lambda name, folder: "sid")
    url = f"{P}/demo-record?datetime=2026_07_01_18_30"
    for _ in range(10):
        assert client.get(url, headers=STRANGER).status_code == 200
    assert client.get(url, headers=STRANGER).status_code == 429


def test_get_record_limit_is_site_wide_and_rejected_sign_ins_dont_use_it(client, monkeypatch, clock):
    monkeypatch.setattr(google_access, "find_file_by_name", lambda name, folder: "sid")
    url = f"{P}/record?datetime=2026_07_01_18_30"
    for _ in range(15):
        assert client.get(url, headers=STRANGER).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer down"}).status_code == 503
    for _ in range(10):
        assert client.get(url, headers=GM).status_code == 200
    resp = client.get(url, headers={"Authorization": "Bearer tok:other-gm@example.com"})
    assert resp.status_code == 429 and resp.get_json()["code"] == "http_429"
    clock.advance(61)
    assert client.get(url, headers=GM).status_code == 200


def _apps_script_ok(monkeypatch):
    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"statusCode": 200, "spreadsheet_id": "new-sid"}

    monkeypatch.setattr(google_access.requests, "post", lambda url, json, timeout: Reply())


def test_create_record_limit_is_three_a_minute_and_twenty_an_hour(client, monkeypatch, clock):
    _apps_script_ok(monkeypatch)
    url = f"{P}/record?datetime=2026_07_01_18_30"
    for _ in range(10):
        assert client.post(url, headers=STRANGER).status_code == 401
    created = 0
    for _ in range(6):                      # 6 minutes x 3 = 18 within the hour
        for _ in range(3):
            assert client.post(url, headers=GM).status_code == 201
            created += 1
        assert client.post(url, headers=GM).status_code == 429
        clock.advance(61)
    for _ in range(2):
        assert client.post(url, headers=GM).status_code == 201
        created += 1
    assert created == 20
    clock.advance(61)
    assert client.post(url, headers=GM).status_code == 429, "the hourly cap should still hold"
    clock.advance(3600)
    assert client.post(url, headers=GM).status_code == 201


def test_auth_status_limit_is_per_sign_in(client, clock):
    a, b = {"Authorization": "Bearer tok:gm@example.com"}, {"Authorization": "Bearer tok:someone@example.com"}
    for _ in range(30):
        assert client.get(f"{P}/auth/status", headers={"Authorization": "Bearer junk"}).status_code == 401
        assert client.get(f"{P}/auth/status", headers={"Authorization": "Bearer down"}).status_code == 503
    for _ in range(10):
        assert client.get(f"{P}/auth/status", headers=a).status_code == 200
    resp = client.get(f"{P}/auth/status", headers=a)
    assert resp.status_code == 429 and resp.get_json()["code"] == "http_429"
    # Another sign-in (here a non-GM, as anyone can get through the demo) has its own counter...
    for _ in range(10):
        assert client.get(f"{P}/auth/status", headers=b).status_code == 200
    assert client.get(f"{P}/auth/status", headers=b).status_code == 429
    # ...and the GM's own counter recovers after a minute.
    clock.advance(61)
    assert client.get(f"{P}/auth/status", headers=a).status_code == 200


def test_preflights_never_use_a_counter(client, monkeypatch):
    monkeypatch.setattr(google_access, "find_file_by_name", lambda name, folder: "sid")
    monkeypatch.setattr(google_access, "sheet_batch_read", lambda ranges, sid: _sheet([]))
    _apps_script_ok(monkeypatch)
    paths = ["/auth/status", "/record?datetime=2026_07_01_18_30", "/round?day=1st&type=Morning&spreadsheet_id=s"]
    for path in paths:
        for _ in range(40):
            assert client.options(f"{P}{path}", headers=PREFLIGHT).status_code == 200
    assert client.get(f"{P}/auth/status", headers=GM).status_code == 200
    assert client.get(f"{P}/record?datetime=2026_07_01_18_30", headers=GM).status_code == 200
    assert client.post(f"{P}/record?datetime=2026_07_01_18_30", headers=GM).status_code == 201
    assert client.get(f"{P}/round?day=1st&type=Morning&spreadsheet_id=s", headers=GM).status_code == 200


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
