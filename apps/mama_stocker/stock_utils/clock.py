"""「現在」與「今天」：整個 mama-stocker 只認台北時間。

實際的時間來自 shared.timeutil；這裡只是 mama-stocker 自己的入口，讓測試可以只換掉 mama-stocker 的「現在」
（替換 clock.now），不影響同一個行程裡的其他 app。

★ 不要用 date.today() 或 datetime.now()：那是主機的時區，Render 上是 UTC。
  台北每天 00:00～08:00，兩者差一天——元旦那八小時，預抓抓的是去年的配息
  公告，使用者問的卻是今年的。
"""

from __future__ import annotations

from datetime import date, datetime

from shared.timeutil import TAIPEI, taipei_now

__all__ = ["TAIPEI", "now", "today"]


def now() -> datetime:
    """台北時間的現在（帶時區）。"""
    return taipei_now()


def today() -> date:
    return now().date()
