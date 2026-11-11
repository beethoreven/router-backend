"""Talking to Google: Sheets contents, Drive file lookups, and the Apps Script that creates files.

Two paths on purpose:
- Reading (Sheets values, Drive file search) uses the service account, read-only scopes.
- Creating a session file goes through the Apps Script web app, which runs as the owner's Google
  account: a service account has no Drive storage quota and can't own new files (storageQuotaExceeded).
  Calling Drive directly with OAuth doesn't work either: files granted through the Picker were not
  readable afterwards (files.get failed).

★ Every outside call has a timeout: the whole router shares 16 threads. That includes the service
  account's token refresh (google-auth's default is 120 s).
★ Nothing connects at import or in create_app: credentials and sessions are built on first use.
"""

from __future__ import annotations

import threading

import gspread
import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from . import config

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
GOOGLE_TIMEOUT_SECONDS = 15
TOKEN_REFRESH_TIMEOUT_SECONDS = 10
# Copying the template is the slowest call; Apps Script answers only once the copy exists.
APPS_SCRIPT_TIMEOUT_SECONDS = 30


class ServiceAccount:
    """Lazily built service-account credentials, gspread client and Drive session (one set per process)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self._credentials = None
        self._sheets = None
        self._drive = None
        self._spreadsheets: dict[str, gspread.Spreadsheet] = {}

    # ★ The lock only guards building these objects in memory (no network: credentials refresh on
    #   their first request, outside it). open_by_key fetches from Google, so it runs unlocked; two
    #   threads may both open the same sheet once, and setdefault keeps the first.

    def _creds(self) -> Credentials:
        if self._credentials is None:
            self._credentials = Credentials.from_service_account_info(
                dict(config.get().service_account_info), scopes=list(config.SCOPES))
        return self._credentials

    def _session(self) -> AuthorizedSession:
        return AuthorizedSession(self._creds(), refresh_timeout=TOKEN_REFRESH_TIMEOUT_SECONDS)

    def drive(self) -> AuthorizedSession:
        with self._lock:
            if self._drive is None:
                self._drive = self._session()
            return self._drive

    def _sheets_client(self) -> gspread.Client:
        with self._lock:
            if self._sheets is None:
                client = gspread.authorize(self._creds(), session=self._session())
                client.http_client.set_timeout(GOOGLE_TIMEOUT_SECONDS)
                self._sheets = client
            return self._sheets

    def spreadsheet(self, spreadsheet_id: str) -> gspread.Spreadsheet:
        cached = self._spreadsheets.get(spreadsheet_id)
        if cached is not None:
            return cached
        opened = self._sheets_client().open_by_key(spreadsheet_id)
        return self._spreadsheets.setdefault(spreadsheet_id, opened)


service_account = ServiceAccount()


def sheet_batch_read(ranges: list[str], spreadsheet_id: str) -> list[list]:
    """Read several A1 ranges (any tabs) in one Sheets API call; one 2-D list per range, in order.

    The quota is 60 reads a minute per service account, shared by every game, so callers combine
    everything they need into one call.
    """
    result = service_account.spreadsheet(spreadsheet_id).values_batch_get(ranges)
    return [vr.get("values", []) for vr in result.get("valueRanges", [])]


def _escape_query_value(value: str) -> str:
    """Escape a value placed inside a Drive query's single quotes (same idea as SQL injection)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_file_by_name(name: str, folder_id: str) -> str | None:
    """The id of the first non-trashed file with this exact name in the folder, or None."""
    query = (
        f"name = '{_escape_query_value(name)}' "
        f"and '{_escape_query_value(folder_id)}' in parents "
        f"and trashed = false"
    )
    resp = service_account.drive().get(
        f"{DRIVE_API_BASE}/files",
        params={"q": query, "fields": "files(id, name)"},
        timeout=GOOGLE_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0]["id"] if files else None


class AppsScriptError(RuntimeError):
    """Apps Script answered but said the copy failed (its message is meant for people)."""


def create_via_apps_script(datetime_str: str, editor_email: str | None, *, demo: bool = False):
    """Ask Apps Script to copy the template (or FirelessWar_Demo) and add editor_email as an editor.

    Returns (spreadsheet_id, warning); warning is set when the copy worked but adding the editor didn't.
    Apps Script always answers HTTP 200 (ContentService can't set a status), so success is the
    statusCode field in the body. It redirects (302) to the result; requests follows it as a GET.
    """
    cfg = config.get()
    body = {"secret": cfg.apps_script_secret, "datetime": datetime_str, "editor_email": editor_email}
    if demo:
        body["mode"] = "demo"
    resp = requests.post(cfg.apps_script_url, json=body, timeout=APPS_SCRIPT_TIMEOUT_SECONDS)
    resp.raise_for_status()
    result = resp.json()
    if result.get("statusCode") != 200:
        raise AppsScriptError(result.get("error") or "Apps Script 回傳未知錯誤")
    return result["spreadsheet_id"], result.get("warning")
