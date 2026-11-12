"""The diary app's API (all under /diary): sign-in exchange, daily check, feedback.

Errors are {"error": message for people, "code": code}. Every call except /api/v1/session carries our
own app pass (auth.app_pass); a missing or bad pass is rejected before any quota is used or the
database is touched.
"""

from __future__ import annotations

import base64
import binascii
import json
import re

from flask import Blueprint, jsonify, request
from psycopg.types.json import Jsonb

from shared import google_auth, payload, timeutil
from shared.quota import DailyCounter
from shared.textutil import graphemes

from . import cache, config, db
from . import feedback as feedback_db
from .auth import app_pass, rejections
from .textutil import parse_version

bp = Blueprint("diary_api", __name__)

CHECK_MAX_BYTES = 16 * 1024
# Feedback: the log attachment is at most 1 MiB compressed (≈1.4 MB as base64) plus text; the server
# refuses anything that decompresses past 4 MiB (zip bombs).
FEEDBACK_MAX_BODY = 1_600_000
ATTACHMENT_MAX_COMPRESSED = 1024 * 1024
ATTACHMENT_MAX_DECOMPRESSED = 4 * 1024 * 1024
SNAPSHOT_MAX_BYTES = 64 * 1024
DESCRIPTION_MAX_CHARS = 1000
# Any real Google account can call feedback, and each call queries the database; attempts are capped
# in memory first so retries can't keep waking the database (3 new + 5 resends a day fits easily).
FEEDBACK_ATTEMPTS_PER_DAY = 20

check_quota = DailyCounter(lambda: config.get().daily_check_limit)
feedback_attempts = DailyCounter(lambda: FEEDBACK_ATTEMPTS_PER_DAY)

PLATFORMS = ("android", "ios")
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _error(status: int, code: str, message: str):
    return jsonify({"error": message, "code": code}), status


def authenticate():
    """For endpoints that need a signed-in user: (claims, None) or (None, error response).

    Only our own pass is accepted — never a raw Google ID token. No released app version ever sent
    ID tokens, so there is no compatibility path to carry.
    """
    claims = app_pass.verify(config.get().session_secret, google_auth.extract_bearer_token(request))
    if claims is None:
        rejections.note("invalid pass")
        return None, _error(401, "invalid_token", "登入已過期，請重新登入")
    return claims, None


@bp.post("/api/v1/session")
def session():
    """Called once when the user signs in on the phone: verify the Google ID token, issue a pass.

    Nothing is stored: the pass carries the account id, email and expiry, signed against forgery.
    """
    cfg = config.get()
    if not cfg.session_secret or not cfg.google_client_id:
        return _error(503, "not_configured", "後端還沒設定登入")
    claims = google_auth.verify(google_auth.extract_bearer_token(request), cfg.google_client_id)
    if claims is None:
        rejections.note("invalid Google token")
        return _error(401, "invalid_token", "Google 登入憑證無效或已過期，請重新登入")
    token, exp = app_pass.issue(cfg.session_secret, claims["sub"], claims["email"])
    return jsonify({"token": token, "expires_at": exp})


@bp.post("/api/v1/check")
def check():
    """Daily check: minimum app version (forced update), announcement, and the font list when the
    phone's copy is out of date. One request uses one of the day's checks.

    The request (pass, app version, platform, font list version) only decides the response and is
    not stored; only today's count per account is kept in memory.
    """
    try:
        body = payload.read_json(request, CHECK_MAX_BYTES)
    except payload.PayloadError as exc:
        rejections.note("bad body")
        return _error(exc.status, exc.code, str(exc))
    claims, err = authenticate()
    if err:
        return err
    if not isinstance(body, dict):
        return _error(400, "bad_request", "內容格式不對")
    app_version = parse_version(body.get("app_version"))
    # Sent from the first version, so a future iOS build can get its own minimum version without the
    # server having to guess what old Android builds meant: a value baked into a released client can
    # never be changed afterwards.
    platform = body.get("platform")
    fonts_version = body.get("fonts_version")
    if app_version is None or platform not in PLATFORMS or not (
            fonts_version is None or (isinstance(fonts_version, str) and len(fonts_version) <= 20)):
        return _error(400, "bad_request", "內容格式不對")

    snap = cache.public.ensure_loaded()
    if snap is None:
        # Not loaded yet: "try again later", and the day's quota is not used.
        return _error(503, "not_ready", "暫時無法檢查，請稍後再試")
    if not check_quota.try_take(claims["sub"]):
        rejections.note("daily check limit")
        return _error(429, "daily_limit", "今天的檢查次數已經用完，明天會自動恢復")

    cfg_row = snap["app_config"]
    minimum = parse_version(cfg_row["min_app_version"]) or (0, 0, 0)
    force = app_version < minimum
    result = {
        "min_app_version": cfg_row["min_app_version"],
        "force_update": force,
        "force_reason": cfg_row["force_reason"] if force else "",
        "update_url": cfg_row["update_url"],
        "announcement": cfg_row["announcement"],
        "checks_left": max(config.get().daily_check_limit - check_quota.used(claims["sub"]), 0),
        "server_time": timeutil.now().isoformat(),
    }
    release = snap["fonts"]
    if release is None:
        result["fonts"] = {"status": "none"}
    elif release["version"] == fonts_version:
        result["fonts"] = {"status": "up_to_date", "version": release["version"]}
    else:
        result["fonts"] = {"status": "updated", "version": release["version"], "content": release["content"]}
    if claims.get("renew"):
        # The pass is over 30 days old: hand out a fresh one, so active users never reach expiry.
        result["session_token"], result["session_expires_at"] = app_pass.issue(
            config.get().session_secret, claims["sub"], claims["email"])
    return jsonify(result)


def _short_text(body: dict, key: str, max_len: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
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
    """One feedback report. Re-sending the same id answers 200 duplicate without using quota."""
    try:
        body = payload.read_json(request, FEEDBACK_MAX_BODY)
    except payload.PayloadError as exc:
        rejections.note("bad body")
        return _error(exc.status, exc.code, str(exc))
    claims, err = authenticate()
    if err:
        return err
    try:
        row = _parse_feedback(body)
    except payload.PayloadError as exc:
        return _error(exc.status, exc.code, str(exc))

    if not feedback_attempts.try_take(claims["sub"]):
        rejections.note("feedback attempts")
        return _error(429, "daily_limit", "今天的意見回饋已達上限，明天會自動恢復")
    email = claims["email"]
    dropped = False
    with db.connection() as conn:
        with conn.cursor() as cur:
            # Reports from one account queue up, so the daily limit counts correctly.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"all-mo-diary:feedback:{email}",))
            owner = feedback_db.existing_owner(cur, row["id"])
            if owner is not None:
                if owner == email:
                    return jsonify({"id": row["id"], "status": "duplicate"}), 200
                return _error(409, "id_conflict", "意見回饋編號重複")
            if feedback_db.count_today(cur, email) >= config.get().feedback_daily_limit:
                return _error(429, "daily_limit", "今天的意見回饋已達上限，明天會自動恢復")
            feedback_db.purge_old(cur)
            if row["attachment"] is not None and feedback_db.over_limit(cur):
                # Table full: keep the report, drop the log (the admin console shows a warning).
                row["attachment"] = None
                row["snapshot"] = {**row["snapshot"], "attachment_dropped": True}
                dropped = True
            row["attachment_bytes"] = len(row["attachment"]) if row["attachment"] is not None else 0
            row["email"] = email
            row["snapshot"] = Jsonb(row["snapshot"])
            if not feedback_db.insert(cur, row):
                return jsonify({"id": row["id"], "status": "duplicate"}), 200
    return jsonify({"id": row["id"], "status": "received", "attachment_dropped": dropped}), 201
