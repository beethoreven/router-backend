"""一次配息的每個階段，下次配息該挑哪一筆（market.next_payout）與怎麼回。"""

from datetime import date

from apps.mama_stocker.line_utils import handler
from apps.mama_stocker.stock_utils import market
from apps.mama_stocker.stock_utils.market import Payout


def test_official_pay_date_not_yet_reached(at):
    at(2026, 11, 10)
    p = Payout(date(2026, 10, 20), date(2026, 11, 15), 1.0)
    assert market.next_payout([p]) == p


def test_official_pay_date_passed_moves_to_next(at):
    at(2026, 11, 10)
    paid = Payout(date(2026, 10, 1), date(2026, 11, 1), 1.0)
    upcoming = Payout(date(2027, 1, 5), None, None)
    assert market.next_payout([paid, upcoming]) == upcoming


def test_ex_date_ahead_without_pay_date(at):
    at(2026, 11, 10)
    p = Payout(date(2026, 11, 20), None, 1.0)
    assert market.next_payout([p]) == p


def test_ex_date_passed_pay_date_not_announced_is_still_next(at):
    """除息日過了、錢還沒發：仍然是下次配息，不能當成已入帳。"""
    at(2026, 11, 10)
    p = Payout(date(2026, 10, 31), None, 1.0)
    assert market.next_payout([p]) == p


def test_waiting_for_pay_date_gives_up_after_30_days(at):
    p = Payout(date(2026, 10, 1), None, 1.0)
    at(2026, 10, 31)
    assert market.next_payout([p]) == p
    at(2026, 11, 1)
    assert market.next_payout([p]) is None


def test_estimated_pay_date_ahead_is_kept(at):
    at(2026, 11, 10)
    p = Payout(date(2026, 10, 25), date(2026, 11, 15), 0.5, pay_estimated=True)
    assert market.next_payout([p]) == p


def test_estimated_pay_date_passed_becomes_not_announced(at):
    """上櫃 ETF 上次隔 26 天、這次隔 28 天，中間那兩天的情況。"""
    at(2026, 11, 10)
    p = Payout(date(2026, 10, 13), date(2026, 11, 8), 0.5, pay_estimated=True)
    got = market.next_payout([p])
    assert got is not None
    assert got.ex_date == p.ex_date and got.cash == 0.5
    assert got.pay_date is None and not got.pay_estimated


def test_reply_for_ex_date_passed_pay_date_not_announced(at):
    at(2026, 11, 10)
    sym = market.Symbol("00950B", "測試債", "otc")
    history = [Payout(date(2026, 8, 13), date(2026, 9, 8), 0.5),
               Payout(date(2026, 10, 13), date(2026, 11, 8), 0.6, pay_estimated=True)]
    text = handler._dividend(sym, ("shares", 1000), 10.0, history, 1, 6.0)
    assert text == ("下次配息日：尚未公告\n"
                    "預期配息金額：600 元\n"
                    "最晚何時前要購入：2026/10/13（已過）")
