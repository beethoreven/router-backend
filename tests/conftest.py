"""Test setup every app shares. Each app keeps its own fixtures in tests/<app>/conftest.py.

- Tests that need Postgres use the `empty_database` fixture: a throwaway database named by
  TEST_DATABASE_URL (locally: `tools/local_db.sh start`). Without it those tests are skipped.
  **Never point it at a real database**: the fixture empties the public schema first.
- Run everything before pushing — every app, not only yours (CLAUDE.md):
      TEST_DATABASE_URL=postgresql://postgres@localhost:5434/amd_test .venv/bin/python -m pytest
"""

from __future__ import annotations

import os

import psycopg
import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


@pytest.fixture
def empty_database() -> str:
    """The test database's URL, with the public schema emptied. Skips when TEST_DATABASE_URL isn't set."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set (tools/local_db.sh start)")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return TEST_DATABASE_URL
