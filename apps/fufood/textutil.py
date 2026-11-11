"""本 app 自己的版本號規則(字數用 shared.textutil.graphemes)。"""

from __future__ import annotations

import re as _re

_VERSION = _re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d)$")


def parse_version(text: object) -> tuple[int, int, int] | None:
    """App 版本 x.y.z → (x, y, z);格式不對回 None。

    範圍照 SPEC 第 2 節的版本號規則:x、y 最大 99,z 最大 9(Play 版本碼 = x×1000 + y×10 + z)。
    """
    if not isinstance(text, str):
        return None
    m = _VERSION.match(text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
