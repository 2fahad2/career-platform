"""Audit writer tests (DB) — tenant isolation, forgery rejection, redaction,
append-only, and the closed action vocabulary.

The vocabulary test is not style enforcement. `audit_events` is one of the six
tables that survive a customer's «حذف بياناتي» (onboarding.privacy
RETAINED_TABLES), so every row in it has to be a security, money or privacy
event we could defend keeping. The registry is what makes that structural
instead of a habit.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from career.audit import (
    ACTION_ACTIVATION_LINK_ISSUED,
    ACTION_CV_PAIR_QUARANTINED,
    AUDIT_ACTIONS,
    AuditCategory,
    UnregisteredAuditAction,
    recent_audit,
    record_audit,
)
from career.db.session import tenant_session


def test_audit_written_and_visible(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        record_audit(
            s, tenant_id=a, actor="nightly", action=ACTION_CV_PAIR_QUARANTINED,
            resource_type="tailored_cv", resource_id=str(uuid.uuid4()),
            correlation_id="corr-1", details={"blockers": ["sha256_mismatch"]},
        )
    with tenant_session(a) as s:
        events = recent_audit(s)
    assert len(events) == 1
    assert events[0].action == ACTION_CV_PAIR_QUARANTINED
    assert events[0].details == {"blockers": ["sha256_mismatch"]}


def test_audit_is_tenant_isolated(two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    with tenant_session(a) as s:
        record_audit(s, tenant_id=a, actor="operator",
                     action=ACTION_ACTIVATION_LINK_ISSUED)
    # Under B, A's audit event is invisible.
    with tenant_session(b) as s:
        assert recent_audit(s) == []


def test_forged_tenant_id_rejected(two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    # Under A's context, writing an audit row for tenant B violates WITH CHECK.
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        with tenant_session(a) as s:
            record_audit(s, tenant_id=b, actor="operator",
                         action=ACTION_ACTIVATION_LINK_ISSUED)
    assert "row-level security" in str(excinfo.value).lower()


def test_details_secret_redacted(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        record_audit(
            s, tenant_id=a, actor="operator",
            action=ACTION_ACTIVATION_LINK_ISSUED,
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
        ev = record_audit(s, tenant_id=a, actor="operator",
                          action=ACTION_ACTIVATION_LINK_ISSUED)
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


# ── the closed vocabulary ────────────────────────────────────────────────────


def test_an_unregistered_action_is_refused_before_it_reaches_the_table(
    two_tenants: tuple[str, str],
) -> None:
    """The failure mode this prevents is not a crash, it is a slow drift: one
    `worker.started` here, one `poll.tick` there, and within a quarter the
    table nobody may delete is mostly noise nobody reads — which is also how
    the privacy exemption stops being defensible."""
    a, _ = two_tenants
    with pytest.raises(UnregisteredAuditAction) as excinfo:
        with tenant_session(a) as s:
            record_audit(s, tenant_id=a, actor="worker", action="poll.tick")
    # the message must tell the next author both of their options
    assert "AUDIT_ACTIONS" in str(excinfo.value)
    assert "journal" in str(excinfo.value)
    with tenant_session(a) as s:
        assert recent_audit(s) == []


def test_every_registered_action_carries_one_of_the_three_categories() -> None:
    """Security, money, privacy — nothing else earns retention through a
    deletion request."""
    assert AUDIT_ACTIONS, "the registry is empty — nothing writes audit rows"
    for action, category in AUDIT_ACTIONS.items():
        assert isinstance(category, AuditCategory), action


def test_the_registry_holds_only_actions_something_actually_emits() -> None:
    """A registered action with no caller is the same vacuum as an unwired
    module, one layer down: it reads as a control that exists. An emitter
    names the action either through the module constant or as the literal
    (salla.provisioning passes the literal), so both spellings count.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "career"
    body = "\n".join(
        p.read_text(encoding="utf-8") for p in src.rglob("*.py")
        if p.name != "audit.py"
    )
    for action in AUDIT_ACTIONS:
        assert action in body or f"ACTION_{action.upper()}" in body, (
            f"{action} is registered but nothing in src/career emits it — "
            "either wire it or take it out of the registry"
        )
