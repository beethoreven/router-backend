"""管理後台的 API(SPEC 第 3 節「管理後台功能」)。

除了 /api/admin/login,每一支都掛 require_admin。寫入一律只取白名單欄位
(datasets.py、app_config.py、feedback.py),不把請求內容直接交給資料庫。
登入、session 與管理員名單是 shared.admin_auth(auth.admin)。
"""

from __future__ import annotations

import json

from flask import Blueprint, g, jsonify, request

from shared import google_auth, payload
from shared.admin_auth import normalize_email
from shared.quota import DailyCounter

from . import app_config, cache, config, datasets, db
from . import feedback as feedback_db
from .auth import admin, rejections

require_admin = admin.require_admin

bp = Blueprint("admin", __name__, url_prefix="/api/admin")

ADMIN_MAX_BODY = 256 * 1024
# 後台登入要查資料庫(是不是管理員);任何真的 Google 帳號都能來試,所以先在記憶體限制
# 每個帳號每天的嘗試次數,超過就不碰資料庫(有效的 Google 帳號一直重試,也不會一直叫醒資料庫)。
login_attempts = DailyCounter(lambda: 30)


def _error(status: int, code: str, message: str):
    return jsonify({"error": message, "code": code}), status


def _body() -> dict:
    data = payload.read_json(request, ADMIN_MAX_BODY)
    if not isinstance(data, dict):
        raise payload.PayloadError("內容格式不對")
    return data


@bp.errorhandler(payload.PayloadError)
def _payload_error(exc):
    return _error(exc.status, exc.code, str(exc))


@bp.errorhandler(datasets.EditError)
@bp.errorhandler(app_config.ConfigError)
def _edit_error(exc):
    return _error(400, "invalid", str(exc))


# ── 登入 ──────────────────────────────────────────────────────

@bp.post("/login")
def login():
    """用 Google 登入換一張後台 session。不是管理員回 403,而且什麼都不存
    (一般使用者在網站登入沒有作用,SPEC 第 13 節「網站」)。"""
    if not admin.enabled():
        return _error(503, "not_configured", "後端還沒設定 SESSION_SECRET")
    body = _body()
    claims = google_auth.verify(body.get("credential"), config.get().google_client_id)
    if claims is None:
        rejections.note("後台登入憑證無效")
        return _error(401, "invalid_token", "Google 憑證無效")
    if not login_attempts.try_take(claims["sub"]):
        rejections.note("後台登入嘗試過多")
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


# ── 公用資料 ──────────────────────────────────────────────────

@bp.get("/overview")
@require_admin
def overview():
    with db.connection() as conn:
        with conn.cursor() as cur:
            draft = datasets.draft(cur)
            releases = {}
            for name in datasets.DATASETS:
                latest = datasets.latest(cur, name)
                releases[name] = {
                    "version": latest["version"] if latest else None,
                    "published_at": latest["published_at"] if latest else None,
                    "published_by": latest["published_by"] if latest else None,
                    "has_changes": datasets.has_changes(cur, name),
                    "history": datasets.history(cur, name),
                }
            fb_bytes = feedback_db.table_bytes(cur)
    cache_status = cache.status()
    db_versions = {name: info["version"] for name, info in releases.items()}
    return jsonify({
        "draft": draft,
        "releases": releases,
        "cache": cache_status,
        "cache_consistent": cache_status["loaded"] and cache_status["datasets"] == {k: v for k, v in db_versions.items() if v},
        "feedback_table_mb": round(fb_bytes / 1024 / 1024, 2),
        "feedback_table_limit_mb": config.get().feedback_table_limit_mb,
    })


def _edit(fn, *args):
    with db.connection() as conn:
        with conn.cursor() as cur:
            result = fn(cur, *args)
    return result


@bp.post("/groups")
@require_admin
def create_group():
    return jsonify({"id": _edit(datasets.create_group, _body())}), 201


@bp.patch("/groups/<int:item_id>")
@require_admin
def update_group(item_id: int):
    _edit(datasets.update_group, item_id, _body())
    return jsonify({"ok": True})


@bp.delete("/groups/<int:item_id>")
@require_admin
def delete_group(item_id: int):
    _edit(datasets.delete_group, item_id)
    return jsonify({"ok": True})


@bp.post("/foods")
@require_admin
def create_food():
    return jsonify({"id": _edit(datasets.create_food, _body())}), 201


@bp.patch("/foods/<int:item_id>")
@require_admin
def update_food(item_id: int):
    _edit(datasets.update_food, item_id, _body())
    return jsonify({"ok": True})


@bp.delete("/foods/<int:item_id>")
@require_admin
def delete_food(item_id: int):
    _edit(datasets.delete_food, item_id)
    return jsonify({"ok": True})


@bp.post("/units")
@require_admin
def create_unit():
    return jsonify({"id": _edit(datasets.create_unit, _body())}), 201


@bp.patch("/units/<int:item_id>")
@require_admin
def update_unit(item_id: int):
    _edit(datasets.update_unit, item_id, _body())
    return jsonify({"ok": True})


@bp.delete("/units/<int:item_id>")
@require_admin
def delete_unit(item_id: int):
    _edit(datasets.delete_unit, item_id)
    return jsonify({"ok": True})


@bp.post("/publish/<dataset>")
@require_admin
def publish(dataset: str):
    """發布草稿。先寫資料庫(交易成功),再從資料庫重新載入快取。"""
    version = _edit(datasets.publish, dataset, g.admin_email)
    try:
        cache.public.reload()
        cache_ok = True
    except Exception:  # noqa: BLE001 - 資料已經發布;快取晚點由背景或「重新載入快取」補上
        cache_ok = False
    return jsonify({"version": version, "cache_reloaded": cache_ok})


@bp.post("/cache/reload")
@require_admin
def reload_cache():
    cache.public.reload()
    return jsonify(cache.status())


# ── App 版本與強制更新 ────────────────────────────────────────

@bp.get("/app-config")
@require_admin
def get_app_config():
    with db.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(app_config.get(cur))


@bp.put("/app-config")
@require_admin
def put_app_config():
    _edit(app_config.update, _body(), g.admin_email)
    try:
        cache.public.reload()
    except Exception:  # noqa: BLE001 - 已寫入資料庫;快取晚點補上
        pass
    with db.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(app_config.get(cur))


@bp.get("/formats")
@require_admin
def list_formats():
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT format_version, definition, status, min_app_version, updated_at FROM formats"
                        " ORDER BY format_version")
            rows = [{"format_version": v, "definition": d, "status": s, "min_app_version": m,
                     "updated_at": u.isoformat()} for v, d, s, m, u in cur.fetchall()]
    return jsonify({"formats": rows})


# ── 意見回饋 ──────────────────────────────────────────────────

@bp.get("/feedback")
@require_admin
def list_feedback():
    filters = {k: request.args.get(k, "").strip() for k in ("kind", "status", "app_version", "device_model")}
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    with db.connection() as conn:
        with conn.cursor() as cur:
            feedback_db.purge_old(cur)
            return jsonify(feedback_db.list_page(cur, filters, page))


@bp.get("/feedback/<uuid:feedback_id>")
@require_admin
def get_feedback(feedback_id):
    with db.connection() as conn:
        with conn.cursor() as cur:
            item = feedback_db.get(cur, str(feedback_id))
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
    """個資刪除請求:刪掉某個 Email 的所有意見回饋。"""
    email = normalize_email(_body().get("email"))
    if email is None:
        return _error(400, "invalid", "Email 格式不對")
    return jsonify({"deleted": _edit(feedback_db.delete_by_email, email)})


# ── 管理員 ────────────────────────────────────────────────────

@bp.get("/admins")
@require_admin
def list_admins():
    with db.connection() as conn:
        with conn.cursor() as cur:
            return jsonify({"admins": admin.admins.list_all(cur)})


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
