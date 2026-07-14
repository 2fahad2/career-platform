"""Central audit writer.

One place records audit events, so the PII/secret discipline is enforced in a
single spot: details are passed through the secret-redaction sanitizer before
storage (defense in depth — details are already PII-free by contract). Writes
happen inside the caller's tenant-bound transaction; RLS confines each row to
its tenant, and the app role has INSERT/SELECT only (append-only).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import AuditEvent
from career.logging_filters import sanitize_secret_value


def record_audit(
    session: Session,
    *,
    tenant_id: str | uuid.UUID,
    actor: str,
    action: str,
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    correlation_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event in the CURRENT transaction. ``tenant_id`` must match
    the session's tenant context or RLS (WITH CHECK) rejects the write."""
    safe_details = sanitize_secret_value(details or {})
    event = AuditEvent(
        tenant_id=uuid.UUID(str(tenant_id)),
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=uuid.UUID(str(resource_id)) if resource_id is not None else None,
        correlation_id=correlation_id,
        details=safe_details,
    )
    session.add(event)
    session.flush()
    return event


def recent_audit(session: Session, *, limit: int = 100) -> list[AuditEvent]:
    """Return the current tenant's most recent audit events (RLS-confined)."""
    stmt = (
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())
