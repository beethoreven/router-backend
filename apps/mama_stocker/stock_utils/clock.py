"""「現在」與「今天」：整個 mama-stocker 只認台北時間。

實際的時間來自 shared.timeutil；這裡只是 mama-stocker 自己的入口，讓測試可以只換掉 mama-stocker 的「現在」
（替換 clock.now），不影響同一個行程裡的其他 app。

★ 不要用 date.today() 或 datetime.now()：那是主機的時區，Render 上是 UTC。
  台北每天 00:00～08:00，兩者差一天——元旦那八小時，預抓抓的是去年的配息
  公告，使用者問的卻是今年的。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from shared.timeutil import TAIPEI, taipei_now

__all__ = ["TAIPEI", "now", "today", "OPEN", "CLOSE", "in_session", "last_close",
           "last_slot"]


def now() -> datetime:
    """台北時間的現在（帶時區）。"""
    return taipei_now()


def today() -> date:
    return now().date()


# ── 交易時間 ─────────────────────────────────────────────────────
# 只看星期幾，不查休市行事曆。用到的地方都要能接受「國定假日被當成交易日」：
# 結果只會是多抓一次或查不到，不能是回錯的數字。

OPEN, CLOSE = time(9, 0), time(13, 30)


def _previous_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def in_session(t: datetime) -> bool:
    """t 是不是在盤中（平日 9:00～13:30）。"""
    return t.weekday() < 5 and OPEN <= t.time() < CLOSE


def last_close(t: datetime) -> date:
    """t 之前最近一次收盤是哪一天。盤中是前一個交易日：今天還沒收盤。"""
    d = t.date()
    if d.weekday() < 5 and t.time() >= CLOSE:
        return d
    return _previous_weekday(d)


def last_slot(t: datetime, slots: tuple[time, ...]) -> datetime:
    """平日的 slots（例如 08:30、20:00）裡，t 之前（含）最近的那一個時刻。"""
    d = t.date()
    while True:
        if d.weekday() < 5:
            for s in sorted(slots, reverse=True):
                at = datetime.combine(d, s, tzinfo=t.tzinfo)
                if at <= t:
                    return at
        d -= timedelta(days=1)
