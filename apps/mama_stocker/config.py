"""mama-stocker 的設定：從 MAMA_STOCKER__* 環境變數讀一次（經過 AppSettings）。

各模組一律用 `config.get()` 讀設定，不讀 os.environ。create_app() 在任何請求之前設好；
預抓的背景執行緒讀的也是這一份，所以放在模組裡、不放在請求的 context 裡。
一個行程只有一個 mama-stocker app，所以只有一份。

設定都在這裡、在 create_app 裡設好，不在 import 當下讀，所以模組 import 的順序不影響設定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from router.settings import AppSettings


# 試跑鑰匙最短幾個字。用 `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` 產生的是 43 個字。
DRY_RUN_KEY_MIN_LENGTH = 32


@dataclass(frozen=True)
class StockerConfig:
    # 兩個都設才啟用 LINE bot；缺任一個，webhook 與走勢圖回 503。
    # Channel secret 在 LINE Developers 的「Basic settings」，access token 在「Messaging API」最下面。
    line_channel_secret: str = field(default="", repr=False)
    line_channel_access_token: str = field(default="", repr=False)
    line_timeout_seconds: int = 5
    # 試跑（webhook 的 X-Dry-Run，見 app.py）專用的鑰匙，跟 channel secret 分開：開發機上只放這一把，
    # 不放正式的 channel secret。沒設就不能試跑。規則見 dry_run_enabled。
    dry_run_key: str = field(default="", repr=False)
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
            dry_run_key=s.text("DRY_RUN_KEY"),
            database_url=s.text("DATABASE_URL"),
            db_timeout_seconds=s.int("DB_TIMEOUT_SECONDS", 4),
            auth_cache_ttl_seconds=s.int("AUTH_CACHE_TTL_SECONDS", 60),
            start_warm_up=s.text("START_WARM_UP", "1") != "0",
        )

    def line_enabled(self) -> bool:
        return bool(self.line_channel_secret and self.line_channel_access_token)

    def dry_run_enabled(self) -> bool:
        """試跑鑰匙能不能用：至少 DRY_RUN_KEY_MIN_LENGTH 個字，而且不能就是 channel secret。

        太短的猜得出來；跟 channel secret 相同的話，等於又把正式的 secret 放回開發機。
        """
        key = self.dry_run_key
        return len(key) >= DRY_RUN_KEY_MIN_LENGTH and key != self.line_channel_secret


_current = StockerConfig()


def set_current(cfg: StockerConfig) -> None:
    global _current
    _current = cfg


def get() -> StockerConfig:
    return _current
