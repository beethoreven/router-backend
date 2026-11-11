"""媽媽記副食品的後端,掛在 /mama-fufood 底下。

寶寶的名稱與紀錄只在手機與使用者自己的 Google Drive,這個後端不保存。它提供:
- routes.py:登入(Google ID Token 換通行證)、每日檢查、意見回饋、舊格式轉換。
- admin_routes.py:管理後台(公用資料、App 版本設定、意見回饋、管理員)。

設定:MAMA_FUFOOD__* 環境變數(config.py)。登入、資料庫、錯誤處理與各種上限用 shared/
(auth.py 與 db.py 寫了用哪些)。

★ 建 app 時不開執行緒、不對外連線:公用資料快取的背景載入,等第一個請求進來才在 worker 裡開始
  (在 import 時就開的版本,部署到 Render 後背景載入一次都沒有跑)。
★ 沒有自己的 /health:保活打的是 router 的 /health。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import jsonify

from router.settings import AppSettings
from shared.flask_app import allow_cors, new_app

from . import admin_routes, cache, config, db, routes

log = logging.getLogger(__name__)

# 最大的請求是舊格式轉換(壓縮後 8 MiB);各支 API 另有自己更小的上限(shared.payload.read_json)。
MAX_CONTENT_LENGTH = 9 * 1024 * 1024


def create_app(settings: AppSettings):
    cfg = config.FufoodConfig.from_settings(settings)
    config.set_current(cfg)
    if not cfg.database_url:
        log.warning("MAMA_FUFOOD__DATABASE_URL is not set: public data can't load and /mama-fufood/ready stays 503")

    app = new_app(__name__, max_content_length=MAX_CONTENT_LENGTH)
    # 只有管理後台需要跨來源(網頁在 GitHub Pages);App 的請求不是瀏覽器發的,不受 CORS 影響。
    allow_cors(app, cfg.allowed_origins, paths=r"/api/admin/*")

    app.register_blueprint(routes.bp)
    app.register_blueprint(admin_routes.bp)

    @app.get("/ready")
    def ready():
        """快取載入了沒(部署後確認用)。快取是從資料庫載入的,載入成功就代表資料庫連得上。

        ★ 這支不需要登入,所以**不碰資料庫**:否則任何人一直打,就能讓 Neon 一直醒著、把免費額度
          用完。最後一次連上資料庫的時間取自 shared.database 記下的值。
        """
        status = cache.status()
        last = db.database.last_ok
        return jsonify({
            "ok": status["loaded"],
            "cache": status,
            "database_configured": bool(config.get().database_url),
            "database_last_ok": datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
        }), 200 if status["loaded"] else 503

    if cfg.start_cache_loader:
        app.before_request(cache.public.start_background_loader)

    return app
