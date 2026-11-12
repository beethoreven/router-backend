"""即時行情問不到時：先退官方收盤表（日期要是最近一次收盤），再退 Yahoo（回覆要標出來）；
個股利率不跟著失敗。"""

from datetime import date

import pytest

from apps.mama_stocker.line_utils import commands, handler
from apps.mama_stocker.stock_utils import market, sources
from apps.mama_stocker.stock_utils.market import Payout

TSMC = market.Symbol("2330", "台積電", "tse")
ETF = market.Symbol("0056", "高股息", "tse")


def _realtime_down(monkeypatch, yahoo=None):
    """官方即時行情掛掉。yahoo 是備用來源要回的東西；None 表示 Yahoo 也掛了。"""
    def down(market_, code):
        raise ConnectionError("Remote end closed connection without response")
    monkeypatch.setattr(sources, "realtime", down)

    def backup(market_, code):
        if yahoo is None:
            raise ValueError("Yahoo 也掛了")
        return yahoo
    monkeypatch.setattr(sources, "yahoo_quote", backup)


def _table(monkeypatch, day: date, close: float | None = 2390.0):
    rows = {"2330": {"name": "台積電", "close": close, "date": day},
            "0056": {"name": "高股息", "close": 37.5, "date": day}}
    monkeypatch.setattr(sources, "twse_quotes", lambda: rows)
    monkeypatch.setattr(sources, "tpex_quotes", lambda: {})


def _must_not_ask(market_, code):
    raise AssertionError("官方問得到，不該問 Yahoo")


YAHOO_NOW = {"date": date(2026, 11, 12), "last": 2355.0, "prev_close": 2395.0}


# ── 收盤表當備用 ─────────────────────────────────────────────────

def test_after_close_uses_todays_closing_table(at, monkeypatch):
    at(2026, 11, 12, 18, 0)   # 週四
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 11, 12))
    assert market.price(TSMC) == (False, 2390.0, False)


def test_after_close_table_still_yesterday_is_not_used(at, monkeypatch):
    """剛收盤、表還沒更新：表上是昨天的收盤價，不能當成「當前」。"""
    at(2026, 11, 12, 14, 0)
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 11, 11))
    with pytest.raises(ConnectionError):
        market.price(TSMC)


def test_during_market_hours_table_is_never_used(at, monkeypatch):
    at(2026, 11, 12, 10, 0)
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 11, 11))
    with pytest.raises(ConnectionError):
        market.price(TSMC)


@pytest.mark.parametrize("now", [(2026, 11, 14, 12, 0),    # 週六
                                 (2026, 11, 16, 8, 30)])   # 週一開盤前
def test_weekend_and_before_open_use_fridays_table(at, monkeypatch, now):
    at(*now)
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 11, 13))
    assert market.price(TSMC) == (False, 2390.0, False)


def test_holiday_weekday_is_not_found_rather_than_wrong(at, monkeypatch):
    """不查休市行事曆：國定假日的晚上，表停在前一個交易日，日期對不上就查不到。"""
    at(2026, 10, 9, 18, 0)    # 週五，國慶連假
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 10, 8))
    with pytest.raises(ConnectionError):
        market.price(TSMC)


def test_no_close_on_table_raises_original_error(at, monkeypatch):
    at(2026, 11, 12, 18, 0)
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 11, 12), close=None)
    with pytest.raises(ConnectionError):
        market.price(TSMC)


def test_realtime_without_this_code_falls_back_too(at, monkeypatch):
    at(2026, 11, 12, 18, 0)
    _realtime_down(monkeypatch)
    monkeypatch.setattr(sources, "realtime", lambda m, c: None)
    _table(monkeypatch, date(2026, 11, 12))
    assert market.price(TSMC) == (False, 2390.0, False)


def test_realtime_still_wins_when_it_answers(at, monkeypatch):
    at(2026, 11, 12, 10, 0)
    monkeypatch.setattr(sources, "yahoo_quote", _must_not_ask)
    monkeypatch.setattr(sources, "realtime", lambda m, c: {
        "date": date(2026, 11, 12), "last": 2360.0, "prev_close": 2395.0})
    _table(monkeypatch, date(2026, 11, 11))
    assert market.price(TSMC) == (True, 2360.0, False)


def test_closing_tables_keep_the_trading_date(monkeypatch):
    monkeypatch.setattr(sources, "_cache", {})
    monkeypatch.setattr(sources, "_failed", {})
    monkeypatch.setattr(sources, "_get_json", lambda url, **p: [
        {"Date": "1151112", "Code": "2330", "Name": "台積電", "ClosingPrice": "2,390.00"}])
    assert sources.twse_quotes()["2330"] == {
        "name": "台積電", "close": 2390.0, "date": date(2026, 11, 12)}


# ── 利率不跟著現價失敗 ───────────────────────────────────────────

def _rate(text: str):
    kind, payload = commands.parse(text)
    assert kind == commands.RATE
    return handler._answer(kind, payload)


def test_stock_rate_uses_official_yield_without_current_price(at, monkeypatch):
    at(2026, 11, 12, 10, 0)
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 11, 11))
    monkeypatch.setattr(sources, "twse_yields", lambda: {"2330": 0.96})
    monkeypatch.setattr(market, "payouts", lambda sym: [
        Payout(date(2026, 3, 17), date(2026, 4, 9), 5.0),
        Payout(date(2026, 6, 11), date(2026, 7, 9), 5.0),
        Payout(date(2026, 9, 16), date(2026, 10, 8), 5.0)])
    assert _rate("台積電 利率") == "配息方式：三個月配\n預估年利率：0.96%"


def test_etf_rate_needs_current_price_and_says_not_found(at, monkeypatch):
    """ETF 的殖利率要用現價算；沒有現價時不能回 0%（會被讀成不配息）。"""
    at(2026, 11, 12, 10, 0)
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 11, 11))
    monkeypatch.setattr(market, "payouts", lambda sym: [
        Payout(date(2026, 1, 21), date(2026, 2, 11), 1.0),
        Payout(date(2026, 4, 21), date(2026, 5, 11), 1.0)])
    with pytest.raises(LookupError):
        _rate("0056 利率")
    monkeypatch.setattr(handler.users, "is_allowed", lambda sender: True)
    reply = handler.handle_command("0056 利率", sender="Utest", private=True)
    assert reply.startswith("現在查不到資料")


def test_yield_without_current_raises():
    with pytest.raises(LookupError):
        market.yield_percent(ETF, [], None)


# ── Yahoo 當最後的備用 ───────────────────────────────────────────

def test_market_hours_uses_yahoo_and_says_so(at, monkeypatch):
    at(2026, 11, 12, 13, 0)
    _realtime_down(monkeypatch, yahoo=YAHOO_NOW)
    _table(monkeypatch, date(2026, 11, 11))
    assert market.price(TSMC) == (True, 2355.0, True)


def test_official_close_table_comes_before_yahoo(at, monkeypatch):
    at(2026, 11, 12, 18, 0)
    _realtime_down(monkeypatch)
    monkeypatch.setattr(sources, "yahoo_quote", _must_not_ask)
    _table(monkeypatch, date(2026, 11, 12))
    assert market.price(TSMC) == (False, 2390.0, False)


def test_after_close_before_table_updates_uses_yahoo(at, monkeypatch):
    at(2026, 11, 12, 14, 0)
    _realtime_down(monkeypatch, yahoo=YAHOO_NOW)
    _table(monkeypatch, date(2026, 11, 11))
    assert market.price(TSMC) == (False, 2355.0, True)


def test_yahoo_without_this_code_raises_official_error(at, monkeypatch):
    at(2026, 11, 12, 10, 0)
    _realtime_down(monkeypatch)
    monkeypatch.setattr(sources, "yahoo_quote", lambda m, c: None)
    _table(monkeypatch, date(2026, 11, 11))
    with pytest.raises(ConnectionError):
        market.price(TSMC)


def _ask(text: str):
    kind, payload = commands.parse(text)
    return handler._answer(kind, payload)


def test_price_reply_from_yahoo_has_the_note(at, monkeypatch):
    at(2026, 11, 12, 10, 0)
    _realtime_down(monkeypatch, yahoo=YAHOO_NOW)
    _table(monkeypatch, date(2026, 11, 11))
    assert _ask("台積電") == "盤中即時價：2,355\n" + handler.BACKUP_NOTE


def test_price_reply_from_official_has_no_note(at, monkeypatch):
    at(2026, 11, 12, 18, 0)
    _realtime_down(monkeypatch)
    _table(monkeypatch, date(2026, 11, 12))
    assert _ask("台積電") == "當前收盤價：2,390"


def test_stock_rate_with_official_yield_needs_no_note(at, monkeypatch):
    at(2026, 11, 12, 10, 0)
    _realtime_down(monkeypatch, yahoo=YAHOO_NOW)
    _table(monkeypatch, date(2026, 11, 11))
    monkeypatch.setattr(sources, "twse_yields", lambda: {"2330": 0.96})
    monkeypatch.setattr(market, "payouts", lambda sym: [
        Payout(date(2026, 3, 17), date(2026, 4, 9), 5.0),
        Payout(date(2026, 6, 11), date(2026, 7, 9), 5.0),
        Payout(date(2026, 9, 16), date(2026, 10, 8), 5.0)])
    assert _ask("台積電 利率") == "配息方式：三個月配\n預估年利率：0.96%"


def test_etf_rate_from_yahoo_price_has_the_note(at, monkeypatch):
    at(2026, 11, 12, 10, 0)
    _realtime_down(monkeypatch, yahoo={"date": date(2026, 11, 12), "last": 40.0,
                                       "prev_close": 40.5})
    _table(monkeypatch, date(2026, 11, 11))
    monkeypatch.setattr(market, "payouts", lambda sym: [
        Payout(date(2026, 1, 21), date(2026, 2, 11), 1.0),
        Payout(date(2026, 4, 21), date(2026, 5, 11), 1.0),
        Payout(date(2026, 7, 21), date(2026, 8, 11), 1.0),
        Payout(date(2026, 10, 21), date(2026, 11, 11), 1.0)])
    assert _ask("0056 利率") == ("配息方式：三個月配\n預估年利率：10%\n"
                                + handler.BACKUP_NOTE)


def test_dividend_from_yahoo_price_has_the_note(at, monkeypatch):
    at(2026, 11, 12, 10, 0)
    _realtime_down(monkeypatch, yahoo={"date": date(2026, 11, 12), "last": 40.0,
                                       "prev_close": 40.5})
    _table(monkeypatch, date(2026, 11, 11))
    monkeypatch.setattr(market, "payouts", lambda sym: [
        Payout(date(2026, 1, 21), date(2026, 2, 11), 1.0),
        Payout(date(2026, 4, 21), date(2026, 5, 11), 1.0),
        Payout(date(2026, 7, 21), date(2026, 8, 11), 1.0),
        Payout(date(2026, 10, 21), date(2026, 11, 11), 1.0)])
    assert _ask("0056 配息 3張").endswith("\n" + handler.BACKUP_NOTE)


# ── Yahoo 的回應：格式不對就當失敗 ───────────────────────────────

class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def _yahoo_returns(monkeypatch, data, asked=None):
    def get(url, **kw):
        if asked is not None:
            asked.append(url)
        return _Resp(data)
    monkeypatch.setattr(sources.requests, "get", get)


GOOD = {"symbol": "8069.TWO", "price": {"raw": "140"},
        "regularMarketPreviousClose": {"raw": "145"},
        "regularMarketTime": "2026-11-12T05:03:20Z"}


def test_yahoo_parses_price_and_taipei_date(monkeypatch):
    asked = []
    _yahoo_returns(monkeypatch, [GOOD], asked)
    assert sources.yahoo_quote("otc", "8069") == {
        "date": date(2026, 11, 12), "last": 140.0, "prev_close": 145.0}
    assert asked[0].endswith(";symbols=8069.TWO")


def test_yahoo_date_is_taipei_not_utc(monkeypatch):
    _yahoo_returns(monkeypatch, [dict(GOOD, regularMarketTime="2026-11-11T17:00:00Z")])
    assert sources.yahoo_quote("otc", "8069")["date"] == date(2026, 11, 12)


def test_yahoo_missing_symbol_is_none(monkeypatch):
    _yahoo_returns(monkeypatch, [])
    assert sources.yahoo_quote("tse", "9999") is None


@pytest.mark.parametrize("data", [
    {"error": "x"},                                         # 不是 list
    [dict(GOOD, price=None)],                               # 沒有價格
    [dict(GOOD, price={"raw": "-"})],                       # 價格不是數字
    [dict(GOOD, price={"raw": "0"})],                       # 價格是 0
    [dict(GOOD, regularMarketTime="昨天")],                  # 時間看不懂
    [dict(GOOD, regularMarketTime="2026-11-12T05:03:20")],  # 沒有時區
])
def test_yahoo_unexpected_format_is_a_failure(monkeypatch, data):
    _yahoo_returns(monkeypatch, data)
    with pytest.raises(ValueError):
        sources.yahoo_quote("otc", "8069")
