"""Single webhook-intake authority (LEGACY §13: one module owns each cross-cutting
truth). Both Salla and WhatsApp dedupe-persist their raw events through here, so
the idempotency semantics can never diverge between providers.

Dedupe is by ``event_fingerprint`` (unique). Returns the new row id on a fresh
event, or None if it was already seen. Commits so the intake is durable before
the endpoint returns 200.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent


def persist_deduped_event(
    session: Session,
    *,
    provider: str,
    event_type: str,
    fingerprint: str,
    payload: dict[str, Any],
    order_id: str | None = None,
) -> str | None:
    stmt = (
        pg_insert(WebhookEvent)
        .values(
            provider=provider,
            event_type=event_type,
            event_fingerprint=fingerprint,
            signature_valid=True,
            salla_order_id=order_id,
            payload=payload,
            processing_status="received",
        )
        .on_conflict_do_nothing(constraint="uq_webhook_events_event_fingerprint")
        .returning(WebhookEvent.id)
    )
    row = session.execute(stmt).first()
    session.commit()
    return str(row[0]) if row is not None else None
