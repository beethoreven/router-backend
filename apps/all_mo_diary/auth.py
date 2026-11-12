"""The diary's sign-in pieces, built from shared/ with the diary's own names and secret.

- app_pass: what the phone keeps after signing in (shared.app_pass). Prefix and purpose are fixed:
  changing either signs every user out.
- admin: the admin console's sessions, admin list and require_admin gate (shared.admin_auth).
- rejections: rejected requests, counted (shared.rejections).
"""

from __future__ import annotations

from shared.admin_auth import AdminAuth
from shared.app_pass import AppPass
from shared.rejections import RejectionLog

from . import config, db

app_pass = AppPass(prefix="amd1.", purpose=b"all-mo-diary:app-token:v1", days=180, renew_days=30)

admin = AdminAuth(
    "all-mo-diary",
    database=db.database,
    purpose=b"all-mo-diary:admin-session:v1",
    secret=lambda: config.get().session_secret,
    first_admin=lambda: config.get().first_admin_email,
    idle_days=lambda: config.get().session_idle_days,
    cache_ttl=lambda: config.get().auth_cache_ttl_seconds,
    sessions_table="admin_sessions",
    admins_table="admins",
)

rejections = RejectionLog("all-mo-diary")
