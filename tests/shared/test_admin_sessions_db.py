"""Admin sessions against a real Postgres: only the token's hash is stored, and an old table that still
holds plain tokens is emptied and moved over once."""

from __future__ import annotations

import hashlib

import psycopg

from shared.admin_auth import AdminSessions, sessions_schema_sql, upgrade_sessions_sql


def _sessions() -> AdminSessions:
    return AdminSessions(purpose=b"test:admin-session:v1", secret=lambda: "s", idle_days=lambda: 90,
                         table="admin_sessions")


def _schema(cur) -> None:
    cur.execute(upgrade_sessions_sql("admin_sessions"))
    cur.execute(sessions_schema_sql("admin_sessions"))


def test_the_table_never_holds_the_token(empty_database):
    sessions = _sessions()
    with psycopg.connect(empty_database) as conn, conn.cursor() as cur:
        _schema(cur)
        token = sessions.create(cur, "Admin@Example.com")
        cur.execute("SELECT token_hash, email FROM admin_sessions")
        assert cur.fetchall() == [(hashlib.sha256(token.encode()).hexdigest(), "admin@example.com")]
        assert sessions.resolve(cur, token) == "admin@example.com"
        # A copy of the table is not a way in: the stored value itself is not a token we accept.
        cur.execute("SELECT token_hash FROM admin_sessions")
        assert sessions.resolve(cur, cur.fetchone()[0]) is None
        sessions.delete(cur, token)
        assert sessions.resolve(cur, token) is None


def test_an_old_table_with_plain_tokens_is_emptied_and_renamed_once(empty_database):
    sessions = _sessions()
    with psycopg.connect(empty_database) as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE admin_sessions (
            token TEXT PRIMARY KEY, email TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        old = "0123456789abcdef0123456789abcdef01234567" + sessions._tag("0123456789abcdef0123456789abcdef01234567")
        cur.execute("INSERT INTO admin_sessions (token, email) VALUES (%s, 'a@b.c')", (old,))
        _schema(cur)
        cur.execute("SELECT count(*) FROM admin_sessions")
        assert cur.fetchone()[0] == 0, "plain tokens don't survive the upgrade"
        assert sessions.resolve(cur, old) is None
        new = sessions.create(cur, "a@b.c")
        _schema(cur)  # every later start: nothing happens
        assert sessions.resolve(cur, new) == "a@b.c"
