"""本 app 自己的文字規則:版本號格式,以及哪些字存不進資料庫、寫不進回應(字數用 shared.textutil.graphemes)。"""

from __future__ import annotations

import re as _re

_VERSION = _re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d)$")

# 落單的代理字元:JSON 允許 "\ud800" 這種寫法,解析出來卻不是合法的 UTF-8,
# 寫進資料庫或寫進回應(轉成 UTF-8)時都會失敗。
UNENCODABLE = _re.compile("[\ud800-\udfff]")
# 存不進 Postgres 的字:落單的代理字元,加上 NUL(text 與 jsonb 都不收)。
UNSTORABLE = _re.compile("[\x00\ud800-\udfff]")


def parse_version(text: object) -> tuple[int, int, int] | None:
    """App 版本 x.y.z → (x, y, z);格式不對回 None。

    x、y 最大 99,z 最大 9:Play 的版本碼是 x×1000 + y×10 + z,z 超過 9 就會和下一個 y 撞號。
    """
    if not isinstance(text, str):
        return None
    m = _VERSION.match(text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def contains(value, pattern: _re.Pattern) -> bool:
    """dict 的鍵與值、list 的每一項裡,有沒有符合 pattern 的字。

    用自己的堆疊走訪,不用遞迴:巢狀很深的 JSON 不會讓這裡用完 Python 的遞迴上限。
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if pattern.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False
