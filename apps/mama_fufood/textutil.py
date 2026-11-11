"""本 app 自己的版本號規則(字數用 shared.textutil.graphemes)。"""

from __future__ import annotations

import re as _re

_VERSION = _re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d)$")


def parse_version(text: object) -> tuple[int, int, int] | None:
    """App 版本 x.y.z → (x, y, z);格式不對回 None。

    x、y 最大 99,z 最大 9:Play 的版本碼是 x×1000 + y×10 + z,z 超過 9 就會和下一個 y 撞號。
    """
    if not isinstance(text, str):
        return None
    m = _VERSION.match(text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
