"""場次檔案(record):檔名怎麼組、去哪裡找、請 Apps Script 建立。

查(GET)與建(POST)刻意分開:要不要建立由前端在 GET 回 404 之後決定。
"""

from __future__ import annotations

import re

from . import config, google_access

# 檔名是 FirelessWar_YYYY_MM_DD_HH_MM;只驗格式,不驗日期是否真實存在(只當唯一識別字串用)
_DATETIME_PATTERN = re.compile(r"^\d{4}_\d{2}_\d{2}_\d{2}_\d{2}$")


def validate_datetime_param(datetime_str: str) -> str:
    if not _DATETIME_PATTERN.match(datetime_str):
        raise ValueError(
            f"datetime 參數格式不正確:{datetime_str},應為 YYYY_MM_DD_HH_MM(例如 2026_07_01_18_30)"
        )
    return datetime_str


def find_record(datetime_str: str, *, demo: bool = False) -> str | None:
    prefix = config.DEMO_RECORD_FILENAME_PREFIX if demo else config.RECORD_FILENAME_PREFIX
    return google_access.find_file_by_name(f"{prefix}{datetime_str}", config.get().drive_folder_id)


def create_record(datetime_str: str, editor_email: str, *, demo: bool = False):
    """(spreadsheet_id, warning)。Apps Script 是同步的:回來時檔案已經建好。"""
    return google_access.create_via_apps_script(datetime_str, editor_email, demo=demo)
