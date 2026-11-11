"""呼叫 LINE Messaging API。

只有 Reply：回應剛剛那一則訊息。**不計入免費額度**，但 replyToken 有時效
而且只能用一次 → **同步送**，不能丟背景。

## 沒設定就整支停用

缺任一個 LINE 憑證就 enabled() 回 False，呼叫端不必各自判斷。本機開發不會
誤發訊息給真人。
"""

from __future__ import annotations

import logging

import requests

from .. import config

log = logging.getLogger(__name__)

_REPLY_URL = "https://api.line.me/v2/bot/message/reply"


def channel_secret() -> str:
    return config.get().line_channel_secret


def enabled() -> bool:
    """MAMA_STOCKER__LINE_CHANNEL_SECRET 與 MAMA_STOCKER__LINE_CHANNEL_ACCESS_TOKEN 都在才算開。"""
    return config.get().line_enabled()


def _token() -> str:
    return config.get().line_channel_access_token


def reply(reply_token: str, message: str | list[dict]) -> bool:
    """同步回覆。replyToken 只能用一次、而且有時效。

    message 是一句話，或已經組好的 LINE 訊息物件清單（圖片訊息要用後者）。

    回傳有沒有成功。**失敗只記 log 不拋**——這時候拋例外只會讓 LINE 收到
    500 然後重送，而重送會帶著同一個 replyToken（已經用過或已過期），
    第二次一樣失敗。
    """
    if not enabled():
        return False
    messages = [{"type": "text", "text": message}] if isinstance(message, str) else message
    try:
        r = requests.post(
            _REPLY_URL,
            json={"replyToken": reply_token, "messages": messages},
            headers={
                "Authorization": f"Bearer {_token()}",
                "Content-Type": "application/json",
            },
            timeout=config.get().line_timeout_seconds,
        )
        if r.status_code >= 400:
            log.warning("LINE reply 失敗 %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - 回覆失敗不該讓 webhook 回 5xx
        log.warning("LINE reply 例外: %s", type(exc).__name__)
        return False


def group_member_name(group_id: str, user_id: str) -> str | None:
    """群組成員的顯示名稱；查不到就 None。

    ★ getProfile 只對「已加 bot 好友」的人有效，群組裡的人要用這一支。
    """
    if not enabled():
        return None
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}",
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=3,
        )
        return r.json().get("displayName") if r.status_code < 400 else None
    except Exception:  # noqa: BLE001 - 沒有名字還是可以記 id
        return None
