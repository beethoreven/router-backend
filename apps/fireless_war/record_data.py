"""場次檔案(record):檔名怎麼組、去哪裡找、請 Apps Script 建立。

查(GET)與建(POST)刻意分開:要不要建立由前端在 GET 回 404 之後決定。
"""

from __future__ import annotations

import re

from . import config, google_access

# 檔名是 FirelessWar_YYYY_MM_DD_HH_MM;只驗格式,不驗日期是否真實存在(只當唯一識別字串用)。
# 用 fullmatch 與 [0-9]:正規表示式的 $ 在結尾換行前也算符合,\d 則接受任何 Unicode 數字(全形、阿拉伯文…)。
_DATETIME_PATTERN = re.compile(r"[0-9]{4}_[0-9]{2}_[0-9]{2}_[0-9]{2}_[0-9]{2}")


def validate_datetime_param(datetime_str: str) -> str:
    if not _DATETIME_PATTERN.fullmatch(datetime_str):
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


def is_demo_record(spreadsheet_id: str) -> bool:
    """這份試算表是不是 demo 場次檔(檔名 FirelessWar_Demo_YYYY_MM_DD_HH_MM)。

    demo 對任何 Google 帳號開放,只能讀它自己建的場次檔,不能拿來讀服務帳號讀得到的其他試算表。
    """
    title = google_access.spreadsheet_title(spreadsheet_id)
    prefix = config.DEMO_RECORD_FILENAME_PREFIX
    return title.startswith(prefix) and _DATETIME_PATTERN.fullmatch(title[len(prefix):]) is not None
