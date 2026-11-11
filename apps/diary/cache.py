"""公用資料的記憶體快取（沿用 mama-fufood 的 services/public_cache.py）：App 設定（最低版本等）與字體清單。

每日檢查只讀這裡,**完全不碰資料庫**。

- **資料庫是唯一的來源,記憶體只是副本。**快取只會整份「從資料庫重新載入」,
  沒有任何地方直接改快取的內容;後台發布時先寫資料庫,成功後才呼叫 reload()。
- **重啟後自動重建:**收到第一個請求時開始在背景載入(不在 import 時開執行緒,原因見 app.py);失敗(例如資料庫還在喚醒)就每 30 秒重試,
  收到檢查請求時也會再試一次(最快 5 秒一次,避免一大串請求同時去叫醒資料庫)。
- **還沒載入完成時:**檢查 API 回「暫時無法檢查」,不扣使用者的每日額度。
"""

from __future__ import annotations

import logging
import threading
import time

from . import app_config, config, db, fonts
from . import textutil as timeutil

log = logging.getLogger(__name__)

_REQUEST_RETRY_MIN_SECONDS = 5

# 整份快照一次換掉(指派是原子的),讀的人永遠拿到完整的一份。
_snapshot: dict | None = None
_load_lock = threading.Lock()
_last_attempt = 0.0
_loader_started = False


def snapshot() -> dict | None:
    return _snapshot


def _read_from_db() -> dict:
    with db.connection() as conn:
        with conn.cursor() as cur:
            release = fonts.latest_release(cur)
            config_row = app_config.get(cur)
    return {"fonts": release, "app_config": config_row, "loaded_at": timeutil.now().isoformat()}


def _reload_locked() -> dict:
    global _snapshot, _last_attempt
    _last_attempt = time.monotonic()
    fresh = _read_from_db()
    _snapshot = fresh
    log.info("公用資料快取已載入：字體清單 %s", fresh["fonts"]["version"] if fresh["fonts"] else "（還沒發布）")
    return fresh


def reload() -> dict:
    """從資料庫重新載入整份快取。失敗就拋例外,舊的快照保持不動。"""
    with _load_lock:
        return _reload_locked()


def ensure_loaded() -> dict | None:
    """檢查請求進來時用:還沒載入就試一次(有最短間隔),失敗回 None。"""
    if _snapshot is not None:
        return _snapshot
    if time.monotonic() - _last_attempt < _REQUEST_RETRY_MIN_SECONDS:
        return None
    if not _load_lock.acquire(blocking=False):
        # 別的請求(或背景重試)正在載入:這次先回「暫時無法檢查」,不排隊等資料庫
        # (請求不能卡在包著網路操作的鎖後面)。
        return None
    try:
        return _reload_locked()
    except Exception as exc:  # noqa: BLE001 - 任何失敗都只代表「現在還沒有快取」
        log.warning("公用資料快取載入失敗：%s", type(exc).__name__)
        return None
    finally:
        _load_lock.release()


def _retry_until_loaded() -> None:
    while _snapshot is None:
        try:
            reload()
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("公用資料快取載入失敗，%s 秒後重試：%s", config.get().cache_retry_seconds, type(exc).__name__)
        time.sleep(config.get().cache_retry_seconds)


def start_background_loader() -> None:
    """啟動時呼叫一次:在背景載入,失敗就每 CACHE_RETRY_SECONDS 秒重試到成功為止。"""
    global _loader_started
    if _loader_started or not config.get().database_url:
        return
    _loader_started = True
    log.info("開始在背景載入公用資料快取")
    threading.Thread(target=_retry_until_loaded, name="public-cache-loader", daemon=True).start()


def status() -> dict:
    snap = _snapshot
    if snap is None:
        return {"loaded": False, "fonts_version": None, "loaded_at": None}
    return {"loaded": True, "loaded_at": snap["loaded_at"],
            "fonts_version": snap["fonts"]["version"] if snap["fonts"] else None}


def reset_for_tests() -> None:
    global _snapshot, _last_attempt, _loader_started
    _snapshot = None
    _last_attempt = 0.0
    _loader_started = False
