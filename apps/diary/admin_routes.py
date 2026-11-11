"""The admin console's API (/diary/api/admin/...). Adapted from mama-fufood's routes/admin.py.

Every route except /login carries @require_admin — the console's lock is cosmetic, this is the gate.
Writes take only whitelisted fields (each module validates); request bodies never go straight to SQL.
"""

from __future__ import annotations

import json

from flask import Blueprint, g, jsonify, request

from shared import google_auth, payload
from shared.admin_auth import normalize_email
from shared.quota import DailyCounter

from . import app_config, cache, config, db, fonts, github_release
from . import feedback as feedback_db
from .auth import admin, rejections

require_admin = admin.require_admin

bp = Blueprint("diary_admin", __name__, url_prefix="/api/admin")

ADMIN_MAX_BODY = 256 * 1024
# Login has to ask the database "is this an admin?", and any real Google account can try; attempts per
# account per day are capped in memory first so retries can't keep waking the database.
login_attempts = DailyCounter(lambda: 30)


def _error(status: int, code: str, message: str):
    return jsonify({"error": message, "code": code}), status


def _body() -> dict:
    data = payload.read_json(request, ADMIN_MAX_BODY)
    if not isinstance(data, dict):
        raise payload.PayloadError("內容格式不對")
    return data


def _edit(fn, *args):
    with db.connection() as conn:
        with conn.cursor() as cur:
            return fn(cur, *args)


def _reload_cache() -> bool:
    try:
        cache.public.reload()
        return True
    except Exception:  # noqa: BLE001 — already written; the background loader or "reload" fixes it later
        return False


@bp.errorhandler(app_config.ConfigError)
@bp.errorhandler(fonts.FontError)
def _edit_error(exc):
    return _error(400, "invalid", str(exc))


@bp.errorhandler(github_release.UploadError)
def _upload_error(exc):
    return _error(exc.status, "upload_failed", str(exc))


# ── Sign-in ───────────────────────────────────────────────────

@bp.post("/login")
def login():
    """Google sign-in → an admin session. A non-admin gets 403 and nothing is stored."""
    cfg = config.get()
    if not admin.enabled() or not cfg.google_client_id:
        return _error(503, "not_configured", "後端還沒設定登入")
    body = _body()
    claims = google_auth.verify(body.get("credential"), cfg.google_client_id)
    if claims is None:
        rejections.note("admin login: invalid Google token")
        return _error(401, "invalid_token", "Google 憑證無效")
    if not login_attempts.try_take(claims["sub"]):
        rejections.note("admin login: too many")
        return _error(429, "too_many", "登入嘗試太多次，明天再試")
    with db.connection() as conn:
        with conn.cursor() as cur:
            if not admin.admins.is_admin(cur, claims["email"]):
                return _error(403, "forbidden", "這個帳號不是管理員")
            admin.sessions.purge_expired(cur)
            token = admin.sessions.create(cur, claims["email"])
    return jsonify({"token": token, "email": claims["email"]})


@bp.post("/logout")
def logout():
    token = google_auth.extract_bearer_token(request)
    if admin.sessions.looks_valid(token):
        with db.connection() as conn:
            with conn.cursor() as cur:
                admin.sessions.delete(cur, token)
    admin.forget(token)
    return jsonify({"ok": True})


@bp.get("/me")
@require_admin
def me():
    return jsonify({"email": g.admin_email})


# ── Overview ──────────────────────────────────────────────────

@bp.get("/overview")
@require_admin
def overview():
    with db.connection() as conn:
        with conn.cursor() as cur:
            release = fonts.latest_release(cur)
            fb_bytes = feedback_db.table_bytes(cur)
            cur.execute("SELECT count(*) FROM feedback WHERE status = 'new'")
            new_feedback = int(cur.fetchone()[0])
    status = cache.status()
    return jsonify({
        "fonts_version": release["version"] if release else None,
        "cache": status,
        "cache_consistent": status["loaded"] and status["fonts_version"] == (release["version"] if release else None),
        "feedback_table_mb": round(fb_bytes / 1024 / 1024, 2),
        "feedback_table_limit_mb": config.get().feedback_table_limit_mb,
        "new_feedback": new_feedback,
        "fonts_upload_configured": bool(config.get().fonts_repo and config.get().github_token),
    })


@bp.post("/cache/reload")
@require_admin
def reload_cache():
    cache.public.reload()
    return jsonify(cache.status())


# ── Fonts ─────────────────────────────────────────────────────

@bp.get("/fonts")
@require_admin
def list_fonts():
    with db.connection() as conn:
        with conn.cursor() as cur:
            return jsonify({"fonts": fonts.list_drafts(cur), "release": fonts.latest_release(cur)})


@bp.post("/fonts")
@require_admin
def create_font():
    font_id = _edit(fonts.create, _body())
    return jsonify({"id": font_id}), 201


@bp.patch("/fonts/<int:font_id>")
@require_admin
def update_font(font_id: int):
    ok = _edit(fonts.update, font_id, _body())
    return jsonify({"ok": True}) if ok else _error(404, "not_found", "找不到這個字體")


@bp.delete("/fonts/<int:font_id>")
@require_admin
def delete_font(font_id: int):
    _edit(fonts.delete, font_id)
    return jsonify({"ok": True})


@bp.post("/fonts/<int:font_id>/file")
@require_admin
def upload_font_file(font_id: int):
    """Body: the raw .otf.gz from tools/reduce_font.py (Content-Type: application/gzip). Streamed to the
    fonts repository's GitHub Release without being kept in memory (github_release.py)."""
    if _edit(fonts.get, font_id) is None:
        return _error(404, "not_found", "找不到這個字體")
    length = request.content_length or 0
    cfg = config.get()
    info = github_release.upload(request.stream, length, font_id, cfg.fonts_repo, cfg.github_token)
    _edit(fonts.set_file, font_id, info)
    return jsonify(info)


@bp.post("/fonts/publish")
@require_admin
def publish_fonts():
    result = _edit(fonts.publish, g.admin_email)
    result["cache_reloaded"] = _reload_cache()
    return jsonify(result)


# ── App version and forced update ─────────────────────────────

@bp.get("/app-config")
@require_admin
def get_app_config():
    return jsonify(_edit(app_config.get))


@bp.put("/app-config")
@require_admin
def put_app_config():
    _edit(app_config.update, _body(), g.admin_email)
    _reload_cache()
    return jsonify(_edit(app_config.get))


# ── Feedback ──────────────────────────────────────────────────

@bp.get("/feedback")
@require_admin
def list_feedback():
    filters = {k: request.args.get(k, "").strip() for k in ("kind", "status", "app_version", "device_model")}
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1

    def _list(cur):
        feedback_db.purge_old(cur)
        return feedback_db.list_page(cur, filters, page)

    return jsonify(_edit(_list))


@bp.get("/feedback/<uuid:feedback_id>")
@require_admin
def get_feedback(feedback_id):
    item = _edit(feedback_db.get, str(feedback_id))
    if item is None:
        return _error(404, "not_found", "找不到這則意見回饋")
    attachment = item.pop("attachment")
    item["logs"] = None
    if attachment is not None:
        try:
            raw = payload.gunzip_limited(bytes(attachment), 4 * 1024 * 1024)
            try:
                item["logs"] = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                item["logs"] = {"text": raw.decode("utf-8", errors="replace")}
        except payload.PayloadError:
            item["logs"] = {"text": "（記錄檔解不開）"}
    return jsonify(item)


@bp.patch("/feedback/<uuid:feedback_id>")
@require_admin
def update_feedback(feedback_id):
    body = _body()
    try:
        ok = _edit(feedback_db.update, str(feedback_id), body)
    except ValueError as exc:
        return _error(400, "invalid", str(exc))
    return jsonify({"ok": True}) if ok else _error(404, "not_found", "找不到這則意見回饋")


@bp.delete("/feedback/<uuid:feedback_id>")
@require_admin
def delete_feedback(feedback_id):
    ok = _edit(feedback_db.delete, str(feedback_id))
    return jsonify({"ok": True}) if ok else _error(404, "not_found", "找不到這則意見回饋")


@bp.post("/feedback/delete-by-email")
@require_admin
def delete_feedback_by_email():
    """A personal-data deletion request: delete every report from one e-mail address."""
    email = normalize_email(_body().get("email"))
    if email is None:
        return _error(400, "invalid", "Email 格式不對")
    return jsonify({"deleted": _edit(feedback_db.delete_by_email, email)})


# ── Admins ────────────────────────────────────────────────────

@bp.get("/admins")
@require_admin
def list_admins():
    return jsonify({"admins": _edit(admin.admins.list_all)})


@bp.post("/admins")
@require_admin
def add_admin():
    body = _body()
    email = normalize_email(body.get("email"))
    note = body.get("note", "")
    if email is None:
        return _error(400, "invalid", "Email 格式不對")
    if not isinstance(note, str) or len(note) > 100:
        return _error(400, "invalid", "備註要是 100 字以內的文字")
    _edit(admin.admins.add, email, note.strip(), g.admin_email)
    admin.forget_all()
    return jsonify({"ok": True}), 201


@bp.delete("/admins/<path:email>")
@require_admin
def remove_admin(email: str):
    email = normalize_email(email)
    if email is None:
        return _error(400, "invalid", "Email 格式不對")
    if email == config.get().first_admin_email:
        return _error(400, "invalid", "第一位管理員由環境變數設定，不能在這裡移除")
    if email == g.admin_email:
        return _error(400, "invalid", "不能移除自己")

    def _remove(cur):
        removed = admin.admins.remove(cur, email)
        admin.sessions.delete_for_email(cur, email)
        return removed

    removed = _edit(_remove)
    admin.forget_all()
    return jsonify({"ok": True}) if removed else _error(404, "not_found", "這個 Email 不在名單裡")
