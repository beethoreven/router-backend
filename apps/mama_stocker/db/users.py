"""誰可以用這個 bot：LINE 帳號綁定在 users 表、而且是啟用中的人。

**誰能用是資料，不是程式**。把 userId 寫死
在程式裡的話，每加一個人就要改 code、部署。用一張表，開通就是一句 SQL
（寫法見 db/schema.py）。

## 快取

這張表只有幾列、幾乎不會變，而每一則訊息都要查。所以整張讀進來放一分鐘
——剛開通或剛停用的人，最慢一分鐘生效。

## 查不到的時候

★ **寧可擋錯，不可放錯**：資料庫沒設定、或連不上而且手上沒有舊名單時，
  當作沒有人有權限。連不上但有舊名單時繼續用舊的——資料庫打個嗝不該讓
  已經開通的人突然不能用。
"""

from __future__ import annotations

import logging
import threading
import time

from .. import config
from . import connection

log = logging.getLogger(__name__)

_lock = threading.Lock()
_allowed: frozenset[str] | None = None
_expires_at = 0.0
# 手上這份名單是什麼時候從資料庫讀到的。資料庫掛著、沿用舊名單時，log 要看得出
# 已經沿用多久了。
_loaded_at = 0.0


def _load() -> frozenset[str]:
    with connection.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT line_user_id FROM users "
                " WHERE line_user_id IS NOT NULL AND status = 'active'"
            )
            return frozenset(row[0] for row in cur.fetchall())


def is_allowed(line_user_id: str | None) -> bool:
    """這個 LINE 帳號能不能用 bot（群組與私訊都看這一支）。"""
    global _allowed, _expires_at, _loaded_at
    if not line_user_id or not connection.enabled():
        return False
    # 整段在鎖裡：同時進來的幾則訊息只需要查一次。握手有 4 秒上限，不會卡死。
    with _lock:
        # 只看期限，不看「手上有沒有名單」：不然剛開機又連不上資料庫時，會因為
        # 名單是空的而每一則訊息都再等一次連線逾時。
        if time.monotonic() >= _expires_at:
            try:
                _allowed = _load()
                _loaded_at = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - 見開頭「查不到的時候」
                if _allowed is None:
                    log.warning("讀取 users 失敗，手上沒有舊名單，所有人都擋：%s", type(exc).__name__)
                else:
                    # ★ 這段期間剛停用的人仍然能用、剛開通的人還不能用——
                    #   要知道這件事正在發生，所以每次沿用都講。
                    log.warning("讀取 users 失敗，沿用 %.0f 秒前的名單：%s",
                                time.monotonic() - _loaded_at, type(exc).__name__)
            # 失敗時也往後延：資料庫掛著的時候，不要每一則訊息都去等一次逾時。
            _expires_at = time.monotonic() + config.get().auth_cache_ttl_seconds
        return _allowed is not None and line_user_id in _allowed
