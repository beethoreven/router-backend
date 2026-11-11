"""mama-stocker 的資料庫（Neon Postgres）連線：shared.database.Database。

    with connection.connection() as conn:
        with conn.cursor() as cur:
            ...

每次呼叫各開一條連線、用完就關，不用連線池（為什麼見 shared/database.py）。代價是每次多一次
握手（同區約 0.2～0.6 秒），所以 Render 與 Neon 要開在同一區；權限查詢也因此有快取
（db/users.py），不是每則訊息都連一次。

數字是照 webhook 的 5 秒預算定的：
    max_connections 3   同時最多三條。這是反壓上限，不是效能旋鈕
    slot_wait       5   等不到名額最多等 5 秒，超過就明確失敗，不無限排隊
    connect_timeout 4   握手不能把 webhook 的 5 秒吃完

建表：Database 在第一次連線時，在 advisory lock 底下跑 schema.ensure_schema 一次。原本只有
行程內的鎖，部署重疊、或切換期間舊服務也還開著時，兩個行程可能同時建表、互相撞上，所以改用跨行程的 advisory lock。
"""

from __future__ import annotations

from shared.database import Database

from .. import config
from . import schema

database = Database(
    "mama-stocker",
    url=lambda: config.get().database_url,
    schema=schema.ensure_schema,
    schema_lock="mama-stocker:schema",
    max_connections=3,
    slot_wait=5,
    connect_timeout=4,
    timeout_seconds=lambda: config.get().db_timeout_seconds,
)


def enabled() -> bool:
    """沒設 DATABASE_URL 時是 False：查不到權限，等於沒有人能用（見 db/users.py）。"""
    return database.configured()


def connection():
    """`with connection.connection() as conn:`。測試可以換掉這一支，證明某條路徑沒有連資料庫。"""
    return database.connection()
