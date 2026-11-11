"""mama-stocker 的設定：從 MAMA_STOCKER__* 環境變數讀一次（經過 AppSettings）。

各模組一律用 `config.get()` 讀設定，不讀 os.environ。create_app() 在任何請求之前設好；
預抓的背景執行緒讀的也是這一份，所以放在模組裡、不放在請求的 context 裡。
一個行程只有一個 mama-stocker app，所以只有一份。

設定都在這裡、在 create_app 裡設好，不在 import 當下讀，所以模組 import 的順序不影響設定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from router.settings import AppSettings


@dataclass(frozen=True)
class StockerConfig:
    # 兩個都設才啟用 LINE bot；缺任一個，webhook 與走勢圖回 503。
    # Channel secret 在 LINE Developers 的「Basic settings」，access token 在「Messaging API」最下面。
    line_channel_secret: str = field(default="", repr=False)
    line_channel_access_token: str = field(default="", repr=False)
    line_timeout_seconds: int = 5
    # 只存誰可以用 bot（users）與群組名單（group_speakers）；股票資料不進資料庫。
    # ★ 沒設的話查不到名單，所有人都會被當成沒有權限。
    database_url: str = field(default="", repr=False)
    # 每一句 SQL 的上限。這裡的查詢都是幾列的小表，webhook 只有 5 秒，卡住的查詢不能把它吃完。
    db_timeout_seconds: int = 4
    # 使用者名單的快取秒數。剛開通或停用的人最慢這麼久生效。
    auth_cache_ttl_seconds: int = 60
    # 測試把背景預抓關掉。
    start_warm_up: bool = True

    @classmethod
    def from_settings(cls, s: AppSettings) -> "StockerConfig":
        return cls(
            line_channel_secret=s.text("LINE_CHANNEL_SECRET"),
            line_channel_access_token=s.text("LINE_CHANNEL_ACCESS_TOKEN"),
            line_timeout_seconds=s.int("LINE_TIMEOUT_SECONDS", 5),
            database_url=s.text("DATABASE_URL"),
            db_timeout_seconds=s.int("DB_TIMEOUT_SECONDS", 4),
            auth_cache_ttl_seconds=s.int("AUTH_CACHE_TTL_SECONDS", 60),
            start_warm_up=s.text("START_WARM_UP", "1") != "0",
        )

    def line_enabled(self) -> bool:
        return bool(self.line_channel_secret and self.line_channel_access_token)


_current = StockerConfig()


def set_current(cfg: StockerConfig) -> None:
    global _current
    _current = cfg


def get() -> StockerConfig:
    return _current
