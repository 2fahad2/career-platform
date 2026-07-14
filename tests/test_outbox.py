"""Transactional outbox tests (DB) — same-txn atomicity, RLS, relay."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.session import tenant_session
from career.queue.adapter import InMemoryQueue
from career.queue.outbox import relay_once, write_outbox_event


def _outbox_count_for(owner_engine: Engine, tenant_id: str) -> int:
    with Session(owner_engine) as s:
        return s.execute(
            text("SELECT count(*) FROM outbox_events WHERE tenant_id = :t"),
            {"t": tenant_id},
        ).scalar_one()


def test_event_and_business_change_commit_together(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        doc_id = str(uuid.uuid4())
        s.execute(
            text(
                "INSERT INTO documents (id, tenant_id, storage_key, content_sha256,"
                " content_type, size_bytes, status)"
                " VALUES (:id,:t,:k,:sha,'application/pdf',1,'active')"
            ),
            {"id": doc_id, "t": a, "k": "tenants/a/d.pdf", "sha": "0" * 64},
        )
        write_outbox_event(
            s, tenant_id=a, aggregate_type="document", aggregate_id=doc_id,
            event_type="document.created", payload={"run_id": "r1"},
        )
    # Both committed.
    assert _outbox_count_for(owner_engine, a) == 1


def test_rollback_discards_event(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, _ = two_tenants
    try:
        with tenant_session(a) as s:
            write_outbox_event(
                s, tenant_id=a, aggregate_type="document",
                event_type="document.created",
            )
            raise RuntimeError("boom before commit")
    except RuntimeError:
        pass
    assert _outbox_count_for(owner_engine, a) == 0


def test_outbox_is_tenant_isolated(two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    with tenant_session(a) as s:
        write_outbox_event(
            s, tenant_id=a, aggregate_type="document", event_type="x",
        )
    # Under B's context, A's outbox event is invisible.
    with tenant_session(b) as s:
        count = s.execute(text("SELECT count(*) FROM outbox_events")).scalar_one()
    assert count == 0


def test_relay_publishes_and_marks_published(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, b = two_tenants
    with tenant_session(a) as s:
        write_outbox_event(s, tenant_id=a, aggregate_type="document", event_type="a.evt")
    with tenant_session(b) as s:
        write_outbox_event(s, tenant_id=b, aggregate_type="document", event_type="b.evt")

    queue = InMemoryQueue()
    with Session(owner_engine) as owner:
        relayed = relay_once(owner, queue)
    assert relayed == 2  # relay spans tenants (owner bypasses RLS)
    assert len(queue) == 2

    # Messages carry each event's tenant and are idempotency-keyed by event id.
    tenants_seen = set()
    while (m := queue.dequeue()) is not None:
        tenants_seen.add(m.tenant_id)
        assert m.idempotency_key  # stable key present
    assert tenants_seen == {a, b}

    # Nothing left unpublished → a second relay is a no-op.
    with Session(owner_engine) as owner:
        assert relay_once(owner, InMemoryQueue()) == 0
