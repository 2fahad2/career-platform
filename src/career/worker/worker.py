"""Worker skeleton — processes a queue message with the platform invariants.

§15.11: the worker re-verifies tenant ownership from the DB and never trusts the
queue payload. The re-verification rides on RLS itself: work runs inside a
session bound to the message's claimed tenant, and the referenced aggregate is
loaded under that context — a forged cross-tenant reference is simply invisible,
so ownership fails closed.

Idempotency: the message is *claimed* in processed_messages via INSERT ...
ON CONFLICT DO NOTHING before the handler runs. A redelivered message finds the
claim already present and is skipped. If the handler raises, the whole
transaction (claim included) rolls back and the message can be retried.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from career.db.models import ProcessedMessage
from career.db.session import tenant_session
from career.queue.message import InvalidMessageError, QueueMessage

VerifyOwnership = Callable[[Session, QueueMessage], bool]
Handler = Callable[[Session, QueueMessage], None]
SessionScope = Callable[[str], AbstractContextManager[Session]]


class WorkerOutcome(enum.Enum):
    PROCESSED = "processed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    REJECTED_OWNERSHIP = "rejected_ownership"
    INVALID = "invalid"


class _OwnershipRejected(Exception):
    pass


class _DuplicateMessage(Exception):
    pass


def ownership_via_table(table: str, id_column: str = "id") -> VerifyOwnership:
    """Build an ownership checker: the aggregate row is visible under the current
    (RLS-enforced) tenant context. ``table``/``id_column`` are fixed config, not
    user input — but they are validated as identifiers to be safe."""
    if not table.isidentifier() or not id_column.isidentifier():
        raise ValueError(f"unsafe identifier: {table}.{id_column}")

    def _check(session: Session, message: QueueMessage) -> bool:
        row = session.execute(
            text(f"SELECT 1 FROM {table} WHERE {id_column} = :aid"),  # noqa: S608
            {"aid": message.aggregate_id},
        ).first()
        return row is not None

    return _check


def _claim(session: Session, message: QueueMessage) -> bool:
    """Claim the idempotency key. Returns True if newly claimed, False if the
    message was already processed. Uses RETURNING (rather than rowcount, which is
    unreliable for ON CONFLICT DO NOTHING) so a row comes back only on a real
    insert — concurrency-safe."""
    stmt = (
        pg_insert(ProcessedMessage)
        .values(tenant_id=message.tenant_id, idempotency_key=message.idempotency_key)
        .on_conflict_do_nothing(
            constraint="uq_processed_messages_tenant_id_idempotency_key"
        )
        .returning(ProcessedMessage.id)
    )
    return session.execute(stmt).first() is not None


def process_message(
    message: QueueMessage,
    *,
    handler: Handler,
    verify_ownership: VerifyOwnership | None = None,
    session_scope: SessionScope = tenant_session,
) -> WorkerOutcome:
    try:
        message.validate()
    except InvalidMessageError:
        return WorkerOutcome.INVALID

    try:
        with session_scope(message.tenant_id) as session:
            # §15.11 — ownership re-verified from the DB (RLS confines the read).
            if message.aggregate_id is not None and verify_ownership is not None:
                if not verify_ownership(session, message):
                    raise _OwnershipRejected
            # Idempotent claim before doing any work.
            if not _claim(session, message):
                raise _DuplicateMessage
            handler(session, message)
    except _OwnershipRejected:
        return WorkerOutcome.REJECTED_OWNERSHIP
    except _DuplicateMessage:
        return WorkerOutcome.SKIPPED_DUPLICATE
    return WorkerOutcome.PROCESSED


def drain(
    dequeue: Callable[[], QueueMessage | None],
    *,
    handler: Handler,
    verify_ownership: VerifyOwnership | None = None,
    session_scope: SessionScope = tenant_session,
    max_messages: int = 1000,
) -> Iterator[tuple[QueueMessage, WorkerOutcome]]:
    """Process available messages until the queue is empty or the cap is hit."""
    for _ in range(max_messages):
        message = dequeue()
        if message is None:
            return
        outcome = process_message(
            message, handler=handler, verify_ownership=verify_ownership,
            session_scope=session_scope,
        )
        yield message, outcome
