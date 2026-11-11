"""公用資料的記憶體快取:已發布的公用資料集與 App 版本設定。

每日檢查只讀這裡,**完全不碰資料庫**。怎麼載入、重試、保持一致由 shared.snapshot_cache
處理(資料庫是唯一的來源,記憶體只是副本;後台發布時先寫資料庫,成功後才 reload());
這裡只決定裝什麼。還沒載入完成時,檢查 API 回「暫時無法檢查」,不扣使用者的每日額度。
"""

from __future__ import annotations

from shared import timeutil
from shared.snapshot_cache import SnapshotCache

from . import app_config, config, datasets, db


def _read_from_db() -> dict:
    with db.connection() as conn:
        with conn.cursor() as cur:
            releases = {name: datasets.latest(cur, name) for name in datasets.DATASETS}
            config_row = app_config.get(cur)
    return {
        "datasets": {name: r for name, r in releases.items() if r is not None},
        "app_config": config_row,
        "loaded_at": timeutil.now().isoformat(),
    }


public = SnapshotCache(
    "mama-fufood",
    load=_read_from_db,
    retry_seconds=lambda: config.get().cache_retry_seconds,
    enabled=lambda: bool(config.get().database_url),
)


def status() -> dict:
    snap = public.snapshot()
    if snap is None:
        return {"loaded": False, "datasets": {}, "loaded_at": None}
    return {"loaded": True, "loaded_at": snap["loaded_at"],
            "datasets": {k: v["version"] for k, v in snap["datasets"].items()}}
