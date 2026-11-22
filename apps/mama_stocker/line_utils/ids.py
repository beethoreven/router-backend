"""LINE 給的 ID：用之前先確認形狀，形狀不對一律當作沒有。

webhook 裡的 userId、groupId 會拿去組 LINE API 的網址、寫進資料庫、原樣回給使用者。外人偽造
的事件由簽章擋下；channel secret 外洩時簽章就擋不住了，這裡是第二道：網址的路徑、SQL 的值、
回覆的文字，只會出現這個形狀的字串。

    userId   U 加 32 個小寫十六進位字元（LINE 官方文件寫明的格式）
    groupId  C 加 32 個小寫十六進位字元（文件沒有寫格式，範例都是這個形狀）

★ 形狀不對時記一行 warning（只記是哪個欄位，不記值）：LINE 哪天換了格式，這一行是唯一的
  線索——不然症狀只是「群組名單查不到名字、離開的人沒被刪」，看不出原因。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_USER = re.compile(r"U[0-9a-f]{32}")
_GROUP = re.compile(r"C[0-9a-f]{32}")


def _checked(value, shape: re.Pattern, field: str) -> str | None:
    if value is None or value == "":
        # 沒有是正常的：群組裡用電腦版發的訊息不附 userId。
        return None
    if isinstance(value, str) and shape.fullmatch(value):
        return value
    log.warning("webhook 的 %s 形狀不對，當作沒有", field)
    return None


def user_id(value) -> str | None:
    """形狀對的 userId，不對或沒有是 None。"""
    return _checked(value, _USER, "userId")


def group_id(value) -> str | None:
    """形狀對的 groupId，不對或沒有是 None。"""
    return _checked(value, _GROUP, "groupId")
