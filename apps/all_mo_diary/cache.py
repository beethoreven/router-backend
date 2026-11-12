"""The diary's public data in memory: app config (minimum version, announcement) and the font list.

The daily check reads only this and **never touches the database**. How it loads, retries and stays
consistent is shared.snapshot_cache; this file only says what goes in it.
"""

from __future__ import annotations

from shared import timeutil
from shared.snapshot_cache import SnapshotCache

from . import app_config, config, db, fonts


def _read_from_db() -> dict:
    with db.connection() as conn:
        with conn.cursor() as cur:
            release = fonts.latest_release(cur)
            config_row = app_config.get(cur)
    return {"fonts": release, "app_config": config_row, "loaded_at": timeutil.now().isoformat()}


public = SnapshotCache(
    "all-mo-diary",
    load=_read_from_db,
    retry_seconds=lambda: config.get().cache_retry_seconds,
    enabled=lambda: bool(config.get().database_url),
)


def status() -> dict:
    snap = public.snapshot()
    if snap is None:
        return {"loaded": False, "fonts_version": None, "loaded_at": None}
    return {"loaded": True, "loaded_at": snap["loaded_at"],
            "fonts_version": snap["fonts"]["version"] if snap["fonts"] else None}
