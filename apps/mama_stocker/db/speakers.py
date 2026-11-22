"""在群組裡說過話或剛加入、還沒開通的人（為什麼要記，見 db/schema.py）。

## 留多久

    開通了            刪（users 表裡有這個 userId，不論啟用或停用）
    90 天沒再出現      刪
    離開群組          刪，但只刪記在那個群組底下的（他可能還在別的群組）
    bot 被移出群組    刪掉記在那個群組底下的所有人
    封鎖 bot          刪，同一個人一天最多刪一次（見 _unfollowed）；之後他在群組裡說話，
                      還是會再被記：那時看得到的就是他在群組裡

前兩種沒有對應的事件，所以每次讀寫這張表都先順手刪一次（_prune）：記人、查名單、處理離開或
封鎖都會。過期的列會留到下一次有人讀寫為止；但查名單一定先刪過，名單上不會出現該刪的人。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from . import connection

# 多久沒再出現就刪。
KEEP_DAYS = 90

# 同一個人在這個行程裡，隔多久才再記一次（更新 seen_at 與名字）。
#
# 群組裡同一個人會講很多句，不必每一句都連一次資料庫。但也不能整個行程只記一次：服務可以連續
# 跑好幾個月不重啟，seen_at 會停在第一次看到的那天，人明明一直在說話，90 天後卻被當成沒出現而
# 刪掉，而且因為這個行程記得「記過了」，要等到重啟才會再記。一天記一次，seen_at 最多差一天。
_RECORD_EVERY = 86400

# userId → 這個行程上次記他的時刻（monotonic）。重新部署後會清空，那時每個人再記一次而已。
# 多條執行緒同時處理 webhook，清舊的時候要走過整份，所以要鎖。
_recorded: dict[str, float] = {}
_recorded_lock = threading.Lock()

# userId → 這個行程上次因為他封鎖 bot 而刪他的時刻（monotonic）。
#
# 封鎖的事件任何加過好友的人都送得出來，而且可以反覆封鎖、解除封鎖。每一次都連資料庫的話，
# 一個人就能讓資料庫一直醒著（Neon 每被叫醒一次至少醒五分鐘，算用量）。所以同一個人一天只為
# 封鎖刪一次：跟「記」一樣，一個沒開通的人一天最多讓資料庫醒一次。
_unfollowed: dict[str, float] = {}


def already_recorded(line_user_id: str) -> bool:
    """這個人在 _RECORD_EVERY 之內記過了。"""
    with _recorded_lock:
        at = _recorded.get(line_user_id)
    return at is not None and time.monotonic() - at < _RECORD_EVERY


def _remember(line_user_id: str) -> None:
    now = time.monotonic()
    with _recorded_lock:
        # 順手清掉超過 _RECORD_EVERY 的：這份記憶只留最近一天出現過的人，不會一直長大。
        for uid in [u for u, at in _recorded.items() if now - at >= _RECORD_EVERY]:
            del _recorded[uid]
        _recorded[line_user_id] = now


def _forget_in_memory(line_user_ids) -> None:
    """資料庫裡刪掉的人，這個行程也要忘掉：不然他再說話時會被當成「今天記過了」而不再記。"""
    with _recorded_lock:
        for uid in line_user_ids:
            _recorded.pop(uid, None)


def _prune(cur) -> None:
    """刪掉開通了的人，以及 KEEP_DAYS 天沒再出現的人（見開頭「留多久」）。"""
    cur.execute("DELETE FROM group_speakers s USING users u WHERE u.line_user_id = s.line_user_id")
    cur.execute("DELETE FROM group_speakers WHERE seen_at < now() - %s * interval '1 day'",
                (KEEP_DAYS,))


def record(line_user_id: str, group_id: str | None, name: str | None, via: str) -> None:
    """via 是 'message'（說了話）或 'joined'（剛加入）。"""
    with connection.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO group_speakers (line_user_id, name, group_id, via)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (line_user_id) DO UPDATE
                   SET name = COALESCE(EXCLUDED.name, group_speakers.name),
                       group_id = EXCLUDED.group_id,
                       seen_at = now()
                """,
                (line_user_id, name, group_id, via),
            )
            # 寫入之後才刪：停用中的人（users 裡有、但不是啟用中）也會走到這裡，剛寫入的
            # 那一列要在同一個交易裡刪掉。
            _prune(cur)
    _remember(line_user_id)


def forget(line_user_ids: list[str], *, group_id: str | None = None) -> None:
    """刪掉這些人。給 group_id 時，只刪記在那個群組底下的（離開一個群組，可能還在別的群組）。"""
    if not line_user_ids:
        return
    with connection.connection() as conn:
        with conn.cursor() as cur:
            _prune(cur)
            if group_id is None:
                cur.execute("DELETE FROM group_speakers WHERE line_user_id = ANY(%s)",
                            (line_user_ids,))
            else:
                cur.execute("DELETE FROM group_speakers WHERE line_user_id = ANY(%s) AND group_id = %s",
                            (line_user_ids, group_id))
    _forget_in_memory(line_user_ids)


def forget_blocker(line_user_id: str) -> None:
    """有人封鎖 bot：刪掉他，但同一個人一天只刪一次（理由見 _unfollowed）。

    刪成功才算數：資料庫連不上時，下一次封鎖會再試。
    """
    now = time.monotonic()
    with _recorded_lock:
        at = _unfollowed.get(line_user_id)
        if at is not None and now - at < _RECORD_EVERY:
            return
    forget([line_user_id])
    with _recorded_lock:
        for uid in [u for u, t in _unfollowed.items() if now - t >= _RECORD_EVERY]:
            del _unfollowed[uid]
        _unfollowed[line_user_id] = now


def forget_group(group_id: str) -> None:
    """bot 被移出這個群組：刪掉記在這個群組底下的所有人。"""
    with connection.connection() as conn:
        with conn.cursor() as cur:
            _prune(cur)
            cur.execute("DELETE FROM group_speakers WHERE group_id = %s RETURNING line_user_id",
                        (group_id,))
            gone = [row[0] for row in cur.fetchall()]
    _forget_in_memory(gone)


def pending() -> list[tuple[str | None, str, str, datetime]]:
    """還沒開通的人，[(顯示名稱, userId, via, 最近一次看到)]，最近看到的排前面。

    ★ 開通的人是查的時候才刪（_prune），不是開通的時候：開通是管理者直接下 SQL，這裡不會知道。
      下面的 NOT EXISTS 在 _prune 之後其實篩不掉任何人，留著是讓這一句自己就是對的。
    """
    with connection.connection() as conn:
        with conn.cursor() as cur:
            _prune(cur)
            cur.execute(
                """
                SELECT s.name, s.line_user_id, s.via, s.seen_at
                  FROM group_speakers s
                 WHERE NOT EXISTS (
                           SELECT 1 FROM users u WHERE u.line_user_id = s.line_user_id)
                 ORDER BY s.seen_at DESC
                 LIMIT 20
                """
            )
            return cur.fetchall()
