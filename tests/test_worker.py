"""Worker tests (DB) — the §15.11 ownership guarantee + idempotency.

The worker re-verifies tenant ownership from the DB via RLS: a message that
forges another tenant's aggregate is rejected because that row is invisible
under the claimed tenant's context.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.session import tenant_session
from career.queue.message import QueueMessage
from career.worker.worker import (
    WorkerOutcome,
    ownership_via_table,
    process_message,
)


def _make_document(tenant_id: str) -> str:
    doc_id = str(uuid.uuid4())
    with tenant_session(tenant_id) as s:
        s.execute(
            text(
                "INSERT INTO documents (id, tenant_id, storage_key, content_sha256,"
                " content_type, size_bytes, status)"
                " VALUES (:id,:t,:k,:sha,'application/pdf',1,'active')"
            ),
            {"id": doc_id, "t": tenant_id, "k": f"tenants/{tenant_id}/{doc_id}.pdf",
             "sha": "0" * 64},
        )
    return doc_id


def _msg(tenant_id: str, aggregate_id: str | None, key: str) -> QueueMessage:
    return QueueMessage(
        tenant_id=tenant_id, task_type="process_document", run_id="r",
        correlation_id="c", idempotency_key=key,
        aggregate_type="document", aggregate_id=aggregate_id,
    )


def _processed_count(owner_engine: Engine, tenant_id: str, key: str) -> int:
    with Session(owner_engine) as s:
        return s.execute(
            text("SELECT count(*) FROM processed_messages WHERE tenant_id = :t"
                 " AND idempotency_key = :k"),
            {"t": tenant_id, "k": key},
        ).scalar_one()


def test_happy_path_processes_once(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, _ = two_tenants
    doc = _make_document(a)
    ran: list[str] = []
    outcome = process_message(
        _msg(a, doc, "k-happy"),
        handler=lambda s, m: ran.append(m.idempotency_key),
        verify_ownership=ownership_via_table("documents"),
    )
    assert outcome is WorkerOutcome.PROCESSED
    assert ran == ["k-happy"]
    assert _processed_count(owner_engine, a, "k-happy") == 1


def test_forged_cross_tenant_reference_rejected(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, b = two_tenants
    doc_b = _make_document(b)  # owned by B
    ran: list[str] = []
    # Message claims tenant A but references B's document.
    outcome = process_message(
        _msg(a, doc_b, "k-forged"),
        handler=lambda s, m: ran.append(m.idempotency_key),
        verify_ownership=ownership_via_table("documents"),
    )
    assert outcome is WorkerOutcome.REJECTED_OWNERSHIP
    assert ran == []  # handler never ran
    assert _processed_count(owner_engine, a, "k-forged") == 0  # nothing claimed


def test_redelivery_is_idempotent(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, _ = two_tenants
    doc = _make_document(a)
    ran: list[str] = []
    checker = ownership_via_table("documents")
    first = process_message(
        _msg(a, doc, "k-dup"), handler=lambda s, m: ran.append("x"),
        verify_ownership=checker,
    )
    second = process_message(
        _msg(a, doc, "k-dup"), handler=lambda s, m: ran.append("x"),
        verify_ownership=checker,
    )
    assert first is WorkerOutcome.PROCESSED
    assert second is WorkerOutcome.SKIPPED_DUPLICATE
    assert ran == ["x"]  # handler ran exactly once
    assert _processed_count(owner_engine, a, "k-dup") == 1


def test_invalid_message_rejected(two_tenants: tuple[str, str]) -> None:
    bad = QueueMessage(
        tenant_id="not-a-uuid", task_type="t", run_id="r",
        correlation_id="c", idempotency_key="k",
    )
    ran: list[str] = []
    outcome = process_message(bad, handler=lambda s, m: ran.append("x"))
    assert outcome is WorkerOutcome.INVALID
    assert ran == []


def test_handler_failure_rolls_back_claim(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, _ = two_tenants
    doc = _make_document(a)

    def boom(_s: object, _m: object) -> None:
        raise RuntimeError("handler exploded")

    try:
        process_message(
            _msg(a, doc, "k-fail"), handler=boom,
            verify_ownership=ownership_via_table("documents"),
        )
    except RuntimeError:
        pass
    # Claim rolled back → the message can be retried.
    assert _processed_count(owner_engine, a, "k-fail") == 0
