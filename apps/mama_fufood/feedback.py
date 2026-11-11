"""意見回饋(SPEC 第 10 節)。

- 使用者按了送出才會有資料。Email 取自驗證過的登入憑證,是意見回饋裡唯一的
  個人資料;不另存帳號編號,不存 IP。
- 每則意見回饋的編號在手機上產生,重送不會變成兩則、也不會重複扣額度。
- 每人每天最多 3 則(以 Email、台北時間計,直接從資料庫算)。
- 最長保留 90 天;資料表超過上限時不再收附件,並在後台顯示警告。
"""

from __future__ import annotations

from shared import timeutil

from . import config

STATUSES = ("new", "in_progress", "done", "wont_fix")
KINDS = ("suggestion", "problem")
_LIST_COLUMNS = ("id", "received_at", "email", "kind", "description", "app_version", "device_model",
                 "os_version", "status", "attachment_bytes")


def purge_old(cur) -> int:
    cur.execute("DELETE FROM feedback WHERE received_at < now() - make_interval(days => %s)",
                (config.get().feedback_retention_days,))
    return cur.rowcount


def table_bytes(cur) -> int:
    cur.execute("SELECT pg_total_relation_size('feedback')")
    return int(cur.fetchone()[0])


def over_limit(cur) -> bool:
    return table_bytes(cur) >= config.get().feedback_table_limit_mb * 1024 * 1024


def existing_owner(cur, feedback_id: str) -> str | None:
    cur.execute("SELECT email FROM feedback WHERE id = %s", (feedback_id,))
    row = cur.fetchone()
    return row[0] if row else None


def count_today(cur, email: str) -> int:
    cur.execute("SELECT count(*) FROM feedback WHERE email = %s AND received_at >= %s",
                (email, timeutil.taipei_day_start_utc()))
    return int(cur.fetchone()[0])


def insert(cur, row: dict) -> bool:
    """存一則意見回饋。編號已存在就不動,回傳 False。"""
    cur.execute(
        """
        INSERT INTO feedback (id, email, kind, description, device_time, install_id, app_version,
                              build_number, device_model, os_version, snapshot, attachment, attachment_bytes)
        VALUES (%(id)s, %(email)s, %(kind)s, %(description)s, %(device_time)s, %(install_id)s,
                %(app_version)s, %(build_number)s, %(device_model)s, %(os_version)s, %(snapshot)s,
                %(attachment)s, %(attachment_bytes)s)
        ON CONFLICT (id) DO NOTHING
        """,
        row,
    )
    return cur.rowcount == 1


def list_page(cur, filters: dict, page: int, per_page: int = 50) -> dict:
    where, args = [], []
    for key in ("kind", "status", "app_version", "device_model"):
        value = filters.get(key)
        if value:
            where.append(f"{key} = %s")
            args.append(value)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    cur.execute(f"SELECT count(*) FROM feedback {clause}", args)
    total = int(cur.fetchone()[0])
    cur.execute(
        f"SELECT {', '.join(_LIST_COLUMNS)} FROM feedback {clause} ORDER BY received_at DESC LIMIT %s OFFSET %s",
        (*args, per_page, max(page - 1, 0) * per_page),
    )
    items = []
    for r in cur.fetchall():
        item = dict(zip(_LIST_COLUMNS, r))
        item["id"] = str(item["id"])
        item["received_at"] = item["received_at"].isoformat()
        item["description"] = item["description"][:120]
        items.append(item)
    cur.execute("SELECT DISTINCT app_version FROM feedback ORDER BY app_version DESC")
    versions = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT device_model FROM feedback ORDER BY device_model")
    models = [r[0] for r in cur.fetchall()]
    return {"total": total, "page": page, "per_page": per_page, "items": items,
            "app_versions": versions, "device_models": models}


def get(cur, feedback_id: str) -> dict | None:
    cur.execute(
        "SELECT id, received_at, email, kind, description, device_time, install_id, app_version, build_number,"
        "       device_model, os_version, snapshot, attachment, attachment_bytes, status, admin_note, updated_at"
        "  FROM feedback WHERE id = %s",
        (feedback_id,),
    )
    r = cur.fetchone()
    if r is None:
        return None
    keys = ("id", "received_at", "email", "kind", "description", "device_time", "install_id", "app_version",
            "build_number", "device_model", "os_version", "snapshot", "attachment", "attachment_bytes",
            "status", "admin_note", "updated_at")
    item = dict(zip(keys, r))
    item["id"] = str(item["id"])
    item["received_at"] = item["received_at"].isoformat()
    item["updated_at"] = item["updated_at"].isoformat()
    return item


def update(cur, feedback_id: str, data: dict) -> bool:
    fields: dict = {}
    if "status" in data:
        if data["status"] not in STATUSES:
            raise ValueError("處理狀態不對")
        fields["status"] = data["status"]
    if "admin_note" in data:
        if not isinstance(data["admin_note"], str) or len(data["admin_note"]) > 2000:
            raise ValueError("備註要是 2000 字以內的文字")
        fields["admin_note"] = data["admin_note"]
    if not fields:
        return True
    assignments = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(f"UPDATE feedback SET {assignments}, updated_at = now() WHERE id = %s",
                (*fields.values(), feedback_id))
    return cur.rowcount == 1


def delete(cur, feedback_id: str) -> bool:
    cur.execute("DELETE FROM feedback WHERE id = %s", (feedback_id,))
    return cur.rowcount == 1


def delete_by_email(cur, email: str) -> int:
    """個資刪除請求:刪掉這個 Email 的所有意見回饋(SPEC 第 3 節「管理後台功能」)。"""
    cur.execute("DELETE FROM feedback WHERE email = %s", (email,))
    return cur.rowcount
