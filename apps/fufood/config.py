"""媽媽記副食品的設定,只在啟動時從 FUFOOD__* 環境變數讀一次(經由 AppSettings)。

其他模組一律用 `config.get()` 取值,不讀 os.environ。create_app() 在第一個請求之前設好,
背景執行緒(公用資料快取的載入)讀的也是這一份,所以放在模組裡而不是請求裡。
一個行程只有一個 fufood app,所以只有一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from router.settings import AppSettings


@dataclass(frozen=True)
class FufoodConfig:
    # ★ 沒有預設值也沒有備援:沒設就是連不上,不會退回任何本機資料庫。
    database_url: str = field(default="", repr=False)
    # Google Cloud 的「網頁應用程式」用戶端 ID。後台網頁登入用它;App 登入時帶的
    # serverClientId 也是它,所以 App 送來的 ID token 的 aud 一樣是這個值。
    google_client_id: str = ""
    # App 通行證與後台 session 的簽章金鑰。換掉它,所有人都要重新登入。
    session_secret: str = field(default="", repr=False)
    # 允許呼叫後台 API 的網頁來源(只有後台需要跨來源)。
    allowed_origins: tuple[str, ...] = ()
    # 第一位管理員。其他管理員在後台的「管理員」頁設定。
    first_admin_email: str = ""
    # 資料庫一句查詢最多等幾秒。
    db_timeout_seconds: int = 20
    # 公用資料快取載入失敗時,隔幾秒再試(SPEC 第 3 節第 2 項:每 30 秒重試)。
    cache_retry_seconds: int = 30
    # 每人每天的上限(SPEC 第 3、4、10 節;「一天」以台北時間計)。
    daily_check_limit: int = 10
    convert_daily_limit: int = 5
    feedback_daily_limit: int = 3
    feedback_retention_days: int = 90
    # 意見回饋資料表超過這個大小就不再收附件(記錄檔),並在後台顯示警告。
    feedback_table_limit_mb: int = 150
    session_idle_days: int = 90
    # 「這張 session 是哪個管理員」的快取秒數。拿掉權限後最慢這麼久生效。
    auth_cache_ttl_seconds: int = 30
    # 第一個請求進來時在背景載入公用資料快取。只有測試會關掉。
    start_cache_loader: bool = True

    @classmethod
    def from_settings(cls, s: AppSettings) -> "FufoodConfig":
        return cls(
            database_url=s.text("DATABASE_URL"),
            google_client_id=s.text("GOOGLE_CLIENT_ID"),
            session_secret=s.text("SESSION_SECRET"),
            allowed_origins=s.origins(),
            first_admin_email=s.text("FIRST_ADMIN_EMAIL").lower(),
            db_timeout_seconds=s.int("DB_TIMEOUT_SECONDS", 20),
            cache_retry_seconds=s.int("CACHE_RETRY_SECONDS", 30),
            daily_check_limit=s.int("DAILY_CHECK_LIMIT", 10),
            convert_daily_limit=s.int("CONVERT_DAILY_LIMIT", 5),
            feedback_daily_limit=s.int("FEEDBACK_DAILY_LIMIT", 3),
            feedback_retention_days=s.int("FEEDBACK_RETENTION_DAYS", 90),
            feedback_table_limit_mb=s.int("FEEDBACK_TABLE_LIMIT_MB", 150),
            session_idle_days=s.int("SESSION_IDLE_DAYS", 90),
            auth_cache_ttl_seconds=s.int("AUTH_CACHE_TTL_SECONDS", 30),
            start_cache_loader=s.text("START_CACHE_LOADER", "1") != "0",
        )


_current = FufoodConfig()


def set_current(cfg: FufoodConfig) -> None:
    global _current
    _current = cfg


def get() -> FufoodConfig:
    return _current
