"""App 用的 API:登入、每日檢查、意見回饋、舊格式轉換。

登入時用 Google ID Token 換一張通行證(auth.app_pass),之後每一支都帶通行證
(Authorization: Bearer ...)。沒帶或無效的請求直接拒絕:不扣額度、不碰資料庫。
連不上 Google 時回 503(shared.flask_app),App 保持登入、晚點再試。
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import re
import threading

from flask import Blueprint, jsonify, request
from psycopg.types.json import Jsonb

from shared import google_auth, payload, timeutil
from shared.quota import DailyCounter
from shared.textutil import graphemes

from . import cache, config, db, export_formats
from . import feedback as feedback_db
from .auth import app_pass, rejections, secret
from .textutil import parse_version

bp = Blueprint("public", __name__)

# 每日檢查的內容很小(版本號而已),上限給 16 KB。
CHECK_MAX_BYTES = 16 * 1024
# 意見回饋:附件壓縮後最多 1 MiB,base64 之後約 1.4 MB,
# 再加上描述與快照。伺服器解壓上限 4 MiB,超過就拒絕(防壓縮炸彈)。
FEEDBACK_MAX_BODY = 1_600_000
ATTACHMENT_MAX_COMPRESSED = 1024 * 1024
ATTACHMENT_MAX_DECOMPRESSED = 4 * 1024 * 1024
SNAPSHOT_MAX_BYTES = 64 * 1024
DESCRIPTION_MAX_CHARS = 1000
# 舊格式轉換:只送 data.json(不含照片),App 先用 gzip 壓縮。最大的情況
# (8 個寶寶、每天 10 筆、兩年半)約 25 MB,一般使用約 1 MB。
CONVERT_MAX_COMPRESSED = 8 * 1024 * 1024
CONVERT_MAX_DECOMPRESSED = 32 * 1024 * 1024

check_quota = DailyCounter(lambda: config.get().daily_check_limit)
convert_quota = DailyCounter(lambda: config.get().convert_daily_limit)
# 意見回饋每次都要查資料庫(每日 3 則、重複編號),而任何一個真的 Google 帳號都能送;
# 先在記憶體限制「嘗試次數」,超過就不碰資料庫(有效的 Google 帳號一直重試,也不會一直叫醒資料庫)。
# App 每天最多送 3 則新的 + 重送待送清單 5 次,20 次綽綽有餘。
FEEDBACK_ATTEMPTS_PER_DAY = 20
feedback_attempts = DailyCounter(lambda: FEEDBACK_ATTEMPTS_PER_DAY)
# 轉換要把整份紀錄放進記憶體;Render 免費方案只有 512 MB,同時只做一份。
_convert_slot = threading.BoundedSemaphore(1)

PLATFORMS = ("android", "ios")

_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _error(status: int, code: str, message: str, **extra):
    return jsonify({"error": message, "code": code, **extra}), status


def _identify():
    """App 的請求帶通行證(auth.app_pass)。

    也還接受 Google ID Token:通行證之前發出去的 App(改成 0.1.0 之前、當時標成 1.0.0 的那批安裝)只會送 ID Token。
    那些安裝都更新到會換通行證的版本之後,這一支就可以拿掉(只剩 /api/v1/session 收 ID Token)。
    """
    token = google_auth.extract_bearer_token(request)
    if app_pass.looks_like(token):
        claims = app_pass.verify(secret(), token)
    else:
        claims = google_auth.verify(token, config.get().google_client_id)
    if claims is None:
        rejections.note("無效憑證")
        return None, _error(401, "invalid_token", "登入憑證無效或已過期")
    return claims, None


# ── 登入:Google ID Token 換 App 的通行證 ──────────────────────

@bp.post("/api/v1/session")
def session():
    """使用者在 App 裡登入時呼叫一次。驗證 Google ID Token,發一張通行證給手機。

    伺服器不保存任何東西:通行證本身帶著帳號編號、Email 與到期時間,用簽章防偽造。
    """
    if not secret():
        return _error(503, "not_configured", "後端還沒設定 SESSION_SECRET")
    claims = google_auth.verify(google_auth.extract_bearer_token(request), config.get().google_client_id)
    if claims is None:
        rejections.note("無效憑證")
        return _error(401, "invalid_token", "登入憑證無效或已過期")
    token, exp = app_pass.issue(secret(), claims["sub"], claims["email"])
    return jsonify({"token": token, "expires_at": exp})


# ── 每日檢查 ──────────────────────────────────────────────────

def _parse_datasets(raw) -> dict | None:
    """{"food_list": {"version": "...", "formats": [1]}, ...};格式不對回 None。"""
    if not isinstance(raw, dict) or len(raw) > 20:
        return None
    parsed = {}
    for name, info in raw.items():
        if not isinstance(name, str) or not isinstance(info, dict):
            return None
        version = info.get("version")
        formats = info.get("formats")
        if version is not None and not (isinstance(version, str) and len(version) <= 20):
            return None
        if (not isinstance(formats, list) or not formats or len(formats) > 20
                or any(isinstance(f, bool) or not isinstance(f, int) for f in formats)):
            return None
        parsed[name] = {"version": version, "formats": formats}
    return parsed


@bp.post("/api/v1/check")
def check():
    """每日檢查:回傳最低可用 App 版本,以及每個資料集是
    「已是最新」「有新資料(附內容)」「需要新版 App」三種之一。一次請求算一次額度。

    請求內容(登入憑證、App 版本、支援的格式版本、各資料集版本)只用來決定回應,
    用完不存;記憶體裡只留當天的次數。
    """
    try:
        body = payload.read_json(request, CHECK_MAX_BYTES)
    except payload.PayloadError as exc:
        rejections.note("內容不合")
        return _error(exc.status, exc.code, str(exc))
    claims, err = _identify()
    if err:
        return err
    app_version = parse_version(body.get("app_version")) if isinstance(body, dict) else None
    wanted = _parse_datasets(body.get("datasets")) if isinstance(body, dict) else None
    # 平台(android、ios)。第一版就送,iOS 版才能各自設強制更新門檻,
    # 不會出現已發出去的舊版送錯值、伺服器只好長期相容的情況。
    # 目前只檢查格式,還沒有用在判斷上。
    platform = body.get("platform", "android") if isinstance(body, dict) else None
    if app_version is None or wanted is None or platform not in PLATFORMS:
        return _error(400, "bad_request", "內容格式不對")

    snap = cache.public.ensure_loaded()
    if snap is None:
        # 還沒載入完成:回「暫時無法檢查」,不扣額度,App 依重試規則晚點再試。
        return _error(503, "not_ready", "暫時無法檢查，請稍後再試")
    if not check_quota.try_take(claims["sub"]):
        rejections.note("超過每日檢查次數")
        return _error(429, "daily_limit", "今天的檢查次數已經用完，明天會自動恢復")

    cfg = snap["app_config"]
    minimum = parse_version(cfg["min_app_version"]) or (0, 0, 0)
    force = app_version < minimum
    result = {
        "min_app_version": cfg["min_app_version"],
        "force_update": force,
        "force_reason": cfg["force_reason"] if force else "",
        "update_url": cfg["update_url"],
        "checks_left": max(config.get().daily_check_limit - check_quota.used(claims["sub"]), 0),
        "server_time": timeutil.now().isoformat(),
        "datasets": {},
    }
    needs_app_update = False
    for name, info in wanted.items():
        release = snap["datasets"].get(name)
        if release is None:
            continue
        entry = {"version": release["version"], "format_version": release["format_version"]}
        if release["format_version"] not in info["formats"]:
            entry["status"] = "app_update_required"
            needs_app_update = True
        elif info["version"] == release["version"]:
            entry["status"] = "up_to_date"
        else:
            entry["status"] = "updated"
            entry["content"] = release["content"]
        result["datasets"][name] = entry
    if needs_app_update:
        result["notice"] = cfg["dataset_notice"]
    if claims.get("renew"):
        # 通行證發出超過一段時間:順便換一張新的,有在用的人就不會遇到過期。
        result["session_token"], result["session_expires_at"] = app_pass.issue(secret(), claims["sub"], claims["email"])
    return jsonify(result)


# ── 意見回饋 ──────────────────────────────────────────────────

def _short_text(body: dict, key: str, max_len: int, required: bool = True) -> str:
    value = body.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > max_len:
        raise payload.PayloadError(f"{key} 的格式不對")
    return value.strip()


def _parse_feedback(body) -> dict:
    if not isinstance(body, dict):
        raise payload.PayloadError("內容格式不對")
    fid = body.get("id")
    if not isinstance(fid, str) or not _UUID4.match(fid):
        raise payload.PayloadError("意見回饋編號的格式不對")
    kind = body.get("kind")
    if kind not in feedback_db.KINDS:
        raise payload.PayloadError("類型不對")
    description = body.get("description")
    if not isinstance(description, str) or not description.strip():
        raise payload.PayloadError("請填寫描述")
    if graphemes(description) > DESCRIPTION_MAX_CHARS:
        raise payload.PayloadError(f"描述最多 {DESCRIPTION_MAX_CHARS} 字")
    install_id = body.get("install_id")
    if not isinstance(install_id, str) or not _UUID4.match(install_id):
        raise payload.PayloadError("安裝 ID 的格式不對")
    if parse_version(body.get("app_version")) is None:
        raise payload.PayloadError("App 版本的格式不對")
    snapshot = body.get("snapshot")
    if not isinstance(snapshot, dict) or len(json.dumps(snapshot, ensure_ascii=False).encode()) > SNAPSHOT_MAX_BYTES:
        raise payload.PayloadError("快照的格式不對或太大")

    attachment = None
    raw_attachment = body.get("attachment")
    if raw_attachment is not None:
        if kind != "problem":
            raise payload.PayloadError("只有「遇到問題」才附記錄檔")
        if not isinstance(raw_attachment, str):
            raise payload.PayloadError("記錄檔的格式不對")
        try:
            attachment = base64.b64decode(raw_attachment, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise payload.PayloadError("記錄檔的格式不對") from exc
        if len(attachment) > ATTACHMENT_MAX_COMPRESSED:
            raise payload.PayloadError("記錄檔太大", 413, "too_large")
        payload.gunzip_limited(attachment, ATTACHMENT_MAX_DECOMPRESSED)

    return {
        "id": fid,
        "kind": kind,
        "description": description.strip(),
        "device_time": _short_text(body, "device_time", 40),
        "install_id": install_id,
        "app_version": body["app_version"],
        "build_number": _short_text(body, "build_number", 12),
        "device_model": _short_text(body, "device_model", 100),
        "os_version": _short_text(body, "os_version", 100),
        "snapshot": snapshot,
        "attachment": attachment,
    }


@bp.post("/api/v1/feedback")
def submit_feedback():
    """收一則意見回饋。重送同一個編號回 200 duplicate,不重複扣額度。"""
    try:
        body = payload.read_json(request, FEEDBACK_MAX_BODY)
    except payload.PayloadError as exc:
        rejections.note("內容不合")
        return _error(exc.status, exc.code, str(exc))
    claims, err = _identify()
    if err:
        return err
    try:
        row = _parse_feedback(body)
    except payload.PayloadError as exc:
        return _error(exc.status, exc.code, str(exc))

    if not feedback_attempts.try_take(claims["sub"]):
        rejections.note("意見回饋嘗試過多")
        return _error(429, "daily_limit", "今天的意見回饋已達上限，明天會自動恢復")
    email = claims["email"]
    dropped = False
    with db.connection() as conn:
        with conn.cursor() as cur:
            # 同一個 Email 同時送好幾則時排隊,每日 3 則才算得準。
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"mama-fufood:feedback:{email}",))
            owner = feedback_db.existing_owner(cur, row["id"])
            if owner is not None:
                if owner == email:
                    return jsonify({"id": row["id"], "status": "duplicate"}), 200
                return _error(409, "id_conflict", "意見回饋編號重複")
            if feedback_db.count_today(cur, email) >= config.get().feedback_daily_limit:
                return _error(429, "daily_limit", "今天的意見回饋已達上限，明天會自動恢復")
            feedback_db.purge_old(cur)
            if row["attachment"] is not None and feedback_db.over_limit(cur):
                # 資料表滿了:照樣收下意見回饋,只是不收記錄檔(後台會顯示警告)。
                row["attachment"] = None
                row["snapshot"] = {**row["snapshot"], "attachment_dropped": True}
                dropped = True
            row["attachment_bytes"] = len(row["attachment"]) if row["attachment"] is not None else 0
            row["email"] = email
            row["snapshot"] = Jsonb(row["snapshot"])
            if not feedback_db.insert(cur, row):
                return jsonify({"id": row["id"], "status": "duplicate"}), 200
    return jsonify({"id": row["id"], "status": "received", "attachment_dropped": dropped}), 201


# ── 舊格式轉換 ────────────────────────────────────────────────

def _gzip_json(data: dict, status: int = 200):
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if "gzip" in request.headers.get("Accept-Encoding", "").lower():
        response = jsonify()  # 先拿一個 Response,再換成壓縮過的內容
        response.set_data(gzip.compress(raw, compresslevel=6))
        response.headers["Content-Encoding"] = "gzip"
    else:
        response = jsonify()
        response.set_data(raw)
    response.status_code = status
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


@bp.post("/api/v1/convert")
def convert():
    """把舊格式的 data.json 逐版轉到最新格式(匯入舊的匯出檔時用)。

    ★ 無狀態:轉換前後都不保存、不記錄內容(「寶寶資料不上伺服器」的唯一例外,條款與隱私權政策都有寫)。
      這支函式不寫任何包含內容的日誌;出錯時只回錯誤代碼。
    ★ 先驗證登入憑證,才讀那一大包內容。
    """
    claims, err = _identify()
    if err:
        return err
    # ★ 先把內容讀完(最多 8 MiB)再搶名額:讀內容要等手機慢慢上傳,不能拿著唯一的名額等網路。
    #   名額只給解壓、檢查與轉換這段純計算。
    try:
        raw = payload.read_raw(request, CONVERT_MAX_COMPRESSED)
    except payload.PayloadError as exc:
        return _error(exc.status, exc.code, str(exc))
    gzipped = payload.is_gzipped(request)
    if not _convert_slot.acquire(timeout=30):
        return _error(503, "busy", "轉換服務忙碌中，請稍後再試")
    try:
        try:
            data = payload.decode_json(raw, gzipped, CONVERT_MAX_DECOMPRESSED)
        except payload.PayloadError as exc:
            return _error(exc.status, exc.code, str(exc))
        version = data.get("format_version") if isinstance(data, dict) else None
        if isinstance(version, bool) or not isinstance(version, int):
            return _error(400, "bad_request", "內容格式不對")
        if version == export_formats.CURRENT:
            return _error(409, "already_current", "這個檔案已經是最新格式，不需要轉換")
        if version not in export_formats.DEFINITIONS or version > export_formats.CURRENT:
            return _error(400, "unsupported_format", "不支援這個格式版本")
        errors = export_formats.validate(data, version)
        if errors:
            return _error(422, "invalid_file", "檔案內容不符合格式", errors=errors[:20])
        if not convert_quota.try_take(claims["sub"]):
            return _error(429, "daily_limit", "今天的轉換次數已經用完，明天會自動恢復")
        try:
            converted, missing = export_formats.convert(data)
            if not missing and export_formats.validate(converted):
                raise ValueError("轉換結果沒有通過檢查")
        except Exception:  # noqa: BLE001 - 轉換程式的錯;不記內容,把額度還回去
            convert_quota.give_back(claims["sub"])
            return _error(500, "convert_failed", "轉換失敗，請把這個問題回報給我們")
        return _gzip_json({"format_version": export_formats.CURRENT, "data": converted, "missing": missing})
    finally:
        _convert_slot.release()
