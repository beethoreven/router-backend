"""後台的操作紀錄:哪位管理員、什麼時候、做了什麼。

用途是事後查證(例如公用資料或強制更新被改了,是誰、什麼時候改的)。

- 只記管理員的 Email、時間、動作代碼與內容摘要;不記 IP、不記請求的原文。
- 內容摘要只放已經通過檢查的白名單欄位(和真正寫進資料庫的一致)。
- 個資刪除請求只記刪了幾則,不記被刪的 Email:留下那個 Email,就違背了刪除請求。
- 和那次修改寫在同一個交易裡:修改失敗(丟例外、回滾)就不會留下紀錄。
- 保留 config.audit_retention_days 天;每次寫入時順便清掉更舊的,不需要排程。
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from . import config

# 動作代碼。後台畫面依代碼顯示中文名稱(js/admin.js 的 AUDIT_ACTIONS),新增時兩邊一起加。
ACTIONS = (
    "login", "logout_all",
    "group_create", "group_update", "group_delete",
    "food_create", "food_update", "food_delete",
    "unit_create", "unit_update", "unit_delete",
    "publish", "cache_reload", "app_config_update",
    "feedback_update", "feedback_delete", "feedback_delete_by_email",
    "admin_add", "admin_remove",
)

RECENT_LIMIT = 200


def record(cur, admin_email: str, action: str, detail: dict | None = None) -> None:
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action: {action}")
    cur.execute("DELETE FROM admin_audit WHERE at < now() - make_interval(days => %s)",
                (config.get().audit_retention_days,))
    cur.execute("INSERT INTO admin_audit (admin_email, action, detail) VALUES (%s, %s, %s)",
                (admin_email, action, Jsonb(detail or {})))


def recent(cur, limit: int = RECENT_LIMIT) -> list[dict]:
    """最新的在前面。"""
    cur.execute("SELECT at, admin_email, action, detail FROM admin_audit ORDER BY id DESC LIMIT %s", (limit,))
    return [{"at": at.isoformat(), "email": email, "action": action, "detail": detail}
            for at, email, action, detail in cur.fetchall()]
