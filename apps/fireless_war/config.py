"""fireless-war's settings, read once from the FIRELESS_WAR__* environment variables (via AppSettings).

    FIRELESS_WAR__GOOGLE_CLIENT_ID        OAuth client ID the web page signs in with (token audience)
    FIRELESS_WAR__PERMITTED_USERS         comma-separated GM emails (compared lower-case)
    FIRELESS_WAR__SERVICE_ACCOUNT_JSON    the service account key file's contents (read-only Sheets/Drive)
    FIRELESS_WAR__DRIVE_FOLDER_ID         Drive folder holding the session files
    FIRELESS_WAR__APPS_SCRIPT_URL         Apps Script web app (/exec) that copies the template
    FIRELESS_WAR__APPS_SCRIPT_SECRET      must equal SECRET in apps_script/Code.gs
    FIRELESS_WAR__ALLOWED_ORIGINS         exact web origins, e.g. https://beethoreven.github.io
    FIRELESS_WAR__SPREADSHEET_ID          optional: sheet /round reads when no spreadsheet_id is passed

No defaults are written here: a missing required value makes every request answer 503 not_configured
(see app.py) instead of quietly using a value baked into the code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from router.settings import AppSettings, ConfigError, env_prefix

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)

VALID_DAYS = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "Final"]
VALID_TYPES = ["Morning", "Report"]
# Final has no Morning, only Report (the end-of-game result).
DAYS_WITHOUT_MORNING = ["Final"]

# Must match apps_script/Code.gs: find_record searches Drive for the name Apps Script creates.
RECORD_FILENAME_PREFIX = "FirelessWar_"
DEMO_RECORD_FILENAME_PREFIX = "FirelessWar_Demo_"


@dataclass(frozen=True)
class FirelessWarConfig:
    google_client_id: str = ""
    permitted_users: frozenset[str] = frozenset()
    service_account_info: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    drive_folder_id: str = ""
    apps_script_url: str = field(default="", repr=False)
    apps_script_secret: str = field(default="", repr=False)
    allowed_origins: tuple[str, ...] = ()
    spreadsheet_id: str = ""

    @classmethod
    def from_settings(cls, s: AppSettings) -> "FirelessWarConfig":
        return cls(
            google_client_id=s.text("GOOGLE_CLIENT_ID"),
            permitted_users=frozenset(
                e.strip().lower() for e in s.text("PERMITTED_USERS").split(",") if e.strip()),
            service_account_info=_service_account_info(s),
            drive_folder_id=s.text("DRIVE_FOLDER_ID"),
            apps_script_url=s.text("APPS_SCRIPT_URL"),
            apps_script_secret=s.text("APPS_SCRIPT_SECRET"),
            allowed_origins=s.origins(),
            spreadsheet_id=s.text("SPREADSHEET_ID"),
        )

    def missing(self) -> list[str]:
        """Names of required settings that aren't set (names only, never values)."""
        required = {
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "SERVICE_ACCOUNT_JSON": self.service_account_info,
            "DRIVE_FOLDER_ID": self.drive_folder_id,
            "APPS_SCRIPT_URL": self.apps_script_url,
            "APPS_SCRIPT_SECRET": self.apps_script_secret,
            "ALLOWED_ORIGINS": self.allowed_origins,
        }
        return [f"{env_prefix('fireless-war')}{k}" for k, v in required.items() if not v]


def _service_account_info(s: AppSettings) -> Mapping[str, str]:
    raw = s.text("SERVICE_ACCOUNT_JSON")
    if not raw:
        return MappingProxyType({})
    key = f"{env_prefix(s.app)}SERVICE_ACCOUNT_JSON"
    # `from None` matters: JSONDecodeError keeps the whole input (the private key) on `.doc`, and it
    # must not end up in the exception chain or a traceback. Messages never include the value.
    try:
        info = json.loads(raw)
    except ValueError:
        raise ConfigError(f"{key} is not valid JSON. Paste the whole key file's contents.") from None
    if (not isinstance(info, dict) or info.get("type") != "service_account"
            or not info.get("client_email") or not info.get("private_key")):
        raise ConfigError(f"{key} is not a service account key file.")
    return MappingProxyType(info)


_current = FirelessWarConfig()


def set_current(cfg: FirelessWarConfig) -> None:
    global _current
    _current = cfg


def get() -> FirelessWarConfig:
    return _current
