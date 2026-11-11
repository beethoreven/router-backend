"""The font list (docs/SPEC_DRAFT.md §9, §11): drafts edited in the admin console, published as a
versioned release that the phones download with the daily check.

- Each font: id, display name, sort order, disabled, licensed (a label only — uploads are never
  blocked on it), and its file on the GitHub Release (download URL, sizes, SHA-256s).
- Same rules as mama-fufood's food list: IDs come from an IDENTITY column, never change and are never
  reused; a font that has been published can't be deleted, only disabled (phones may have diaries
  that name it).
- Publishing writes a new font_releases row with the full list (enabled fonts that have a file),
  then the in-memory cache reloads from the database.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from .textutil import graphemes, next_stamp

NAME_MAX = 40
_COLUMNS = ("id", "name", "sort", "disabled", "licensed", "file_name", "download_url", "download_bytes",
            "download_sha256", "font_bytes", "font_sha256", "published", "updated_at")


class FontError(ValueError):
    pass


def _row(r) -> dict:
    item = dict(zip(_COLUMNS, r))
    item["updated_at"] = item["updated_at"].isoformat()
    return item


def list_drafts(cur) -> list[dict]:
    cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM fonts ORDER BY sort, id")
    return [_row(r) for r in cur.fetchall()]


def get(cur, font_id: int) -> dict | None:
    cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM fonts WHERE id = %s", (font_id,))
    r = cur.fetchone()
    return _row(r) if r else None


def _name(value) -> str:
    if not isinstance(value, str) or not value.strip() or graphemes(value.strip()) > NAME_MAX:
        raise FontError(f"字體名稱要是 1～{NAME_MAX} 個字")
    return value.strip()


def create(cur, data: dict) -> int:
    name = _name(data.get("name"))
    sort = data.get("sort", 0)
    if isinstance(sort, bool) or not isinstance(sort, int):
        raise FontError("排序要是整數")
    cur.execute("INSERT INTO fonts (name, sort, licensed) VALUES (%s, %s, %s) RETURNING id",
                (name, sort, bool(data.get("licensed", False))))
    return int(cur.fetchone()[0])


def update(cur, font_id: int, data: dict) -> bool:
    fields: dict = {}
    if "name" in data:
        fields["name"] = _name(data["name"])
    if "sort" in data:
        if isinstance(data["sort"], bool) or not isinstance(data["sort"], int):
            raise FontError("排序要是整數")
        fields["sort"] = data["sort"]
    for key in ("disabled", "licensed"):
        if key in data:
            if not isinstance(data[key], bool):
                raise FontError(f"{key} 要是 true 或 false")
            fields[key] = data[key]
    if not fields:
        return get(cur, font_id) is not None
    assignments = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(f"UPDATE fonts SET {assignments}, updated_at = now() WHERE id = %s", (*fields.values(), font_id))
    return cur.rowcount == 1


def set_file(cur, font_id: int, info: dict) -> bool:
    """Records an uploaded file. A new file always has a new name; the old asset stays on the release
    (phones that installed the old file still verify against its hashes until the next publish)."""
    cur.execute(
        "UPDATE fonts SET file_name = %(file_name)s, download_url = %(download_url)s,"
        " download_bytes = %(download_bytes)s, download_sha256 = %(download_sha256)s,"
        " font_bytes = %(font_bytes)s, font_sha256 = %(font_sha256)s, updated_at = now() WHERE id = %(id)s",
        {**info, "id": font_id},
    )
    return cur.rowcount == 1


def delete(cur, font_id: int) -> None:
    font = get(cur, font_id)
    if font is None:
        raise FontError("找不到這個字體")
    if font["published"]:
        raise FontError("發布過的字體不能刪除，只能停用（手機上可能有日記用了它）")
    cur.execute("DELETE FROM fonts WHERE id = %s", (font_id,))


def latest_release(cur) -> dict | None:
    cur.execute("SELECT version, content FROM font_releases ORDER BY version DESC LIMIT 1")
    r = cur.fetchone()
    return {"version": r[0], "content": r[1]} if r else None


def publish(cur, by: str) -> dict:
    """Publishes every font that has a file. Disabled fonts are listed with disabled = true, so phones
    keep showing their name for diaries that use them but don't offer them for download."""
    fonts = [f for f in list_drafts(cur) if f["download_url"]]
    if not fonts:
        raise FontError("還沒有任何上傳了檔案的字體")
    content = {"fonts": [
        {"id": f["id"], "name": f["name"], "sort": f["sort"], "disabled": f["disabled"], "licensed": f["licensed"],
         "file_name": f["file_name"], "download_url": f["download_url"], "download_bytes": f["download_bytes"],
         "download_sha256": f["download_sha256"], "font_bytes": f["font_bytes"], "font_sha256": f["font_sha256"]}
        for f in fonts
    ]}
    previous = latest_release(cur)
    version = next_stamp(previous["version"] if previous else None)
    cur.execute("INSERT INTO font_releases (version, content, published_by) VALUES (%s, %s, %s)",
                (version, Jsonb(content), by))
    cur.execute("UPDATE fonts SET published = true WHERE id = ANY(%s)", ([f["id"] for f in fonts],))
    return {"version": version, "count": len(fonts)}
