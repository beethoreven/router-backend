"""最低可用 App 版本與強制更新的說明(SPEC 第 3 節第 2、5 項)。

- 強制更新只在重大問題、法律衝突或判斷規則修正時使用;畫面要寫明更新原因
  (後台填寫,不用發版就能改)。
- 「需要新版 App」的通知文案與更新連結也在這裡,改文案不用發版。還沒有商店連結時,
  連結可以留空。
"""

from __future__ import annotations

from .textutil import parse_version

FIELDS = ("min_app_version", "force_reason", "update_url", "dataset_notice")
_MAX_TEXT = {"force_reason": 300, "update_url": 500, "dataset_notice": 200}


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
    for key in ("force_reason", "update_url", "dataset_notice"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or len(value) > _MAX_TEXT[key]:
                raise ConfigError(f"{key} 要是 {_MAX_TEXT[key]} 字以內的文字")
            fields[key] = value.strip()
    if "update_url" in fields and fields["update_url"] and not fields["update_url"].startswith("https://"):
        raise ConfigError("更新連結要以 https:// 開頭")
    if not fields:
        return
    assignments = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(f"UPDATE app_config SET {assignments}, updated_at = now(), updated_by = %s WHERE id",
                (*fields.values(), by))
