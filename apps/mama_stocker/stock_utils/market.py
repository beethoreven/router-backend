"""把各個端點的資料拼成「這支股票現在多少錢、怎麼配息、五年怎麼走」。"""

from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
import logging
from statistics import median

from . import clock, sources

log = logging.getLogger(__name__)

_OPEN, _CLOSE = clock.OPEN, clock.CLOSE


def now() -> datetime:
    """台北的現在（見 clock.py）。"""
    return clock.now()


# ── 找股票 ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Symbol:
    code: str
    name: str
    market: str            # 'tse' 上市、'otc' 上櫃

    @property
    def is_etf(self) -> bool:
        # 台灣的 ETF 代號都是 00 開頭（0050、00878、00679B）。
        return self.code.startswith("00")


def _symbol(market: str, code: str, row: dict) -> Symbol:
    return Symbol(code, row["name"], market)


def by_code(code: str) -> Symbol | None:
    code = code.upper()
    for market, table in (("tse", sources.twse_quotes), ("otc", sources.tpex_quotes)):
        row = table().get(code)
        if row:
            return _symbol(market, code, row)
    return None


def by_name(name: str) -> Symbol | None:
    """名稱要**完全符合**。「台積」不會找到台積電：寧可查不到，也不要替使用者猜、
    回成另一支股票。"""
    for market, table in (("tse", sources.twse_quotes), ("otc", sources.tpex_quotes)):
        for code, row in table().items():
            if row["name"] == name:
                return _symbol(market, code, row)
    return None


# ── 現價 ─────────────────────────────────────────────────────────

def price(sym: Symbol) -> tuple[bool, float | None, bool]:
    """(是不是盤中, 價格, 是不是用了非官方的備用來源)。

    盤中 = 現在是 9:00～13:30，而且行情資料的日期就是今天。假日與颱風假
    不必另外查行事曆：那種日子行情資料停在上一個交易日，日期對不上。

    依序問：
      1. 官方即時行情（mis.twse）。
      2. 官方每日收盤表，但**只在表上的日期就是最近一次收盤**的時候（見
         _latest_close）。盤中、或剛收盤表還沒更新，表上是昨天的收盤價，拿來當
         「當前」就錯了，那種時候跳過。
      3. Yahoo 股市（非官方）。回覆裡要標出來，第三個值就是給 handler 判斷的。
    三個都不行就把官方即時行情的錯拋出去。慢一點、查不到都可以，數字一定要對。
    """
    try:
        rt = sources.realtime(sym.market, sym.code)
        if not rt:
            raise LookupError(f"即時行情沒有 {sym.code} 的資料")
    except Exception as exc:  # noqa: BLE001 - 往下退；都退不了就拋這個
        close = _closing_price(sym)
        if close is not None:
            log.warning("即時行情失敗（%s），改用收盤表：%s", sym.code, type(exc).__name__)
            return False, close, False
        try:
            rt = sources.yahoo_quote(sym.market, sym.code)
        except Exception as backup_exc:  # noqa: BLE001
            log.warning("即時行情失敗（%s）：%s；Yahoo 也失敗：%s", sym.code,
                        type(exc).__name__, type(backup_exc).__name__)
            raise exc from None
        if not rt:
            raise exc from None
        log.warning("即時行情失敗（%s），改用 Yahoo：%s", sym.code, type(exc).__name__)
        return (*_from_quote(rt), True)
    return (*_from_quote(rt), False)


def _from_quote(rt: dict) -> tuple[bool, float | None]:
    """即時行情（官方或 Yahoo，格式相同）→ (是不是盤中, 價格)。"""
    t = now()
    trading = rt["date"] == t.date() and _OPEN <= t.time() < _CLOSE
    if rt["last"]:
        # 盤中是最後一筆成交價，收盤後就是收盤價。
        return trading, rt["last"]
    # 今天還沒有任何成交（開盤前、或冷門股開盤後還沒人買賣）：最新的價格
    # 仍然是昨收，所以照「當前收盤價」回——★ 不能標成盤中即時價，那是
    # 2026-10-05 開盤第一天實際發生的錯（見 sources.realtime）。
    return False, rt["prev_close"]


def _latest_close(t: datetime) -> date | None:
    """在 t 這一刻，「最近一次收盤」應該是哪一天；正在開盤中就是 None。

    國定假日那天會算成「今天有收盤」（見 clock 的交易時間），收盤表上卻是
    前一個交易日，日期對不上，結果是查不到，不會回錯的數字。
    """
    return None if clock.in_session(t) else clock.last_close(t)


def _closing_price(sym: Symbol) -> float | None:
    """每日收盤表上的收盤價；表上的日期不是最近一次收盤就是 None。"""
    latest = _latest_close(now())
    if latest is None:
        return None
    table = sources.twse_quotes if sym.market == "tse" else sources.tpex_quotes
    row = table().get(sym.code)
    if not row or row.get("date") != latest:
        return None
    return row.get("close")


# ── 配息 ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Payout:
    ex_date: date              # 除息日：這天之前買進才領得到
    pay_date: date | None      # 發放日：錢入帳的那天
    cash: float | None         # 每股（每單位）配多少；None = 金額還沒公告
    pay_estimated: bool = False  # 發放日是推估的，不是官方公告的


def payouts(sym: Symbol) -> list[Payout]:
    """這支股票過去一兩年與已公告的配息，照除息日由舊到新。"""
    today = now().date()
    if not sym.is_etf:
        rows = []
        for year in (today.year - 1, today.year):
            rows += sources.mops_dividends(sym.market, year).get(sym.code, [])
        found = [Payout(r["ex_date"], r["pay_date"], r["cash"]) for r in rows]
    elif sym.market == "tse":
        found = [Payout(r["ex_date"], r["pay_date"], r["cash"])
                 for r in sources.twse_etf_dividends(sym.code, today)]
    else:
        found = _tpex_etf_payouts(sym.code, today)
    # 同一次配息可能公告兩次（更正），留後面那一筆。
    return sorted({p.ex_date: p for p in found}.values(), key=lambda p: p.ex_date)


def _tpex_etf_payouts(code: str, today: date) -> list[Payout]:
    """上櫃 ETF：過去的配息、預告表、發放日是三個地方的資料。

    發放日官方只在除息之後才公布（見 sources.tpex_etf_pay_dates）。同一檔
    ETF 從除息到發放隔幾天很固定（實測 00679B 兩次都是 21 天、00950B 是
    26 與 28 天），所以還沒公布的就用上一次的間隔推估，並標成推估。
    """
    by_ex = {r["ex_date"]: r["cash"]
             for r in sources.tpex_past_dividends(today).get(code, [])}
    for r in sources.tpex_upcoming_dividends().get(code, []):
        by_ex.setdefault(r["ex_date"], r["cash"])
    pay_dates = sources.tpex_etf_pay_dates(code)
    gap = None
    if pay_dates:
        last_ex = max(pay_dates)
        gap = pay_dates[last_ex] - last_ex
    out = []
    for ex, cash in by_ex.items():
        if ex in pay_dates:
            out.append(Payout(ex, pay_dates[ex], cash))
        elif gap is not None:
            out.append(Payout(ex, ex + gap, cash, pay_estimated=True))
        else:
            out.append(Payout(ex, None, cash))
    return out


def period_months(history: list[Payout]) -> int | None:
    """幾個月配一次：1、3、6、12；完全沒有配息紀錄是 None。

    看的是相鄰兩次除息日隔多久，取中位數——不是數一年配幾次。剛上市半年的
    月配 ETF 只配過五次，用數的會被當成季配。只有一筆紀錄時當年配。
    """
    if not history:
        return None
    gaps = [(b.ex_date - a.ex_date).days for a, b in zip(history, history[1:])]
    if not gaps:
        return 12
    g = median(gaps)
    return 1 if g < 45 else 3 if g < 135 else 6 if g < 270 else 12


def official_yield(sym: Symbol) -> float | None:
    """官方表上的殖利率；ETF 沒有這個欄位，個股不在表上也是 None。"""
    if sym.is_etf:
        return None
    table = sources.twse_yields if sym.market == "tse" else sources.tpex_yields
    return table().get(sym.code)


def yield_percent(sym: Symbol, history: list[Payout], current: float | None) -> float:
    """殖利率（%）。

    個股用官方的數字（最近一個年度的現金股利 ÷ 收盤價）。
    ETF 官方沒有這個欄位（2026-10-04 查過證交所、櫃買、e添富、ETF 訊息中心），
    所以自己算：過去 365 天實際配的現金 ÷ 現價——Yahoo、CMoney 也是這個算法。
    個股在官方表裡找不到時（例如剛上市）也退回這個算法。
    """
    official = official_yield(sym)
    if official is not None:
        return official
    if not current:
        # 沒有現價就算不出來。回 0% 會被讀成「這支不配息」，寧可查不到。
        raise LookupError(f"{sym.code} 沒有現價，算不出殖利率")
    today = now().date()
    paid = sum(p.cash or 0 for p in history
               if today - timedelta(days=365) < p.ex_date <= today)
    return paid / current * 100


# 除息之後，最多等官方公布發放日幾天。超過就當作已入帳（為什麼要有上限，見 next_payout）。
_PAY_DATE_WAIT_DAYS = 30


def next_payout(history: list[Payout]) -> Payout | None:
    """已經公告、錢還沒入帳的那一次；沒有就是 None。

    ★ 看的是發放日，不是除息日：除息日過了但錢還沒發的那一次，對手上有這支
      股票的人來說仍然是「下次配息」。

    一次配息走過的階段，與這裡怎麼判斷：

        發放日是官方公告的   發放日還沒到 → 是它；過了 → 已入帳，看下一筆
        除息日還沒到         是它（發放日沒公告、或是推估的都一樣）
        已除息，發放日是推估的、還沒到
                             是它，照推估的日子回
        已除息，發放日沒公告（或推估的日子已經過了、官方還沒公布）
                             除息後 _PAY_DATE_WAIT_DAYS 天內仍然是它，發放日
                             當成「尚未公告」；超過就當作已入帳，看下一筆

    ★ 最後那一種不能當成已入帳跳過：上櫃 ETF 可能這次比上次晚兩天發放，那兩天
      裡問「配息」，手上那筆還沒入帳的錢就會不見。要有天數上限，是因為官方的
      表格可能永遠不補發放日，沒有上限的話那一筆會永遠卡在「下次配息」。
    """
    today = now().date()
    for p in history:
        if p.pay_date and not p.pay_estimated:
            if p.pay_date >= today:
                return p
            continue
        if p.ex_date >= today or (p.pay_date and p.pay_date >= today):
            return p
        if (today - p.ex_date).days <= _PAY_DATE_WAIT_DAYS:
            # 推估的日子已經過了也不再拿出來講——那是個沒發生的日子。
            return replace(p, pay_date=None, pay_estimated=False)
    return None


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def guess_next_pay_date(history: list[Payout], months: int) -> date | None:
    """還沒公告時猜下一次發放日：上一次的發放日往後推一個週期。

    推出來的日子已經過了就再推一個週期——公司比往年晚公告時，不該回一個
    過去的日期。
    """
    past = [p for p in history if p.pay_date or p.ex_date]
    if not past:
        return None
    last = past[-1]
    guess = add_months(last.pay_date or last.ex_date, months)
    today = now().date()
    while guess < today:
        guess = add_months(guess, months)
    return guess


# ── 五年走勢 ─────────────────────────────────────────────────────

def five_year_monthly(sym: Symbol) -> list[tuple[int, int, float | None]]:
    """最近 60 個月的每月均價，[(年, 月, 價)]，由舊到新。還沒上市的月份價是 None。

    ★ 股價沒有還原：遇到分割（例如 0050 在 2025 年一股拆四股）圖上會是一個
      斷崖。官方的每月均價就是當時的實際成交價。
    """
    today = now().date()
    other = "otc" if sym.market == "tse" else "tse"
    years = list(range(today.year - 5, today.year + 1))
    # ★ 六年一起問，不要一年一年排隊：證交所一支要 0.7 秒左右，排隊就是四五秒，
    #   而這段時間 LINE 正等著抓圖。實測六支同時打約 1 秒，沒有被擋。
    by_year = _monthly_many(sym.market, sym.code, years)
    # 這幾年內從上櫃轉上市（或反過來）的股票，轉之前的資料在另一邊。
    empty = [y for y in years if not by_year.get(y)]
    if empty:
        by_year.update(_monthly_many(other, sym.code, empty))
    out = []
    for i in range(59, -1, -1):
        y, m = divmod(today.year * 12 + today.month - 1 - i, 12)
        out.append((y, m + 1, by_year.get(y, {}).get(m + 1)))
    return out


# 一批每月均價最多等幾秒。
_MONTHLY_DEADLINE = 10


def _monthly_many(market: str, code: str, years: list[int]) -> dict[int, dict[int, float]]:
    """同時問好幾年。**到時間就不等了**，而且只要有一年沒問到就整個失敗。

    ★ 不能把沒問到的那一年當成「沒有資料」：圖上沒有資料的月份會被畫成
      「尚未上市」，那等於把連線失敗畫成一個錯的事實。寧可這張圖出不來。

    ★ 不能用 `with ThreadPoolExecutor()`：它離開時會等每一支都結束，其中一支
      不回來就整個請求陪它等。
    """
    pool = ThreadPoolExecutor(len(years))
    futures = {pool.submit(sources.monthly_prices, market, code, y): y for y in years}
    done, late = wait(futures, timeout=_MONTHLY_DEADLINE)
    pool.shutdown(wait=False, cancel_futures=True)
    if late:
        raise TimeoutError(
            f"{code} {market} 有 {len(late)} 年超過 {_MONTHLY_DEADLINE} 秒沒回來："
            f"{sorted(futures[f] for f in late)}")
    return {futures[f]: f.result() for f in done}
