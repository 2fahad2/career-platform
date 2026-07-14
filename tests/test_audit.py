"""Audit writer tests (DB) — tenant isolation, forgery rejection, redaction,
append-only."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from career.audit import recent_audit, record_audit
from career.db.session import tenant_session


def test_audit_written_and_visible(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        record_audit(
            s, tenant_id=a, actor="worker", action="document.published",
            resource_type="document", resource_id=str(uuid.uuid4()),
            correlation_id="corr-1", details={"count": 2},
        )
    with tenant_session(a) as s:
        events = recent_audit(s)
    assert len(events) == 1
    assert events[0].action == "document.published"
    assert events[0].details == {"count": 2}


def test_audit_is_tenant_isolated(two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    with tenant_session(a) as s:
        record_audit(s, tenant_id=a, actor="worker", action="a.only")
    # Under B, A's audit event is invisible.
    with tenant_session(b) as s:
        assert recent_audit(s) == []


def test_forged_tenant_id_rejected(two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    # Under A's context, writing an audit row for tenant B violates WITH CHECK.
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        with tenant_session(a) as s:
            record_audit(s, tenant_id=b, actor="worker", action="forged")
    assert "row-level security" in str(excinfo.value).lower()


def test_details_secret_redacted(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        record_audit(
            s, tenant_id=a, actor="system", action="provider.call",
            details={"api_key": "sk-should-not-persist", "ok": 1},
        )
    with tenant_session(a) as s:
        ev = recent_audit(s)[0]
    assert ev.details["ok"] == 1
    assert ev.details["api_key"] == "[REDACTED]"
    assert "sk-should-not-persist" not in str(ev.details)


def test_append_only_no_update_or_delete(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        ev = record_audit(s, tenant_id=a, actor="worker", action="immutable")
        eid = str(ev.id)
    # The app role has INSERT/SELECT only — UPDATE and DELETE are denied.
    with pytest.raises(DatabaseError):
        with tenant_session(a) as s:
            s.execute(
                text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
                {"id": eid},
            )
    with pytest.raises(DatabaseError):
        with tenant_session(a) as s:
            s.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": eid})
