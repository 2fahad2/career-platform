"""Transactional outbox helpers.

``write_outbox_event`` runs inside the caller's transaction so the event and the
business state change commit or roll back together — no lost or phantom events.
The relay runs as the owner role (bypasses RLS) to publish events across all
tenants, then stamps ``published_at``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from career.db.models import OutboxEvent
from career.queue.adapter import Queue
from career.queue.message import QueueMessage


def write_outbox_event(
    session: Session,
    *,
    tenant_id: str | uuid.UUID,
    aggregate_type: str,
    event_type: str,
    aggregate_id: str | uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> OutboxEvent:
    """Insert an outbox event in the CURRENT transaction (caller commits)."""
    event = OutboxEvent(
        tenant_id=uuid.UUID(str(tenant_id)),
        aggregate_type=aggregate_type,
        aggregate_id=uuid.UUID(str(aggregate_id)) if aggregate_id is not None else None,
        event_type=event_type,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event


def fetch_unpublished(owner_session: Session, *, limit: int = 100) -> list[OutboxEvent]:
    """Relay read across all tenants (owner role bypasses RLS)."""
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.created_at)
        .limit(limit)
    )
    return list(owner_session.execute(stmt).scalars().all())


def mark_published(owner_session: Session, event_ids: list[uuid.UUID]) -> None:
    if not event_ids:
        return
    owner_session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(event_ids))
        .values(published_at=func.now())
    )


def _event_to_message(event: OutboxEvent) -> QueueMessage:
    payload = dict(event.payload or {})
    eid = str(event.id)
    return QueueMessage(
        tenant_id=str(event.tenant_id),
        task_type=event.event_type,
        run_id=str(payload.get("run_id", eid)),
        correlation_id=str(payload.get("correlation_id", eid)),
        idempotency_key=eid,  # the event id is a stable idempotency key
        aggregate_type=event.aggregate_type,
        aggregate_id=str(event.aggregate_id) if event.aggregate_id else None,
        payload=payload,
    )


def relay_once(owner_session: Session, queue: Queue, *, limit: int = 100) -> int:
    """Publish unpublished events to the queue and mark them published, in one
    transaction. Returns the number relayed. The queue publish happens before
    the mark; a crash between them re-publishes (at-least-once) — consumers are
    idempotent by idempotency_key, so that is safe."""
    events = fetch_unpublished(owner_session, limit=limit)
    if not events:
        return 0
    for event in events:
        queue.enqueue(_event_to_message(event))
    mark_published(owner_session, [event.id for event in events])
    owner_session.commit()
    return len(events)
