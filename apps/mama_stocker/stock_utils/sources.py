"""一支函式對一個公開端點：打、解析成簡單的 dict／list、快取。

## 來源（2026-10-04 逐支實測過）

    證交所 OpenAPI      openapi.twse.com.tw       收盤價、殖利率
    證交所行情          mis.twse.com.tw           盤中即時價（上市、上櫃都在這）
    證交所網站          www.twse.com.tw/rwd       ETF 配息、每月均價
    櫃買中心 OpenAPI    www.tpex.org.tw/openapi   收盤價、殖利率、除權息預告
    櫃買中心網站        www.tpex.org.tw/www       除權息結果、每月均價
    櫃買 ETF 訊息中心   info.tpex.org.tw          上櫃 ETF 的發放日
    公開資訊觀測站      mopsov.twse.com.tw        個股的除息日、發放日、金額
    Yahoo 股市（非官方）tw.stock.yahoo.com        即時價的備用，官方都問不到才用

★ mis.twse.com.tw 從 Render 連不到：2026-11-12 起，TCP 連得上，但送出請求後
  對方不回任何 HTTP 就斷線（RemoteDisconnected）；同一時間本機用一模一樣的
  請求正常，其他網站從 Render 也都正常。2026-10-05 從 Render 還連得到。
  即時行情失敗時怎麼退，見 market.price。

## 快取

整包抓下來放記憶體，什麼時候該重抓，看那份資料什麼時候會變：

    收盤價表、殖利率表  表上有日期（_Dated）。已經是最近一次收盤的版本，就一直
                        用到下一次收盤，期間官方也不會變；收盤後官方還沒出新表
                        的那段時間，照 _TTL 重抓，新表出來最慢一小時跟上。
    配息公告            沒有版本日期可看，公司又常在收盤後到傍晚才公告，所以
                        平日早上、晚上各更新一次（_ANNOUNCE_AT）。
    其他（每月均價等）  最多放 _TTL。
    盤中即時價          不快取，每次都是當下的。

★ 過期就當場重抓，抓不到就明確失敗，**不回舊資料**（理由見 _cached）。

## 每一次抓資料都有總期限

★ requests 的 timeout 不是總期限，它只管「多久沒收到任何東西」。對方慢慢吐
  資料時它永遠不會觸發。2026-10-04 部署到 Render 後就是這樣：某一支在開機
  預抓時卡住不回來，握著那份資料的鎖；之後每個請求都排在那把鎖後面，八條
  執行緒全部佔滿，連 /health 都不回應，只能重新部署。

  所以這裡有三道：下載放到另一條執行緒、等不到就放棄（_DEADLINE）；等鎖也有
  期限；剛失敗過的 30 秒內直接回失敗，不要每個請求都再等一次。
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time

import requests

from . import clock

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (mama-stocker)"}
# 連線 4 秒、每次讀取 15 秒。這不是總期限，總期限見 _DEADLINE。
_TIMEOUT = (4, 15)

# 沒有更好的判斷依據時，抓下來的資料放多久。這就是「回出去的數字最多落後官方
# 多久」，不要為了快而調大。
_TTL = 3600

# 配息公告平日在這兩個時刻之後要重抓：開盤前一次，收盤後公司公告完的晚上一次。
_ANNOUNCE_AT = (dt_time(8, 30), dt_time(20, 0))

# 抓一包資料最多等幾秒（整包，不是每次讀取）。
_DEADLINE = 8
# 抓失敗之後多久內不再試。webhook 只有 5 秒，不能每一則訊息都去等一次逾時。
_RETRY_AFTER = 30

_cache: dict[tuple, tuple[float, object]] = {}
_failed: dict[tuple, tuple[float, Exception]] = {}
_locks: dict[tuple, threading.Lock] = {}
# 每一包最近一次抓取的結果，給 stats() 看。
_last: dict[tuple, dict] = {}
_locks_guard = threading.Lock()


def _with_deadline(load, seconds: float):
    """在另一條執行緒跑 load()，超過 seconds 就不等了。

    放棄的那條執行緒還會在背景跑到自己結束（Python 沒辦法從外面殺執行緒），
    但它是 daemon，而且不握任何鎖——卡著的只有它自己。
    """
    box: dict = {}

    def run():
        try:
            box["value"] = load()
        except Exception as exc:  # noqa: BLE001 - 帶回主執行緒再拋
            box["error"] = exc

    t = threading.Thread(target=run, name="source-load", daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"超過 {seconds:g} 秒沒有抓完")
    if "error" in box:
        raise box["error"]
    return box["value"]


# 預抓的執行緒把這個設成 True：資料還剩不到 _REFRESH_AHEAD 秒就過期時，提前重抓。
_ahead = threading.local()
_REFRESH_AHEAD = 900


class refresh_ahead:
    """`with refresh_ahead():` 裡面的讀取會提前更新快過期的資料。

    給 app.py 的預抓用：它每隔一陣子在背景把常用的那幾包重抓一次，使用者問的
    時候資料永遠還在期限內，不必等。這**不是**回舊資料——回出去的東西一樣
    不會超過 _TTL，只是重抓的那幾秒不落在使用者頭上。預抓沒跑到的時候
    （剛開機、背景執行緒沒了），照樣是過期就當場抓。
    """

    def __enter__(self):
        # 記住進來之前的值、離開時還原，巢狀使用時外層才不會被內層關掉。
        self._was = getattr(_ahead, "on", False)
        _ahead.on = True

    def __exit__(self, *exc):
        _ahead.on = self._was


def _cached(key: tuple, ttl: float, load, *, deadline: float | None = None, fresh=None):
    """key 對應的資料：還在期限內就直接回，過期就當場重抓、抓到才回。

    期限預設是抓下來之後 ttl 秒。fresh(抓的時刻, 資料, 提前量) 可以換成別的判斷，
    例如看表上的日期（_dated_fresh）或固定時刻（_announce_fresh）。提前量是預抓
    要提前多少秒把快過期的資料重抓（見 refresh_ahead），使用者的請求是 0。

    ★ **過期的資料一律不回**，抓不到就拋錯（使用者會看到「現在查不到」）。
      慢一點可以，數字一定要對。所以不做「先回舊的、背景重抓」，也不做
      「抓失敗就沿用舊的」：那兩種都會在使用者不知情的情況下給出舊數字。
    """
    ahead = _REFRESH_AHEAD if getattr(_ahead, "on", False) else 0
    if fresh is None:
        def fresh(fetched, value, ahead):
            return time.time() - fetched < ttl - ahead
    deadline = deadline or _DEADLINE
    hit = _cache.get(key)
    if hit and fresh(hit[0], hit[1], ahead):
        return hit[1]
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    # 等鎖也有期限：前一個人最多抓 _DEADLINE 秒，再多就是出事了。
    if not lock.acquire(timeout=deadline + 2):
        raise TimeoutError(f"{key} 等不到前一次抓取結束")
    try:
        hit = _cache.get(key)
        if hit and fresh(hit[0], hit[1], ahead):
            return hit[1]
        failed = _failed.get(key)
        if failed and time.time() - failed[0] < _RETRY_AFTER:
            # 同一個例外物件在冷卻期間會被拋很多次。每次拋都會把當下的呼叫
            # 堆疊接在它的 traceback 後面，不清掉的話會一次比一次長。
            raise failed[1].with_traceback(None)
        started = time.perf_counter()
        try:
            value = _with_deadline(load, deadline)
        except Exception as exc:  # noqa: BLE001 - 記下來再拋，見 _RETRY_AFTER
            _failed[key] = (time.time(), exc)
            _last[key] = {"at": time.time(), "took": round(time.perf_counter() - started, 1),
                          "error": f"{type(exc).__name__}: {exc}"[:200]}
            print(f"[source] {key} 失敗（{time.perf_counter() - started:.1f} s）："
                  f"{type(exc).__name__}: {exc}"[:300], flush=True)
            raise
        took = time.perf_counter() - started
        if took > 2:
            # 平常每一支都在 1 秒內。慢的要看得見——那是逾時的前兆。
            print(f"[source] {key} 抓了 {took:.1f} s", flush=True)
        _failed.pop(key, None)
        _cache[key] = (time.time(), value)
        _last[key] = {"at": time.time(), "took": round(took, 1), "error": None}
        return value
    finally:
        lock.release()


class _Dated(dict):
    """收盤價表、殖利率表：多帶一個 day，表上最新的資料是哪一個交易日。"""
    day: date | None = None


def _dated(value: dict, days) -> _Dated:
    out = _Dated(value)
    out.day = max((d for d in days if d), default=None)
    return out


def _dated_fresh(fetched: float, value, ahead: float) -> bool:
    """表上已經是最近一次收盤的資料，就一直用到下一次收盤：這段時間官方也不會變。

    還不是（收盤後官方還沒出新表、或國定假日）就照 _TTL 重抓。國定假日會被當成
    交易日（見 clock 的交易時間），結果只是那天照 _TTL 多抓幾次。
    """
    day = getattr(value, "day", None)
    if day is not None and day >= clock.last_close(clock.now()):
        return True
    return time.time() - fetched < _TTL - ahead


def _announce_fresh(fetched: float, value, ahead: float) -> bool:
    """平日 _ANNOUNCE_AT 的每個時刻之後要重抓一次。

    預抓會提前 _REFRESH_AHEAD 秒重抓（例如 08:20 抓）。那一份要算是 08:30 這一輪的，
    不然 08:30 一過又會被當成過期，所以時刻往前放寬同樣的秒數。
    """
    now = clock.now() + timedelta(seconds=ahead)
    boundary = clock.last_slot(now, _ANNOUNCE_AT)
    return fetched >= boundary.timestamp() - _REFRESH_AHEAD


def stats() -> dict[str, dict]:
    """每一包資料現在的狀態：幾秒前抓的、那次花多久、最近一次失敗是什麼。"""
    now = time.time()
    out = {}
    for key in sorted(set(_cache) | set(_last), key=str):
        last = _last.get(key, {})
        out["/".join(str(k) for k in key)] = {
            "cached_age": round(now - _cache[key][0]) if key in _cache else None,
            "last_took": last.get("took"),
            "last_error": last.get("error"),
            "last_attempt_age": round(now - last["at"]) if last else None,
        }
    return out


def _get_json(url: str, **params):
    r = requests.get(url, params=params or None, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post_json(url: str, **data):
    r = requests.post(url, data=data, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ── 小工具：這些網站的日期與數字寫法 ─────────────────────────────

def roc_date(s: str | None) -> date | None:
    """民國日期 → date。吃 '1151002'、'115/10/02'、'115年10月02日' 三種寫法。"""
    m = re.search(r"(\d{2,3})\D?(\d{2})\D?(\d{2})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def number(s) -> float | None:
    """'1,505.00' → 1505.0；空白、'-'、'----'、文字一律 None。"""
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ── 全市場：代號、名稱、收盤價 ───────────────────────────────────

def _day(rows: list[dict]):
    """收盤表每一列的 Date 欄位 → date。整張表通常是同一天，同一個字串只解析一次。"""
    seen: dict[str, date | None] = {}

    def of(r: dict) -> date | None:
        s = r.get("Date") or ""
        if s not in seen:
            seen[s] = roc_date(s)
        return seen[s]
    return of


def twse_quotes() -> dict[str, dict]:
    """上市。{代號: {name, close, date}}，含 ETF。date 是 close 屬於哪個交易日。"""
    def load():
        rows = _get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
        day = _day(rows)
        out = {r["Code"].strip(): {"name": r["Name"].strip(), "close": number(r["ClosingPrice"]),
                                   "date": day(r)}
               for r in rows}
        return _dated(out, (v["date"] for v in out.values()))
    return _cached(("twse_quotes",), _TTL, load, fresh=_dated_fresh)


def tpex_quotes() -> dict[str, dict]:
    """上櫃。{代號: {name, close, date}}，含 ETF（也含權證，沒有濾掉——沒人會問）。"""
    def load():
        rows = _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
        day = _day(rows)
        out = {r["SecuritiesCompanyCode"].strip():
               {"name": r["CompanyName"].strip(), "close": number(r["Close"]), "date": day(r)}
               for r in rows}
        return _dated(out, (v["date"] for v in out.values()))
    # 這一包有 4.6 MB（連權證都在裡面），Render 上實測要 6～7 秒，貼著預設的
    # 8 秒期限，所以給它寬一點。
    return _cached(("tpex_quotes",), _TTL, load, deadline=25, fresh=_dated_fresh)


# ── 全市場：官方殖利率（只有個股，ETF 不在裡面）─────────────────

def twse_yields() -> dict[str, float]:
    def load():
        rows = _get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
        out = {r["Code"].strip(): y for r in rows
               if (y := number(r["DividendYield"])) is not None}
        return _dated(out, map(_day(rows), rows))
    return _cached(("twse_yields",), _TTL, load, fresh=_dated_fresh)


def tpex_yields() -> dict[str, float]:
    def load():
        rows = _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis")
        out = {r["SecuritiesCompanyCode"].strip(): y for r in rows
               if (y := number(r["YieldRatio"])) is not None}
        return _dated(out, map(_day(rows), rows))
    return _cached(("tpex_yields",), _TTL, load, fresh=_dated_fresh)


# ── 盤中即時 ─────────────────────────────────────────────────────

def realtime(market: str, code: str) -> dict | None:
    """證交所行情網站自己用的那一支。market 是 'tse' 或 'otc'。不快取。

    回傳 {date, last, prev_close}：
        date        這筆資料是哪個交易日的（休市日會是上一個交易日）
        last        今天最後一筆成交價；今天還沒有成交是 None
        prev_close  昨收

    ★ **不能只看 z**。z 是「這一瞬間的快照剛好是一筆成交」才有值，盤中絕大多數
      時候是 '-'（2026-10-05 開盤時實測：連續十幾次都是 '-'）。最後一筆成交在
      trade 裡面：{"t": "11:48:45", "z": "2565.0000"}。第一版只看 z，z 是 '-'
      就退回昨收，結果開盤第一天就把昨天的收盤價標成「盤中即時價」回出去。

    ★ 同一個查詢字串，行情網站會回快取住的結果，實測可以到一分鐘左右沒變。
      所以這裡的「即時」是一分鐘內，不是逐筆。
    """
    def load():
        return _get_json(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            ex_ch=f"{market}_{code}.tw", json=1, delay=0,
        )
    # 不走 _cached，所以期限自己套。從 Render 連過去偶爾會慢，失敗就再試一次——
    # 這一支問不到，使用者就只能看到「查不到」。
    try:
        data = _with_deadline(load, 4)
    except Exception as exc:  # noqa: BLE001
        print(f"[source] 即時行情 {code} 第一次失敗，重試：{type(exc).__name__}: {exc}"[:200],
              flush=True)
        data = _with_deadline(load, 4)
    rows = data.get("msgArray") or []
    if not rows:
        return None
    r = rows[0]
    d = r.get("d") or ""
    return {
        "date": date(int(d[:4]), int(d[4:6]), int(d[6:8])) if len(d) == 8 else None,
        "last": number(r.get("z")) or number((r.get("trade") or {}).get("z")),
        "prev_close": number(r.get("y")),
    }



def yahoo_quote(market: str, code: str) -> dict | None:
    """Yahoo 股市網頁自己用的那一支，回傳格式跟 realtime 一樣。不快取。

    非官方來源，只在官方的即時行情和收盤表都用不了的時候才問（見 market.price），
    用到它的回覆要標出來。格式跟預期不一樣就當作失敗（ValueError），不猜。
    這支股票不在回應裡回 None。
    """
    symbol = f"{code}.{'TW' if market == 'tse' else 'TWO'}"

    def load():
        # 參數是用分號接在路徑後面，不是 query string。
        r = requests.get(
            f"https://tw.stock.yahoo.com/_td-stock/api/resource/StockServices.stockList;symbols={symbol}",
            headers=_UA, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    rows = _with_deadline(load, 4)
    if not isinstance(rows, list):
        raise ValueError(f"Yahoo 回的不是 list：{type(rows).__name__}")
    row = next((r for r in rows if isinstance(r, dict) and r.get("symbol") == symbol), None)
    if row is None:
        return None
    try:
        last = float(row["price"]["raw"])
        prev = float(row["regularMarketPreviousClose"]["raw"])
        at = datetime.fromisoformat(row["regularMarketTime"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Yahoo {symbol} 的格式不對：{type(exc).__name__}: {exc}"[:200]) from None
    if last <= 0 or prev <= 0 or at.tzinfo is None:
        raise ValueError(f"Yahoo {symbol} 的數字不對：price={last} prev={prev}")
    return {"date": at.astimezone(clock.TAIPEI).date(), "last": last, "prev_close": prev}

# ── 配息：個股（上市、上櫃同一支）────────────────────────────────

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")


def mops_dividends(market: str, year: int) -> dict[str, list[dict]]:
    """公開資訊觀測站「公司股利分派公告資料彙總表」，整個市場一年份。

    market 是 'tse' 或 'otc'，year 是西元年。
    回傳 {代號: [{ex_date, pay_date, cash}]}，只留有配現金、有除息日的那幾筆。

    ★ 這一支回的是網頁表格不是 JSON，欄位靠位置認（2026-10-04 的版面）：
        0 代號  7 盈餘配的現金  8 公積配的現金  9 特別股的現金
        10 除息交易日  11 現金股利發放日
      哪天版面改了，這裡會安靜地解不出東西——所以解出來是空的就當失敗。
    """
    def load():
        r = requests.post(
            "https://mopsov.twse.com.tw/mops/web/ajax_t108sb27",
            data={"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1,
                  "TYPEK": "sii" if market == "tse" else "otc", "year": year - 1911},
            headers=_UA, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        out: dict[str, list[dict]] = {}
        for row in _ROW.findall(r.content.decode("utf-8", "replace")):
            c = [html.unescape(_TAG.sub("", x)).strip() for x in _CELL.findall(row)]
            if len(c) < 12:
                continue
            cash = sum(number(x) or 0 for x in c[7:10])
            ex = roc_date(c[10])
            if cash > 0 and ex:
                out.setdefault(c[0], []).append(
                    {"ex_date": ex, "pay_date": roc_date(c[11]), "cash": cash})
        if not out and year < clock.today().year:
            raise ValueError("公開資訊觀測站的表格解不出任何一筆，版面可能改了")
        return out
    # 去年那一張已經不會再有新公告，放一天；今年的才需要跟著官方。
    # ★ 這一支的期限比別人長很多。公開資訊觀測站回得很慢，而且是對方慢、不是
    #   解析慢（解析只要幾十毫秒）：2026-10-05 中午實測，同一張六百 KB 的表
    #   本機要 3～6 秒，Render 上要 11～20 秒。8 秒的期限會讓「利率」「配息」
    #   整個回「查不到」。平常是預抓在背景付這個時間，使用者不會等到。
    #   （試過只查一家公司，網站不理 co_id，一律回整張表。）
    # 去年的表只剩零星更正，放一天就好；今年的照配息公告的時刻更新。
    if year >= clock.today().year:
        return _cached(("mops", market, year), _TTL, load, deadline=45, fresh=_announce_fresh)
    return _cached(("mops", market, year), 86400, load, deadline=45)


# ── 配息：上市 ETF ───────────────────────────────────────────────

def twse_etf_dividends(code: str, today: date) -> list[dict]:
    """證交所 ETF 專區的收益分配，往回兩年、往後一年。

    cash 是 None 代表「日期公告了、金額還沒」。
    """
    def load():
        data = _get_json(
            "https://www.twse.com.tw/rwd/zh/ETF/etfDiv", stkNo=code, response="json",
            startDate=f"{today.year - 2}0101", endDate=f"{today.year + 1}1231",
        )
        out = []
        for r in data.get("data") or []:
            ex = roc_date(r[2])
            if ex:
                out.append({"ex_date": ex, "pay_date": roc_date(r[4]), "cash": number(r[5])})
        return out
    return _cached(("twse_etf", code), _TTL, load, fresh=_announce_fresh)


# ── 配息：上櫃 ETF（三支拼起來）─────────────────────────────────

def tpex_past_dividends(today: date) -> dict[str, list[dict]]:
    """櫃買「除權除息計算結果表」，全市場過去 400 天。{代號: [{ex_date, cash}]}"""
    def load():
        data = _post_json(
            "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ", response="json",
            startDate=f"{today - timedelta(days=400):%Y/%m/%d}", endDate=f"{today:%Y/%m/%d}",
        )
        out: dict[str, list[dict]] = {}
        for r in data["tables"][0]["data"]:
            ex, cash = roc_date(r[0]), number(r[6])   # 6 是「息值」
            if ex and cash:
                out.setdefault(r[1].strip(), []).append({"ex_date": ex, "cash": cash})
        return out
    return _cached(("tpex_past",), _TTL, load, fresh=_announce_fresh)


def tpex_upcoming_dividends() -> dict[str, list[dict]]:
    """櫃買「除權除息預告表」。{代號: [{ex_date, cash}]}，cash 可能是 None。"""
    def load():
        rows = _get_json("https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost")
        out: dict[str, list[dict]] = {}
        for r in rows:
            ex = roc_date(r["ExRrightsExDividendDate"])
            if ex and "息" in r["ExRrightsExDividend"]:
                out.setdefault(r["SecuritiesCompanyCode"].strip(), []).append(
                    {"ex_date": ex, "cash": number(r["CashDividend"]) or None})
        return out
    return _cached(("tpex_upcoming",), _TTL, load, fresh=_announce_fresh)


def tpex_etf_pay_dates(code: str) -> dict[date, date]:
    """櫃買 ETF 訊息中心的配息行事曆。{除息日: 發放日}

    ★ 只有最近兩次，而且**除息之後才會出現**——即將到來的那一次查不到發放日
      （2026-10-04 實測：00950B 隔天除息，這裡還只列到上個月）。所以上櫃 ETF
      的下次發放日是用這裡的間隔天數推估的，見 market.py。
    """
    def load():
        rows = _post_json("https://info.tpex.org.tw/api/etfExDivPopup",
                          stkNo=code, lang="zh-tw")
        out = {}
        for r in rows:
            ex, pay = roc_date(r.get("divDate")), roc_date(r.get("inDate"))
            if ex and pay:
                out[ex] = pay
        return out
    return _cached(("tpex_etf_pay", code), _TTL, load, fresh=_announce_fresh)


# ── 每月均價（走勢圖用）─────────────────────────────────────────

def monthly_prices(market: str, code: str, year: int) -> dict[int, float]:
    """某一年每個月的平均價。{月: 價}；那一年沒有交易就是空的。

    上市是成交金額 ÷ 成交股數的加權平均，上櫃是收盤價的平均——兩邊官方給的
    算法不同，但畫五年走勢看不出差別。
    """
    def load():
        if market == "tse":
            data = _get_json(
                "https://www.twse.com.tw/rwd/zh/afterTrading/FMSRFK",
                date=f"{year}0101", stockNo=code, response="json",
            )
            rows = data.get("data") or []
        else:
            data = _post_json(
                "https://www.tpex.org.tw/www/zh-tw/statistics/monthlyStock",
                code=code, date=year, response="json",
            )
            tables = data.get("tables") or []
            rows = tables[0].get("data") or [] if tables else []
        return {int(r[1]): p for r in rows if (p := number(r[4]))}
    # 過去的年份不會再變，放一天。
    ttl = _TTL if year >= clock.today().year else 86400
    return _cached(("monthly", market, code, year), ttl, load)
