"""The one source of "now" (from mama-fufood's timeutil.py, mama-stocker's clock.py and the diary).

- Store and compare in UTC.
- "A day" is a Taipei day (daily limits, midnight reset). Taiwan has no daylight saving: a fixed
  UTC+8 needs no time-zone database.
- ★ Never date.today() / datetime.now() without a zone: that's the host's zone, UTC on Render, and
  between 00:00 and 08:00 Taipei time it is still yesterday there (mama-stocker's lesson).

Tests replace `now` to simulate crossing midnight; everything else goes through it. Replacing it
affects every app in the test process, which is what a test of a day boundary wants.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
_STAMP = "%Y%m%d%H%M%S"


def now() -> datetime:
    """Now, in UTC."""
    return datetime.now(timezone.utc)


def taipei_now() -> datetime:
    return now().astimezone(TAIPEI)


def taipei_today() -> date:
    return taipei_now().date()


def taipei_day_start_utc() -> datetime:
    """Today 00:00 Taipei time, as UTC."""
    d = taipei_today()
    return datetime(d.year, d.month, d.day, tzinfo=TAIPEI).astimezone(timezone.utc)


def stamp(dt: datetime) -> str:
    """A release version: the time in UTC as YYYYMMDDhhmmss, e.g. 20261021153000."""
    return dt.astimezone(timezone.utc).strftime(_STAMP)


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, _STAMP).replace(tzinfo=timezone.utc)


def next_stamp(previous: str | None) -> str:
    """The next release version: now, or the previous one plus a second if now isn't greater
    (two releases in one second, or the clock stepping back), so versions only go up."""
    candidate = stamp(now())
    if previous and candidate <= previous:
        return stamp(parse_stamp(previous) + timedelta(seconds=1))
    return candidate
