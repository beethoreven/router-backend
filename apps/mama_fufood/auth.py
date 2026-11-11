"""本 app 的登入相關元件,用 shared/ 組起來,名稱與金鑰是本 app 自己的。

- app_pass:App 登入後存在手機上的通行證(shared.app_pass)。
- admin:管理後台的 session、管理員名單與 require_admin 這道門(shared.admin_auth)。
- rejections:被拒絕的請求只計數(shared.rejections)。

★ prefix、purpose、sessions 表名都沿用搬過來之前的值,秘密也用同一個(MAMA_FUFOOD__SESSION_SECRET
  填原本的 SESSION_SECRET):手機上已經發出去的通行證與後台的登入才會繼續有效。改任何一個,
  所有人都會被登出。
"""

from __future__ import annotations

from shared.admin_auth import AdminAuth
from shared.app_pass import AppPass
from shared.rejections import RejectionLog

from . import config, db

app_pass = AppPass(prefix="mff1.", purpose=b"mama-fufood:app-token:v1", days=180, renew_days=30)

admin = AdminAuth(
    "mama-fufood",
    database=db.database,
    purpose=b"mama-fufood:admin-session:v1",
    secret=lambda: config.get().session_secret,
    first_admin=lambda: config.get().first_admin_email,
    idle_days=lambda: config.get().session_idle_days,
    cache_ttl=lambda: config.get().auth_cache_ttl_seconds,
    sessions_table="sessions",
    admins_table="admins",
)

rejections = RejectionLog("mama-fufood")


def secret() -> str:
    return config.get().session_secret
