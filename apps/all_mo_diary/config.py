"""The diary's settings, read once from the ALL_MO_DIARY__* environment variables (via AppSettings).

Modules read settings through `config.get()`, never from os.environ. The value is set by
create_app() before any request, and is also what background threads (the cache loader) read, so
it lives in this module rather than in a request context. One diary app per process, so one value.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from router.settings import AppSettings


@dataclass(frozen=True)
class DiaryConfig:
    database_url: str = field(default="", repr=False)
    google_client_id: str = ""
    session_secret: str = field(default="", repr=False)
    allowed_origins: tuple[str, ...] = ()
    first_admin_email: str = ""
    # Font files go to a GitHub Release: "owner/repo" and a token that can
    # write to that repository only.
    fonts_repo: str = ""
    github_token: str = field(default="", repr=False)
    db_timeout_seconds: int = 20
    cache_retry_seconds: int = 30
    daily_check_limit: int = 10
    feedback_daily_limit: int = 3
    feedback_retention_days: int = 90
    feedback_table_limit_mb: int = 150
    session_idle_days: int = 90
    auth_cache_ttl_seconds: int = 30
    # Tests turn the background cache loader off.
    start_cache_loader: bool = True

    @classmethod
    def from_settings(cls, s: AppSettings) -> "DiaryConfig":
        return cls(
            database_url=s.text("DATABASE_URL"),
            google_client_id=s.text("GOOGLE_CLIENT_ID"),
            session_secret=s.text("SESSION_SECRET"),
            allowed_origins=s.origins(),
            first_admin_email=s.text("FIRST_ADMIN_EMAIL").lower(),
            fonts_repo=s.text("FONTS_REPO"),
            github_token=s.text("GITHUB_TOKEN"),
            db_timeout_seconds=s.int("DB_TIMEOUT_SECONDS", 20),
            cache_retry_seconds=s.int("CACHE_RETRY_SECONDS", 30),
            daily_check_limit=s.int("DAILY_CHECK_LIMIT", 10),
            feedback_daily_limit=s.int("FEEDBACK_DAILY_LIMIT", 3),
            feedback_retention_days=s.int("FEEDBACK_RETENTION_DAYS", 90),
            feedback_table_limit_mb=s.int("FEEDBACK_TABLE_LIMIT_MB", 150),
            session_idle_days=s.int("SESSION_IDLE_DAYS", 90),
            auth_cache_ttl_seconds=s.int("AUTH_CACHE_TTL_SECONDS", 30),
            start_cache_loader=s.text("START_CACHE_LOADER", "1") != "0",
        )


_current = DiaryConfig()


def set_current(cfg: DiaryConfig) -> None:
    global _current
    _current = cfg


def get() -> DiaryConfig:
    return _current
