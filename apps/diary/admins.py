"""管理員白名單(SPEC 第 3 節「管理後台功能」:用白名單管理,預計 1~2 人)。"""

from __future__ import annotations

import re

from . import config

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize(email: object) -> str | None:
    if not isinstance(email, str):
        return None
    email = email.strip().lower()
    return email if _EMAIL.match(email) and len(email) <= 254 else None


def is_admin(cur, email: str | None) -> bool:
    if not email:
        return False
    if config.get().first_admin_email and email == config.get().first_admin_email:
        return True
    cur.execute("SELECT 1 FROM admins WHERE email = %s", (email,))
    return cur.fetchone() is not None


def list_all(cur) -> list[dict]:
    cur.execute("SELECT email, note, created_at, created_by FROM admins ORDER BY created_at")
    rows = [{"email": e, "note": n, "created_at": c.isoformat(), "created_by": b, "fixed": False}
            for e, n, c, b in cur.fetchall()]
    if config.get().first_admin_email and all(r["email"] != config.get().first_admin_email for r in rows):
        rows.insert(0, {"email": config.get().first_admin_email, "note": "第一位管理員（環境變數）",
                        "created_at": None, "created_by": "", "fixed": True})
    return rows


def add(cur, email: str, note: str, by: str) -> None:
    cur.execute(
        "INSERT INTO admins (email, note, created_by) VALUES (%s, %s, %s)"
        " ON CONFLICT (email) DO UPDATE SET note = EXCLUDED.note",
        (email, note, by),
    )


def remove(cur, email: str) -> bool:
    cur.execute("DELETE FROM admins WHERE email = %s", (email,))
    return cur.rowcount > 0
