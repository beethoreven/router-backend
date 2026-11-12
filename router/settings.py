"""Each app's settings, read from environment variables that carry the app's prefix.

    DIARY__DATABASE_URL=...      → settings_for("diary").text("DATABASE_URL")
    MAMA_FUFOOD__GOOGLE_CLIENT_ID=... → settings_for("mama-fufood").text("GOOGLE_CLIENT_ID")

Prefix rule: the app name upper-cased, hyphens turned into underscores, followed by two underscores.

★ Apps never read os.environ themselves. Every app used to read a bare DATABASE_URL; in one
  process they would all read the same value. tests/test_isolation.py fails if code under apps/
  touches os.environ / os.getenv.

★ Mistakes stop the service from starting instead of being guessed around: a bare DATABASE_URL, a prefix that matches no app (DAIRY__...),
  two apps pointing at the same database, or a value wrapped in quotes. A wrong guess here means
  one app reading another app's data. On Render a failed start keeps the previous version running.

★ Secrets never appear in error messages or repr: only key names do.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from dotenv import load_dotenv

# Local development reads router-backend/.env; Render sets real environment variables.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_NAME_SHAPE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ORIGIN_SHAPE = re.compile(r"^https?://[^/\s]+$")

# Keys that only make sense for one app. Finding one of these without a prefix means someone
# copied an old single-app .env; there is no right answer to "which app is this for?".
PER_APP_KEYS = (
    "DATABASE_URL",
    "GOOGLE_CLIENT_ID",
    "SESSION_SECRET",
    "ALLOWED_ORIGINS",
    "FIRST_ADMIN_EMAIL",
    "JOB_TOKEN",
    "GITHUB_TOKEN",
    "SERVICE_ACCOUNT_JSON", "APPS_SCRIPT_SECRET", "PERMITTED_USERS",  # fireless-war
    "LINE_CHANNEL_SECRET",
    "LINE_CHANNEL_ACCESS_TOKEN",
)

# Keys that must differ between apps. Two apps on one database would mix their tables.
UNIQUE_KEYS = ("DATABASE_URL", "SESSION_SECRET")


class ConfigError(RuntimeError):
    """A setting is wrong. The message names the variable, never its value."""


def env_prefix(app_name: str) -> str:
    """diary → DIARY__ ; mama-stocker → MAMA_STOCKER__"""
    return app_name.upper().replace("-", "_") + "__"


@dataclass(frozen=True)
class AppSettings:
    """One app's settings with the prefix removed. Built once at startup; read-only."""

    app: str
    _values: Mapping[str, str] = field(repr=False)

    def text(self, key: str, default: str = "") -> str:
        return (self._values.get(key) or default).strip()

    def int(self, key: str, default: int) -> int:
        raw = self.text(key)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{env_prefix(self.app)}{key} must be a whole number.") from None

    def origins(self, key: str = "ALLOWED_ORIGINS") -> tuple[str, ...]:
        """Comma-separated web origins (scheme + host, optional port, no path, no trailing slash)."""
        origins = tuple(o.strip().rstrip("/") for o in self.text(key).split(",") if o.strip())
        for origin in origins:
            if not _ORIGIN_SHAPE.match(origin):
                raise ConfigError(
                    f"{env_prefix(self.app)}{key}: '{origin}' is not an origin. "
                    f"Write it as https://host (port allowed), with no path.")
        return origins


def _check_names(app_names: Iterable[str]) -> list[str]:
    names = list(app_names)
    for name in names:
        if not _NAME_SHAPE.match(name):
            raise ConfigError(f"App name '{name}' must be lowercase letters, digits and single hyphens.")
    prefixes = [env_prefix(n) for n in names]
    if len(set(prefixes)) != len(prefixes):
        raise ConfigError("Two app names map to the same environment-variable prefix.")
    return names


def load_all(app_names: Iterable[str], environ: Mapping[str, str] | None = None) -> dict[str, AppSettings]:
    """Read and check every app's settings. Raises ConfigError on any mistake.

    Pure apart from reading `environ` (defaults to os.environ), so tests pass a dict.
    """
    env = os.environ if environ is None else environ
    names = _check_names(app_names)

    for key in PER_APP_KEYS:
        if env.get(key, "").strip():
            raise ConfigError(
                f"{key} is set without an app prefix. Rename it to <APP>__{key} "
                f"(for example {env_prefix(names[0]) if names else 'DIARY__'}{key}).")

    known = {env_prefix(n): n for n in names}
    values: dict[str, dict[str, str]] = {n: {} for n in names}
    for raw_key, value in env.items():
        if "__" not in raw_key or raw_key.startswith("_"):
            continue
        prefix = raw_key.split("__", 1)[0] + "__"
        key = raw_key[len(prefix):]
        if not key or not prefix[:-2].replace("_", "").isalnum() or not prefix[:-2].isupper():
            continue
        if prefix not in known:
            # Only complain about keys that look like ours, so unrelated tools' variables
            # (e.g. ones set by the platform) don't stop the service.
            if key in PER_APP_KEYS:
                raise ConfigError(f"{raw_key}: the prefix {prefix} matches no app (apps: {', '.join(names)}).")
            continue
        stripped = value.strip()
        if stripped[:1] in ("'", '"') or stripped[-1:] in ("'", '"'):
            # Render keeps the quotes, and a database driver then prints the whole value
            # (password included) in its error. Stop here and name only the key.
            raise ConfigError(f"{raw_key} is wrapped in quotes. Remove them; Render doesn't strip quotes.")
        values[known[prefix]][key] = value

    for key in UNIQUE_KEYS:
        seen: dict[str, str] = {}
        for name in names:
            v = values[name].get(key, "").strip()
            if not v:
                continue
            if v in seen:
                raise ConfigError(
                    f"{env_prefix(seen[v])}{key} and {env_prefix(name)}{key} have the same value. "
                    f"Each app needs its own.")
            seen[v] = name

    return {n: AppSettings(n, MappingProxyType(values[n])) for n in names}
