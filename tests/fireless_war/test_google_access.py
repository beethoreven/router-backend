"""The service account is built from FIRELESS_WAR__SERVICE_ACCOUNT_JSON, and every call has a timeout."""

from __future__ import annotations

from apps.fireless_war import config, google_access

from .conftest import SERVICE_ACCOUNT


class FakeCreds:
    pass


def test_credentials_come_from_the_setting_with_read_only_scopes(client, monkeypatch):
    seen = {}

    def from_info(info, scopes):
        seen.update(info=info, scopes=scopes)
        return FakeCreds()

    monkeypatch.setattr(google_access.Credentials, "from_service_account_info", from_info)
    client.get("/fireless-war/ready")  # loads the app, which sets the config
    session = google_access.service_account.drive()
    assert seen["info"] == SERVICE_ACCOUNT
    assert all(s.endswith(".readonly") for s in seen["scopes"])
    assert session._refresh_timeout == google_access.TOKEN_REFRESH_TIMEOUT_SECONDS
    assert google_access.service_account.drive() is session


def test_drive_lookup_has_a_timeout_and_escapes_the_name(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"files": [{"id": "f1", "name": "x"}]}

    class Session:
        def get(self, url, params, timeout):
            calls.append((params["q"], timeout))
            return Resp()

    monkeypatch.setattr(google_access.service_account, "drive", lambda: Session())
    assert google_access.find_file_by_name("a'b", "folder") == "f1"
    query, timeout = calls[0]
    assert "name = 'a\\'b'" in query and timeout == google_access.GOOGLE_TIMEOUT_SECONDS


def test_a_slow_sheet_open_blocks_nobody_else(monkeypatch):
    """open_by_key is a network call: it must not hold the lock Drive lookups and other sheets need."""
    import threading

    config.set_current(config.FirelessWarConfig(service_account_info=SERVICE_ACCOUNT))
    monkeypatch.setattr(google_access.Credentials, "from_service_account_info", lambda info, scopes: FakeCreds())
    release, entered = threading.Event(), threading.Event()

    class HttpClient:
        def set_timeout(self, t):
            pass

    class Client:
        http_client = HttpClient()

        def open_by_key(self, sid):
            if sid == "slow":
                entered.set()
                release.wait(5)
            return f"sheet:{sid}"

    monkeypatch.setattr(google_access.gspread, "authorize", lambda creds, session: Client())
    google_access.service_account.reset()
    sa = google_access.service_account
    slow = threading.Thread(target=sa.spreadsheet, args=("slow",))
    slow.start()
    try:
        assert entered.wait(5)
        done = []
        other = threading.Thread(target=lambda: done.append((sa.drive(), sa.spreadsheet("fast"))))
        other.start()
        other.join(2)
        assert done and done[0][1] == "sheet:fast", "a slow open_by_key held up other callers"
    finally:
        release.set()
        slow.join(5)
        google_access.service_account.reset()
        config.set_current(config.FirelessWarConfig())


def test_sheets_client_gets_a_timeout_and_spreadsheets_are_reused(monkeypatch):
    config.set_current(config.FirelessWarConfig(service_account_info=SERVICE_ACCOUNT))
    monkeypatch.setattr(google_access.Credentials, "from_service_account_info", lambda info, scopes: FakeCreds())
    opened, timeouts = [], []

    class HttpClient:
        def set_timeout(self, t):
            timeouts.append(t)

    class Client:
        http_client = HttpClient()

        def open_by_key(self, sid):
            opened.append(sid)
            return f"sheet:{sid}"

    def authorize(creds, session):
        assert session._refresh_timeout == google_access.TOKEN_REFRESH_TIMEOUT_SECONDS
        return Client()

    monkeypatch.setattr(google_access.gspread, "authorize", authorize)
    google_access.service_account.reset()
    try:
        sa = google_access.service_account
        assert sa.spreadsheet("a") == "sheet:a" and sa.spreadsheet("a") == "sheet:a" and sa.spreadsheet("b")
        assert opened == ["a", "b"] and timeouts == [google_access.GOOGLE_TIMEOUT_SECONDS]
    finally:
        google_access.service_account.reset()
        config.set_current(config.FirelessWarConfig())
