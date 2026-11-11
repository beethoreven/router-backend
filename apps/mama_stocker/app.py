"""mama-stocker（LINE 查股價 bot）的 Flask app，掛在 /mama-stocker。

    GET  /mama-stocker/warm                觸發預抓用（cron-job.org 每 10 分鐘），不碰資料庫、不對外連線
    POST /mama-stocker/line/webhook        LINE Messaging API 的 webhook
    GET  /mama-stocker/line/img/<token>.png  走勢圖（LINE 照 bot 回出去的簽名網址來抓）

設定：MAMA_STOCKER__* 環境變數（config.py）。資料庫連線與錯誤格式來自 shared/。

★ 建 app 的時候不開執行緒、不對外連線：預抓在第一個請求時才開始（_start_warm_up）。
"""

from __future__ import annotations

import logging
import threading
import time

from flask import Blueprint, current_app, jsonify, request, url_for

from router.settings import AppSettings
from shared.flask_app import new_app

from . import config
from .db import connection as db_connection
from .line_utils import client as line_client
from .line_utils import handler as line_handler
from .line_utils import images as line_images
from .line_utils import signature as line_signature
from .stock_utils import clock, sources

log = logging.getLogger(__name__)

# 最大的請求是 LINE 的 webhook：一包事件，幾 KB。給寬一點，但遠小於別的 app。
MAX_CONTENT_LENGTH = 1 * 1024 * 1024

bp = Blueprint("mama_stocker", __name__)


def create_app(settings: AppSettings):
    cfg = config.StockerConfig.from_settings(settings)
    config.set_current(cfg)

    # LINE 沒設定時整包停用，而「停用」在外面看起來跟「正常」一樣安靜，所以載入時講清楚。
    if not cfg.line_enabled():
        log.warning("MAMA_STOCKER__LINE_CHANNEL_SECRET / MAMA_STOCKER__LINE_CHANNEL_ACCESS_TOKEN 未設定，"
                    "LINE bot 停用：webhook 會回 503")
    # 沒有資料庫就查不到誰有權限，等於沒有人能用——這也要講清楚。
    if not cfg.database_url:
        log.warning("MAMA_STOCKER__DATABASE_URL 未設定：查不到使用者名單，所有人都會被當成沒有權限")

    app = new_app(__name__, max_content_length=MAX_CONTENT_LENGTH)
    app.register_blueprint(bp)
    if cfg.start_warm_up:
        app.before_request(_start_warm_up)
    return app


# ── 預抓 ────────────────────────────────────────────────────────

def _warm_up() -> None:
    """把常用的那幾包抓進快取，快過期的提前重抓——使用者問的時候不必等。

    抓失敗沒關係，真的有人問的時候會再抓一次。
    """
    today = clock.today()
    year = today.year
    loads = [
        ("twse_quotes", sources.twse_quotes), ("tpex_quotes", sources.tpex_quotes),
        ("twse_yields", sources.twse_yields), ("tpex_yields", sources.tpex_yields),
        # 配息公告：第一次問「利率」「配息」要用到今年與去年兩份，各要下載並解析
        # 一張六百 KB 的網頁表格。在 Render 的 0.1 顆 CPU 上那是三到五秒——
        # 2026-10-04 實測，沒預抓時第一次問上櫃股票的利率花了 5.2 秒。
        *((f"mops_{m}_{y}", lambda m=m, y=y: sources.mops_dividends(m, y))
          for m in ("tse", "otc") for y in (year, year - 1)),
        ("tpex_past_dividends", lambda: sources.tpex_past_dividends(today)),
        ("tpex_upcoming_dividends", sources.tpex_upcoming_dividends),
    ]
    with sources.refresh_ahead():
        for name, load in loads:
            started = time.perf_counter()
            try:
                n = len(load())
                took = time.perf_counter() - started
                if took > 0.05:
                    # 真的有去抓才記（還在期限內的不記）。這台主機連不連得到
                    # 資料來源、各要多久，log 就看得出來。
                    log.info("[warm] %s %d 筆，%.1f s", name, n, took)
            except Exception as exc:  # noqa: BLE001
                log.warning("預抓 %s 失敗（%.1f s）：%s", name, time.perf_counter() - started,
                            f"{type(exc).__name__}: {exc}"[:300])


# 距離上一輪至少隔多久才再抓。要比「資料期限 − 提前量」（一小時 − 十五分鐘）短，資料才不會
# 在兩次預抓之間過期。
# ★ 比 cron 的 10 分鐘短一點：門檻剛好等於 cron 的週期的話，時間差一點點就會跳過一輪，
#   實際變成 20 分鐘一次。
_WARM_EVERY = 540
_warm_lock = threading.Lock()
_warm_next = 0.0
# 上一輪預抓開始的時間（monotonic），/warm 回報用。
_warm_started: float | None = None


def _start_warm_up():
    """有請求進來時，如果距離上次預抓超過十分鐘，就在背景再抓一次。

    靠請求來觸發，不是一條一直睡著等的執行緒：容器被凍結再醒來時，不必指望背景
    執行緒還活著。

    ★ 整台的保活打的是 router 的 /health，**不會**經過這裡。
      所以 cron-job.org 另外每 10 分鐘打一次 /mama-stocker/warm，預抓才是定時的。
    ★ 頻率的閘門只有這裡的「間隔＋鎖」：before_request 對 /mama-stocker 底下任何請求都會跑，
      連亂打的 404 也算，不能假設只有 cron 會來。

    ★ **不能在 import 或 create_app 的時候就開執行緒**。2026-10-04 上線第一天就是這樣壞的：
      在模組層開執行緒去預抓時，Render 上 worker 裡每一次對外連線都永遠卡住，
      連 requests 的 timeout 都不會觸發——回覆 LINE、查股價、畫圖全部沒有
      反應，只有不對外連線的 /health 正常。本機完全測不出來。改成這裡之後
      當場就好了（實測 webhook 0.6 秒）。為什麼會卡沒有查到底。
    """
    global _warm_next, _warm_started
    if time.monotonic() < _warm_next or not _warm_lock.acquire(blocking=False):
        return
    _warm_next = time.monotonic() + _WARM_EVERY
    _warm_started = time.monotonic()

    def run():
        try:
            _warm_up()
        finally:
            _warm_lock.release()

    threading.Thread(target=run, name="mama-stocker-warm-up", daemon=True).start()


# ── 路由 ────────────────────────────────────────────────────────

@bp.get("/warm")
def warm():
    """觸發預抓（真正的工作在 before_request 的 _start_warm_up）。

    不叫 /health：/health 在這個服務裡是整台的保活，由 router 回答；各 app 再開一個同名的，容易讓人以為每個 app 都要自己保活。
    不碰資料庫、不對外連線，不需要登入；誰都打得到，所以不回任何設定狀態。
    """
    since = None if _warm_started is None else round(time.monotonic() - _warm_started)
    return jsonify({"ok": True, "warm_seconds_ago": since})


@bp.post("/line/webhook")
def line_webhook():
    """LINE Messaging API 的 webhook。

    ## 這支沒有登入，簽章就是把關

    網址是公開的、沒有 session。唯一能證明「這包資料真的來自 LINE」的是
    X-Line-Signature。驗不過一律 401。

    ## 一律回 200（除了驗簽失敗）

    ★ 處理單一事件失敗時**不要**回 5xx。LINE 會重送整包，而那包裡可能
      有已經處理成功的事件——重送會重新回覆一次，聊天室裡就會出現兩句
      一樣的話。失敗記 log，回 200。（new_app 的 500 handler 只接沒被接住的例外，
      這裡每個事件都自己接住了。）
    """
    if not line_client.enabled():
        return jsonify({"error": "LINE 未設定", "code": "line_not_configured"}), 503

    raw = request.get_data()
    # ★ 一定要用原始 bytes。先 json.loads 再 dumps 會改變空白與鍵序，
    #   雜湊就對不上——症狀是「設定明明都對卻一直 401」。
    if not line_signature.verify(
        raw, request.headers.get("X-Line-Signature"), line_client.channel_secret()
    ):
        return jsonify({"error": "簽章驗證失敗", "code": "bad_signature"}), 401

    # force：不看 Content-Type。簽章已經證明這包是 LINE 送的，沒有理由因為
    # 標頭寫法不同就把事件安靜地丟掉。
    body = request.get_json(force=True, silent=True) or {}

    # 試跑：不回覆給 LINE，把「本來會回什麼」直接放在回應裡（dry_run.py 是呼叫它的小工具）。
    #
    # 這條路是 2026-10-05 加的：線上回了錯的價格、又回「查不到」，而回覆只會
    # 出現在使用者的手機上，從外面完全看不到它回了什麼、卡在哪一個資料來源。
    # ★ 簽章已經在上面驗過——拿得出 channel secret 的人才走得到這裡，所以這
    #   不是一個公開的查詢入口。
    if request.headers.get("X-Dry-Run") == "1":
        out = []
        for event in body.get("events") or []:
            started = time.perf_counter()
            try:
                replies = _handle_line_event(event, dry_run=True)
            except Exception as exc:  # noqa: BLE001
                replies = [f"{type(exc).__name__}: {exc}"]
            out.append({"replies": replies, "seconds": round(time.perf_counter() - started, 1)})
        return jsonify({"ok": True, "dry_run": out, "sources": sources.stats()})

    events = body.get("events") or []
    for event in events:
        try:
            _handle_line_event(event)
        except Exception as exc:  # noqa: BLE001 - 見上面「一律回 200」
            log.warning("LINE 事件處理失敗：%s", type(exc).__name__, exc_info=True)

    # 群組名單排在**所有回覆之後**。記一個人要查一次 LINE（最多 3 秒）、連一次
    # 資料庫（連不上時最多等 9 秒）；排在前面的話，資料庫一掛，同一包裡後面
    # 那則真的在問 bot 的訊息就會被拖過 replyToken 與 webhook 的期限。
    #
    # ★ 記失敗了不設冷卻，下一則訊息會再試：「剛加入」的事件 LINE 只送一次，
    #   冷卻期間丟掉的人就永遠不會出現在名單上。反正已經不擋回覆了。
    for event in events:
        try:
            line_handler.note_group_member(event)
        except Exception as exc:  # noqa: BLE001 - 記不到名單不該讓 webhook 回 5xx
            log.warning("記錄群組成員失敗：%s", type(exc).__name__)
    return jsonify({"ok": True})


def _handle_line_event(event: dict, *, dry_run: bool = False) -> list:
    """處理一則事件。回傳送出去（試跑時是本來會送出去）的訊息。"""
    sent: list = []

    def reply(message) -> None:
        sent.append(message)
        if not dry_run:
            line_client.reply(reply_token, message)

    text = line_handler.text_to_me(event)
    if text is None:
        # 不是在跟我講話就完全不反應——不回覆、不記 log。群組裡大部分
        # 訊息都跟 bot 無關，每一則都留一筆 log 只會把真的錯誤淹掉。
        return sent
    reply_token = event.get("replyToken")
    if not reply_token:
        return sent
    sender = (event.get("source") or {}).get("userId")

    # 拿自己的 userId。開通要先有這個 id，而它只有在對方跟 bot 有互動之後才
    # 拿得到。任何人都能問——回的是「你自己的 id」，不是誰的秘密。
    if line_handler.is_my_id(event, text):
        if sender:
            reply(f"你的 LINE userId：\n{sender}")
        return sent

    private = (event.get("source") or {}).get("type") == "user"
    reply(_line_messages(line_handler.handle_command(text, sender=sender, private=private)))
    return sent


def _line_messages(reply):
    """handler 回的東西 → line_client.reply 收的東西。

    多半是幾行字，原樣交出去。走勢回的是 ImageReply：這裡組出這個後端自己的
    圖片網址，LINE 再照網址來抓（見 line_image）。webhook 這一趟只簽一個
    網址，不畫圖——畫圖是 LINE 來抓的那一趟的事，不佔這裡 5 秒的預算。

    ★ https 寫死：LINE 只收 https。TLS 在 Render 前端就解掉了，這個 process
      看到的 request.scheme 可能是 http——照它組的話網址會是 http://，LINE 不收。
    ★ 網址前綴（/mama-stocker）由 DispatcherMiddleware 放進 SCRIPT_NAME，url_for 會自己帶上
      （tests/mama_stocker/test_webhook.py 有測）。
    ★ 預覽圖與原圖用同一個網址：預覽上限 1MB，這裡的圖才幾十 KB，不必另外
      畫一張小的。
    """
    if not isinstance(reply, line_images.ImageReply):
        return reply
    url = url_for(
        "mama_stocker.line_image",
        token=line_images.sign(reply.spec, line_client.channel_secret()),
        _external=True,
        _scheme="https",
    )
    return [{"type": "image", "originalContentUrl": url, "previewImageUrl": url}]


@bp.get("/line/img/<token>.png")
def line_image(token: str):
    """LINE 照 _line_messages 發出去的網址來抓圖。

    來抓的是看訊息的那台裝置上的 LINE App，不會帶任何登入資訊。網址上的
    簽名證明這是 bot 自己回出去的（為什麼要擋，見 line_utils/images.py）。
    驗不過一律 404。
    """
    if not line_client.enabled():
        return jsonify({"error": "LINE 未設定", "code": "line_not_configured"}), 503
    spec = line_images.verify(token, line_client.channel_secret())
    started = time.perf_counter()
    png = line_images.render(spec) if spec else None
    if png is None:
        return jsonify({"error": "網址無效", "code": "bad_image_url"}), 404
    # LINE 什麼時候、由誰來抓圖，官方文件沒寫，只能靠這一行 log 量。圖出不來的時候，這一行是唯一能分辨「沒來抓」與「畫太久」的線索。
    log.info("[line-img] %s｜%.0f ms｜%d KB｜UA: %s", spec, (time.perf_counter() - started) * 1000,
             len(png) // 1024, request.headers.get("User-Agent", "")[:120] or "(無)")
    resp = current_app.response_class(png, mimetype="image/png")
    # 網址含發出時間，每一次查詢都是新的網址，所以快取再久也不會讓明天的人
    # 看到今天的圖。留得愈久，LINE 愈不需要回來抓。
    resp.headers["Cache-Control"] = "public, max-age=31536000"
    return resp
