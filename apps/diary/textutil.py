"""Small helpers: user-visible character counts, version numbers, time (copied from mama-fufood)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import regex

_VERSION = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,2})$")
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
_STAMP = "%Y%m%d%H%M%S"


def graphemes(text: str) -> int:
    """Characters as people count them: an emoji (even 👨‍👩‍👧) is one. len() would count code points."""
    return len(regex.findall(r"\X", text))


def parse_version(text: object) -> tuple[int, int, int] | None:
    """App version x.y.z → (x, y, z); None when malformed."""
    if not isinstance(text, str):
        return None
    m = _VERSION.match(text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def now() -> datetime:
    """Now in UTC. Tests replace it to simulate crossing midnight."""
    return datetime.now(timezone.utc)


def taipei_today() -> date:
    return now().astimezone(TAIPEI).date()


def taipei_day_start_utc() -> datetime:
    d = taipei_today()
    return datetime(d.year, d.month, d.day, tzinfo=TAIPEI).astimezone(timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_STAMP)


def next_stamp(previous: str | None) -> str:
    """A release version: the publish time (UTC, YYYYMMDDhhmmss), always greater than the previous one."""
    candidate = stamp(now())
    if previous and candidate <= previous:
        return stamp(datetime.strptime(previous, _STAMP).replace(tzinfo=timezone.utc) + timedelta(seconds=1))
    return candidate
