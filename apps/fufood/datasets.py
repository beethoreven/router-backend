"""公用資料集(食材清單、單位)的草稿編輯與發布(SPEC 第 3 節「公用資料集規則」)。

- 分組、食材、單位三張表就是**草稿**;按「發布」才把草稿整理成一份內容,寫進
  dataset_releases 並產生新的版本號。App 只會拿到發布過的內容。
- **ID 永遠不可改、不可刪、不可重用**:ID 由資料庫配發;發布過的項目不能刪,只能
  停用(停用的不出現在新增紀錄的選單,舊紀錄照常顯示);還沒發布過的草稿可以刪,
  但 ID 不會再被用到(IDENTITY 序號只往上)。
- 寫入一律走這裡的白名單欄位,不把請求內容直接交給資料庫。
- ★ 發布只寫資料庫;快取由呼叫端在交易成功之後,從資料庫重新載入
  (cache.py)。
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from shared import timeutil
from shared.textutil import graphemes

DATASETS = ("food_list", "units")
# 每個資料集內容的「資料格式版本」。內容的結構改了才升,和 App 的版本號無關
# (SPEC 第 2 節:公用資料集的格式版本另外計算,不影響 x)。
FORMAT_VERSION = {"food_list": 1, "units": 1}

FOOD_NAME_MAX = 3      # 月曆每排放 emoji 加名稱,名稱一律 3 個字以內(SPEC 第 3 節)
GROUP_NAME_MAX = 6
UNIT_NAME_MAX = 4


class EditError(ValueError):
    """管理員的輸入不合規則。訊息會直接顯示在後台。"""


# ── 讀 ────────────────────────────────────────────────────────

def _groups(cur) -> list[dict]:
    cur.execute("SELECT id, name, emoji, sort, disabled, published FROM food_groups ORDER BY sort, id")
    return [dict(zip(("id", "name", "emoji", "sort", "disabled", "published"), r)) for r in cur.fetchall()]


def _foods(cur) -> list[dict]:
    cur.execute(
        "SELECT f.id, f.group_id, f.name, f.emoji, f.sort, f.disabled, f.published"
        "  FROM foods f JOIN food_groups g ON g.id = f.group_id"
        " ORDER BY g.sort, g.id, f.sort, f.id"
    )
    return [dict(zip(("id", "group_id", "name", "emoji", "sort", "disabled", "published"), r))
            for r in cur.fetchall()]


def _units(cur) -> list[dict]:
    cur.execute("SELECT id, name, sort, is_default, disabled, published FROM units ORDER BY sort, id")
    return [dict(zip(("id", "name", "sort", "default", "disabled", "published"), r)) for r in cur.fetchall()]


def draft(cur) -> dict:
    return {"groups": _groups(cur), "foods": _foods(cur), "units": _units(cur)}


def build_content(cur, dataset: str) -> dict:
    """把草稿整理成 App 下載的內容(不含 published 這類後台用的欄位)。"""
    if dataset == "food_list":
        return {
            "groups": [{k: g[k] for k in ("id", "name", "emoji", "sort", "disabled")} for g in _groups(cur)],
            "foods": [{k: f[k] for k in ("id", "group_id", "name", "emoji", "sort", "disabled")}
                      for f in _foods(cur)],
        }
    if dataset == "units":
        return {"units": [{k: u[k] for k in ("id", "name", "sort", "default", "disabled")} for u in _units(cur)]}
    raise EditError(f"沒有這個資料集：{dataset}")


def latest(cur, dataset: str) -> dict | None:
    cur.execute(
        "SELECT version, format_version, content, published_at, published_by FROM dataset_releases"
        " WHERE dataset = %s ORDER BY version DESC LIMIT 1",
        (dataset,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    version, fmt, content, at, by = row
    return {"name": dataset, "version": version, "format_version": fmt, "content": content,
            "published_at": at.isoformat(), "published_by": by}


def history(cur, dataset: str, limit: int = 20) -> list[dict]:
    cur.execute(
        "SELECT version, published_at, published_by FROM dataset_releases WHERE dataset = %s"
        " ORDER BY version DESC LIMIT %s",
        (dataset, limit),
    )
    return [{"version": v, "published_at": a.isoformat(), "published_by": b} for v, a, b in cur.fetchall()]


def has_changes(cur, dataset: str) -> bool:
    current = latest(cur, dataset)
    return current is None or current["content"] != build_content(cur, dataset)


# ── 檢查 ──────────────────────────────────────────────────────

def _text(data: dict, key: str, max_chars: int, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EditError(f"{label}不能空白")
    value = value.strip()
    if graphemes(value) > max_chars:
        raise EditError(f"{label}最多 {max_chars} 個字")
    return value


def _emoji(data: dict) -> str:
    value = data.get("emoji")
    if not isinstance(value, str) or graphemes(value.strip()) != 1:
        raise EditError("圖示要剛好一個 emoji")
    return value.strip()


def _int(data: dict, key: str, label: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000:
        raise EditError(f"{label}要是 0~100000 的整數")
    return value


def _bool(data: dict, key: str, label: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise EditError(f"{label}要是是或否")
    return value


def validate_content(dataset: str, content: dict) -> list[str]:
    """發布前的整份檢查。"""
    errors: list[str] = []
    if dataset == "food_list":
        group_ids = {g["id"] for g in content["groups"]}
        for f in content["foods"]:
            if f["group_id"] not in group_ids:
                errors.append(f"食材「{f['name']}」的分組不存在")
            if not 1 <= graphemes(f["name"]) <= FOOD_NAME_MAX:
                errors.append(f"食材「{f['name']}」的名稱要 1~{FOOD_NAME_MAX} 個字")
    elif dataset == "units":
        defaults = [u for u in content["units"] if u["default"]]
        if len(defaults) != 1:
            errors.append("預設單位要剛好一個")
        elif defaults[0]["disabled"]:
            errors.append("預設單位不能停用")
    return errors


# ── 寫 ────────────────────────────────────────────────────────

def _next_sort(cur, table: str, where: str = "", args: tuple = ()) -> int:
    cur.execute(f"SELECT coalesce(max(sort), 0) + 1 FROM {table} {where}", args)
    return cur.fetchone()[0]


def _require(cur, table: str, item_id: int, label: str) -> dict:
    cur.execute(f"SELECT published FROM {table} WHERE id = %s", (item_id,))
    row = cur.fetchone()
    if row is None:
        raise EditError(f"找不到這個{label}")
    return {"published": row[0]}


def _group_exists(cur, group_id) -> int:
    if isinstance(group_id, bool) or not isinstance(group_id, int):
        raise EditError("請選分組")
    cur.execute("SELECT 1 FROM food_groups WHERE id = %s", (group_id,))
    if cur.fetchone() is None:
        raise EditError("分組不存在")
    return group_id


def create_group(cur, data: dict) -> int:
    name = _text(data, "name", GROUP_NAME_MAX, "分組名稱")
    emoji = _emoji(data)
    sort = _int(data, "sort", "排序序號") if "sort" in data else _next_sort(cur, "food_groups")
    cur.execute("INSERT INTO food_groups (name, emoji, sort) VALUES (%s, %s, %s) RETURNING id", (name, emoji, sort))
    return cur.fetchone()[0]


def update_group(cur, group_id: int, data: dict) -> None:
    _require(cur, "food_groups", group_id, "分組")
    fields: dict = {}
    if "name" in data:
        fields["name"] = _text(data, "name", GROUP_NAME_MAX, "分組名稱")
    if "emoji" in data:
        fields["emoji"] = _emoji(data)
    if "sort" in data:
        fields["sort"] = _int(data, "sort", "排序序號")
    if "disabled" in data:
        fields["disabled"] = _bool(data, "disabled", "停用")
    _update(cur, "food_groups", group_id, fields)


def delete_group(cur, group_id: int) -> None:
    if _require(cur, "food_groups", group_id, "分組")["published"]:
        raise EditError("發布過的分組不能刪除，只能停用")
    cur.execute("SELECT 1 FROM foods WHERE group_id = %s LIMIT 1", (group_id,))
    if cur.fetchone():
        raise EditError("分組裡還有食材，不能刪除")
    cur.execute("DELETE FROM food_groups WHERE id = %s", (group_id,))


def create_food(cur, data: dict) -> int:
    group_id = _group_exists(cur, data.get("group_id"))
    name = _text(data, "name", FOOD_NAME_MAX, "食材名稱")
    emoji = _emoji(data)
    sort = (_int(data, "sort", "排序序號") if "sort" in data
            else _next_sort(cur, "foods", "WHERE group_id = %s", (group_id,)))
    cur.execute("INSERT INTO foods (group_id, name, emoji, sort) VALUES (%s, %s, %s, %s) RETURNING id",
                (group_id, name, emoji, sort))
    return cur.fetchone()[0]


def update_food(cur, food_id: int, data: dict) -> None:
    _require(cur, "foods", food_id, "食材")
    fields: dict = {}
    if "group_id" in data:
        fields["group_id"] = _group_exists(cur, data["group_id"])
    if "name" in data:
        fields["name"] = _text(data, "name", FOOD_NAME_MAX, "食材名稱")
    if "emoji" in data:
        fields["emoji"] = _emoji(data)
    if "sort" in data:
        fields["sort"] = _int(data, "sort", "排序序號")
    if "disabled" in data:
        fields["disabled"] = _bool(data, "disabled", "停用")
    _update(cur, "foods", food_id, fields)


def delete_food(cur, food_id: int) -> None:
    if _require(cur, "foods", food_id, "食材")["published"]:
        raise EditError("發布過的食材不能刪除，只能停用")
    cur.execute("DELETE FROM foods WHERE id = %s", (food_id,))


def create_unit(cur, data: dict) -> int:
    name = _text(data, "name", UNIT_NAME_MAX, "單位名稱")
    sort = _int(data, "sort", "排序序號") if "sort" in data else _next_sort(cur, "units")
    cur.execute("INSERT INTO units (name, sort) VALUES (%s, %s) RETURNING id", (name, sort))
    return cur.fetchone()[0]


def update_unit(cur, unit_id: int, data: dict) -> None:
    _require(cur, "units", unit_id, "單位")
    cur.execute("SELECT is_default, disabled FROM units WHERE id = %s", (unit_id,))
    is_default, disabled = cur.fetchone()
    fields: dict = {}
    if "name" in data:
        fields["name"] = _text(data, "name", UNIT_NAME_MAX, "單位名稱")
    if "sort" in data:
        fields["sort"] = _int(data, "sort", "排序序號")
    if "disabled" in data:
        fields["disabled"] = _bool(data, "disabled", "停用")
        if fields["disabled"] and is_default:
            raise EditError("預設單位不能停用，請先把別的單位設成預設")
    if "default" in data:
        make_default = _bool(data, "default", "預設")
        if make_default and (fields.get("disabled", disabled)):
            raise EditError("停用的單位不能設成預設")
        if not make_default and is_default:
            raise EditError("一定要有一個預設單位，請改把別的單位設成預設")
        if make_default and not is_default:
            # 預設整張表只能有一個:設了新的,舊的自動取消。
            cur.execute("UPDATE units SET is_default = false, updated_at = now() WHERE is_default")
            fields["is_default"] = True
    _update(cur, "units", unit_id, fields)


def delete_unit(cur, unit_id: int) -> None:
    if _require(cur, "units", unit_id, "單位")["published"]:
        raise EditError("發布過的單位不能刪除，只能停用")
    cur.execute("SELECT is_default FROM units WHERE id = %s", (unit_id,))
    if cur.fetchone()[0]:
        raise EditError("預設單位不能刪除")
    cur.execute("DELETE FROM units WHERE id = %s", (unit_id,))


def _update(cur, table: str, item_id: int, fields: dict) -> None:
    if not fields:
        return
    # 欄位名稱只會是上面白名單裡寫死的字串,值一律用參數帶入。
    assignments = ", ".join(f"{name} = %s" for name in fields)
    cur.execute(f"UPDATE {table} SET {assignments}, updated_at = now() WHERE id = %s",
                (*fields.values(), item_id))


def publish(cur, dataset: str, by: str) -> str:
    """發布草稿,回傳新的版本號。內容和上一版完全一樣時不發布。"""
    if dataset not in DATASETS:
        raise EditError(f"沒有這個資料集：{dataset}")
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"mama-fufood:publish:{dataset}",))
    content = build_content(cur, dataset)
    errors = validate_content(dataset, content)
    if errors:
        raise EditError(";".join(errors))
    current = latest(cur, dataset)
    if current is not None and current["content"] == content:
        raise EditError("草稿和目前發布的版本一樣，沒有要發布的修改")
    version = timeutil.next_stamp(current["version"] if current else None)
    cur.execute(
        "INSERT INTO dataset_releases (dataset, version, format_version, content, published_by)"
        " VALUES (%s, %s, %s, %s, %s)",
        (dataset, version, FORMAT_VERSION[dataset], Jsonb(content), by),
    )
    tables = ("food_groups", "foods") if dataset == "food_list" else ("units",)
    for table in tables:
        cur.execute(f"UPDATE {table} SET published = true WHERE NOT published")
    return version
