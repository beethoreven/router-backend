"""Minimum app version, the reason shown on the forced-update screen, the update link and an
announcement (one row). Edited in the admin console; changing them needs no app release."""

from __future__ import annotations

from .textutil import parse_version

FIELDS = ("min_app_version", "force_reason", "update_url", "announcement")
_MAX_TEXT = {"force_reason": 300, "update_url": 500, "announcement": 300}


class ConfigError(ValueError):
    pass


def get(cur) -> dict:
    cur.execute(f"SELECT {', '.join(FIELDS)}, updated_at, updated_by FROM app_config WHERE id")
    row = cur.fetchone()
    data = dict(zip(FIELDS, row[:len(FIELDS)]))
    data["updated_at"] = row[len(FIELDS)].isoformat()
    data["updated_by"] = row[len(FIELDS) + 1]
    return data


def update(cur, data: dict, by: str) -> None:
    fields: dict = {}
    if "min_app_version" in data:
        if parse_version(data["min_app_version"]) is None:
            raise ConfigError("最低可用版本要寫成 x.y.z，例如 1.2.0")
        fields["min_app_version"] = data["min_app_version"]
    for key in ("force_reason", "update_url", "announcement"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or len(value) > _MAX_TEXT[key]:
                raise ConfigError(f"{key} 要是 {_MAX_TEXT[key]} 字以內的文字")
            fields[key] = value.strip()
    if fields.get("update_url") and not fields["update_url"].startswith("https://"):
        raise ConfigError("更新連結要以 https:// 開頭")
    if not fields:
        return
    assignments = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(f"UPDATE app_config SET {assignments}, updated_at = now(), updated_by = %s WHERE id",
                (*fields.values(), by))
