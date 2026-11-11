"""fireless-war's endpoints (under /fireless-war). The web page calls them with
`Authorization: Bearer <Google ID token>` on every request.

    GET  /auth/status    is this token valid, and is this email a GM?
    GET  /record         find a session file by date-time       (GM)
    POST /record         copy the template into a new one       (GM)
    GET  /round          one round's character table            (GM, 30/min site-wide)
    GET  /demo-record    same as /record, FirelessWar_Demo_*    (any Google sign-in, 10/hour)
    POST /demo-record    copy FirelessWar_Demo                  (any Google sign-in, 10/hour)
    GET  /demo-round     /round with generic character names    (any Google sign-in, 10/min)
    GET  /ready          which required settings are missing (names only)

Rate limits are site-wide counters ("global" key), not per IP: what they protect is the shared Google
quota (60 Sheets reads/min per service account), and the router keeps no IPs. Each route has its own
counter. deduct_when skips 401/503 so unsigned requests can't use up the GMs' quota.

Error bodies: {"error": message for people, "code": ...}. For an unexpected failure only our own
human-written messages (RuntimeError, e.g. Apps Script's error text or a bad cell value) reach the
client; anything else shows its type name, so URLs or ids inside library errors aren't echoed back.
"""

from __future__ import annotations

import logging

from flask import Flask, g, jsonify, request
from flask_limiter import Limiter

from shared import google_auth

from . import auth, config, parse_data, record_data

log = logging.getLogger(__name__)


def _counts(response) -> bool:
    return response.status_code not in (401, 503)


def _error(status: int, code: str, message: str):
    return jsonify({"error": message, "code": code}), status


def _describe(exc: Exception) -> str:
    log.warning("[fireless-war] outside call failed: %s", type(exc).__name__)
    return str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__


def register(app: Flask, limiter: Limiter) -> None:
    @app.get("/ready")
    def ready():
        missing = config.get().missing()
        return jsonify({"ok": not missing, "missing": missing}), 200 if not missing else 503

    @app.get("/auth/status")
    def auth_status():
        """200 + authorized true/false is a normal answer; 401 only when the token itself is invalid."""
        try:
            email = auth.verified_email()
        except google_auth.VerificationUnavailable:
            return jsonify({"authorized": False, "error": "目前無法驗證登入狀態,請稍後再試",
                            "code": "auth_unavailable"}), 503
        if email is None:
            return jsonify({"authorized": False, "error": "登入憑證無效或已過期", "code": "invalid_token"}), 401
        return jsonify({"authorized": auth.is_gm(email), "email": email}), 200

    def _find(demo: bool):
        msgs = DEMO_MESSAGES if demo else GM_MESSAGES
        datetime_str = request.args.get("datetime")
        if not datetime_str:
            return _error(400, "missing_param", msgs["missing_datetime"])
        try:
            record_data.validate_datetime_param(datetime_str)
        except ValueError as e:
            return _error(400, "invalid_param", str(e))
        try:
            spreadsheet_id = record_data.find_record(datetime_str, demo=demo)
        except Exception as e:  # noqa: BLE001 — Drive unreachable, credentials rejected…
            return _error(500, "google_error", msgs["find_failed"] + _describe(e))
        if spreadsheet_id is None:
            return _error(404, "not_found", msgs["not_found"])
        return jsonify({"spreadsheet_id": spreadsheet_id}), 200

    def _create(demo: bool):
        msgs = DEMO_MESSAGES if demo else GM_MESSAGES
        datetime_str = request.args.get("datetime")
        if not datetime_str:
            return _error(400, "missing_param", msgs["missing_datetime"])
        try:
            record_data.validate_datetime_param(datetime_str)
        except ValueError as e:
            return _error(400, "invalid_param", str(e))
        try:
            # g.email is the verified Google email, not anything the caller typed.
            spreadsheet_id, warning = record_data.create_record(datetime_str, g.email, demo=demo)
        except Exception as e:  # noqa: BLE001 — Apps Script unreachable, wrong secret, Drive error…
            return _error(500, "google_error", msgs["create_failed"] + _describe(e))
        result = {"spreadsheet_id": spreadsheet_id}
        if warning:
            # The file exists; only adding the editor failed. Still a success.
            result["warning"] = warning
        return jsonify(result), 201

    def _round(demo: bool):
        msgs = DEMO_MESSAGES if demo else GM_MESSAGES
        day, type_ = request.args.get("day"), request.args.get("type")
        if not day or not type_:
            return _error(400, "missing_param", msgs["missing_round"])
        spreadsheet_id = request.args.get("spreadsheet_id") or config.get().spreadsheet_id
        if not spreadsheet_id:
            return _error(400, "missing_param", msgs["missing_sheet"])
        try:
            # The validation messages are shared with the demo on purpose: the page only sends valid
            # day/type pairs, so no demo user sees them.
            day, type_ = parse_data.validate_round_params(day, type_)
        except ValueError as e:
            return _error(400, "invalid_param", str(e))
        slugs = parse_data.DEMO_CHARACTER_SLUGS if demo else None
        try:
            result = parse_data.build_round_response(day, type_, spreadsheet_id, character_slugs=slugs)
        except Exception as e:  # noqa: BLE001 — sheet unreachable, tab missing, bad cell value…
            return _error(500, "google_error", msgs["round_failed"] + _describe(e))
        return jsonify(result), 200

    @app.get("/record", endpoint="get_record")
    @auth.require_gm
    def get_record():
        return _find(demo=False)

    @app.post("/record", endpoint="create_record")
    @auth.require_gm
    def create_record():
        return _create(demo=False)

    @app.get("/round", endpoint="round_status")
    @limiter.limit("30 per minute", deduct_when=_counts)
    @auth.require_gm
    def round_status():
        return _round(demo=False)

    # The demo's limits are far below the GMs' so demo traffic plus real games stay inside Google's
    # quota; Drive lookups and Apps Script runs share the owner's account with the real game.
    @app.get("/demo-record", endpoint="get_demo_record")
    @limiter.limit("10 per hour", deduct_when=_counts)
    @auth.require_login
    def get_demo_record():
        return _find(demo=True)

    @app.post("/demo-record", endpoint="create_demo_record")
    @limiter.limit("10 per hour", deduct_when=_counts)
    @auth.require_login
    def create_demo_record():
        return _create(demo=True)

    @app.get("/demo-round", endpoint="demo_round_status")
    @limiter.limit("10 per minute", deduct_when=_counts)
    @auth.require_login
    def demo_round_status():
        return _round(demo=True)


GM_MESSAGES = {
    "missing_datetime": "缺少必要參數 datetime",
    "missing_round": "缺少必要參數 day 或 type",
    "missing_sheet": "缺少必要參數 spreadsheet_id",
    "not_found": "查無此場次檔案",
    "find_failed": "查詢場次檔案時發生錯誤:",
    "create_failed": "建立場次檔案時發生錯誤:",
    "round_failed": "讀取資料時發生錯誤:",
}

DEMO_MESSAGES = {
    "missing_datetime": "Missing required parameter: datetime",
    "missing_round": "Missing required parameter: day or type",
    "missing_sheet": "Missing required parameter: spreadsheet_id",
    "not_found": "No demo session file found for that date/time",
    "find_failed": "Error looking up the demo session file: ",
    "create_failed": "Error creating the demo session file: ",
    "round_failed": "Error loading data: ",
}
