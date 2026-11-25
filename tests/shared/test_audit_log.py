"""shared.audit_log without an app around it."""

from __future__ import annotations

import pytest

from shared import audit_log


def test_table_names_are_checked():
    with pytest.raises(ValueError):
        audit_log.schema_sql("x; DROP TABLE y")
    with pytest.raises(ValueError):
        audit_log.AuditLog(database=None, table="Bad-Name")


def test_result_mapping():
    assert [audit_log._result(s) for s in (200, 201, 400, 401, 403, 404, 429, 500)] == [
        "ok", "ok", "failed", "denied", "denied", "failed", "failed", "failed"]


def test_record_rejects_unknown_results():
    log = audit_log.AuditLog(database=None)
    with pytest.raises(ValueError):
        log.record(None, actor="a", action="x", result="maybe")
