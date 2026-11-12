"""The diary's version-number rule. Character counts and time live in shared.textutil / shared.timeutil."""

from __future__ import annotations

import re

_VERSION = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,2})$")


def parse_version(text: object) -> tuple[int, int, int] | None:
    """App version x.y.z → (x, y, z); None when malformed."""
    if not isinstance(text, str):
        return None
    m = _VERSION.match(text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
