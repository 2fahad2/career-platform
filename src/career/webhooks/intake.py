"""Single webhook-intake authority (LEGACY §13: one module owns each cross-cutting
truth). Both Salla and WhatsApp dedupe-persist their raw events through here, so
the idempotency semantics can never diverge between providers.

Dedupe is by ``event_fingerprint`` (unique). Returns the new row id on a fresh
event, or None if it was already seen. Commits so the intake is durable before
the endpoint returns 200.

Since 0024 this module owns two more things about the same rows, for the same
reason — there is one place they can be true.

**Who the body is about.** The payload is the provider's, verbatim, and it
carries the buyer's name, mobile and email (Salla) or the customer's phone,
profile name and typed text (Meta). Until 0024 nothing connected any of that
to a tenant, so a customer's «حذف بياناتي» deleted their profile, their
messages and their channels and left the raw bodies that contained the same
sentences sitting beside them. The link is resolved HERE, at intake, and not
at processing: a row is personal from the millisecond it lands, and the rows
that are never processed — poisoned, deferred, quarantined — are precisely the
ones that live longest. Resolution that fails is not an error; it writes NULL
and the nightly tries again.

**How long the body lives.** Thirty days, then the payload is replaced by a
PII-free skeleton. The row itself is never deleted, because it is also the
idempotency record: `event_fingerprint` is the whole of what makes a replayed
Salla webhook a no-op, and an order whose fingerprint we had thrown away would
provision the same customer a second time.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent
from career.db.session import tenant_for_phone, tenant_for_salla_order

logger = logging.getLogger("career.webhooks")

#: How long a raw provider body is kept before it is redacted.
#:
#: The body is scaffolding, not the record. Everything the product actually
#: runs on has already been parsed out of it into rows that carry their own
#: tenant and their own retention — inbound_messages, subscriptions,
#: subscription_events. What the raw body is still good for after processing is
#: forensics, and thirty days is drawn from the longest of the three things
#: that actually reach back for one:
#:
#: * the retry budget (0022) exhausts in hours, and `replay_lost_events.py`
#:   refuses to replay anything outbound older than two days by default;
#: * the operator's loop is the weekly report, so «what happened last week»
#:   has to be answerable, and «what happened the week before» usually too;
#: * a customer noticing that a delivery never arrived does so within their
#:   delivery cycle, which is the month they paid for.
#:
#: Ninety days — the §12 figure — was rejected: that promise is about the
#: customer's own data for as long as they are a customer, and stretching it
#: over the provider's raw body would keep the most sensitive copy the longest
#: for the least reason. Seven was rejected because a multi-day outage has
#: already happened once and `replay_lost_events` was how it was recovered.
#:
#: This bounds the LIVE window. The nightly backup is a separate control
#: (constant 14) and redaction cannot reach backwards into one: what changes
#: there is that a body is carried in backups taken during a thirty-day window
#: instead of in every backup ever taken.
RAW_PAYLOAD_RETENTION_DAYS = 30

#: The nightly pass is bounded so that a backlog cannot turn into one enormous
#: transaction holding row locks across the intake path. Whatever it does not
#: reach tonight it reaches tomorrow; the window is thirty days wide and the
#: table takes about three hundred rows a month.
_PRUNE_BATCH = 5_000


def _wa_identifiers(payload: dict[str, Any]) -> list[str]:
    """Every phone number a Meta batch names, in the order Meta wrote them.

    `messages[].from`, `statuses[].recipient_id` and `contacts[].wa_id` are the
    three places a number appears; the worker reads the first two to route and
    the third is what carries the profile name. Meta delivers all of them
    WITHOUT the leading «+» while `customer_channels.phone_e164` may hold
    either spelling (career.whatsapp.phones carries that live bug), so the
    caller tries every variant rather than trusting one.
    """
    found: list[str] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                found.append(message.get("from"))
            for status in value.get("statuses") or []:
                found.append(status.get("recipient_id"))
            for contact in value.get("contacts") or []:
                found.append(contact.get("wa_id"))
    return [f for f in found if isinstance(f, str) and f]


def _whatsapp_subjects(session: Session, payload: dict[str, Any]) -> set[str]:
    from career.whatsapp.phones import phone_variants

    subjects: set[str] = set()
    for raw in _wa_identifiers(payload):
        for variant in phone_variants(raw):
            tenant_id = tenant_for_phone(session, variant)
            if tenant_id is not None:
                subjects.add(tenant_id)
                break
    return subjects


def resolve_subject_tenant(
    session: Session,
    *,
    provider: str,
    payload: dict[str, Any],
    order_id: str | None = None,
) -> str | None:
    """Which tenant this body is about, or None when that has no single answer.

    A Salla order id resolves through the subscription that was created from
    it; a WhatsApp phone resolves through the channel it belongs to. Both go
    through the `app.*` sweep functions rather than a query of our own, because
    those functions are the sanctioned way to learn a tenant id without already
    having one (0021) and they return an id, never a row.

    **Exactly one, or nothing.** A single Meta POST can batch messages from two
    different customers — the worker already loops over `entry[].changes[]` for
    that reason — and stamping such a row with one of them would be wrong in
    both directions at once: that customer's deletion request would erase the
    other customer's forensic record, and their data export would be pointed at
    a body containing somebody else's message. Ambiguity stays NULL and is
    handled by the clock instead of by the link.

    Never raises. Resolution runs inside a SAVEPOINT so that a failure — a
    malformed payload, or a database still at a migration where the `app.*`
    functions do not exist yet — rolls back only itself and leaves the
    surrounding transaction able to do the one thing that actually matters
    here, which is to record the event before the endpoint returns 200.
    """
    try:
        with session.begin_nested():
            if provider == "salla":
                return tenant_for_salla_order(session, order_id) if order_id else None
            if provider == "whatsapp":
                subjects = _whatsapp_subjects(session, payload)
                return subjects.pop() if len(subjects) == 1 else None
            return None
    except Exception:  # noqa: BLE001 — the 200 is worth more than the link
        # No payload, no phone, no order id in the line: this logger is not
        # exempt from §15.13 just because it is reporting a failure.
        logger.warning(
            "webhook intake: could not resolve the subject tenant (provider=%s)",
            provider,
            exc_info=True,
        )
        return None


def persist_deduped_event(
    session: Session,
    *,
    provider: str,
    event_type: str,
    fingerprint: str,
    payload: dict[str, Any],
    order_id: str | None = None,
) -> str | None:
    subject_tenant_id = resolve_subject_tenant(
        session, provider=provider, payload=payload, order_id=order_id
    )
    stmt = (
        pg_insert(WebhookEvent)
        .values(
            provider=provider,
            event_type=event_type,
            event_fingerprint=fingerprint,
            signature_valid=True,
            salla_order_id=order_id,
            subject_tenant_id=subject_tenant_id,
            payload=payload,
            processing_status="received",
        )
        .on_conflict_do_nothing(constraint="uq_webhook_events_event_fingerprint")
        .returning(WebhookEvent.id)
    )
    row = session.execute(stmt).first()
    session.commit()
    return str(row[0]) if row is not None else None


# ── redaction ────────────────────────────────────────────────────────────────


def redacted_payload(payload: Any, *, now: datetime) -> dict[str, Any]:
    """What replaces the body: the shape it had, and nothing it contained.

    The top-level keys are kept because they are the difference between «a
    Meta status batch» and «a message batch», or between a Salla order event
    and an install hook, and that is the question a forensic reader asks
    first. Neither provider puts anything personal at the top level — Meta has
    `object`/`entry`, Salla has `event`/`merchant`/`data` — and nothing below
    it survives.
    """
    shape = sorted(payload) if isinstance(payload, dict) else []
    return {"_redacted": True, "redacted_at": now.isoformat(), "shape": shape}


def _redact(event: WebhookEvent, *, now: datetime) -> None:
    event.payload = redacted_payload(event.payload, now=now)
    event.payload_redacted_at = now


def redact_for_tenant(session: Session, *, tenant_id: Any, now: datetime) -> int:
    """Erase the raw bodies belonging to one tenant, now — the deletion path.

    Only the rows that resolved to exactly one tenant, which is the honest
    reach of this operation. A body that was ambiguous or never resolvable is
    not erased on request; it expires on the retention clock instead, and
    `career.onboarding.privacy` says so to the customer rather than reporting a
    completeness it does not have.

    The row survives, redacted. It is the idempotency record, and `career_app`
    has no DELETE on this table in any case.
    """
    events = session.execute(
        select(WebhookEvent).where(
            WebhookEvent.subject_tenant_id == tenant_id,
            WebhookEvent.payload_redacted_at.is_(None),
        )
    ).scalars().all()
    for event in events:
        _redact(event, now=now)
    session.flush()
    return len(events)


def prune_webhook_payloads(
    session: Session,
    *,
    now: datetime,
    retention_days: int = RAW_PAYLOAD_RETENTION_DAYS,
) -> dict[str, int]:
    """The nightly pass: link what has become linkable, expire what is old.

    Two halves, in this order on purpose. The late link exists because the
    most valuable event of all — the order that pays for a new subscription —
    arrives BEFORE the tenant it creates, so at intake there was nothing to
    resolve against and by tonight there is. Linking before redacting means a
    body that is about to expire is still credited to its customer, which is
    what makes the count in their deletion report honest.

    Runs on the owner session inside the nightly, spans tenants by nature, and
    returns counters rather than sending anything: a per-customer notice about
    housekeeping is noise, and the only number an operator needs to see is the
    one that says something is stuck.
    """
    counts = {"linked": 0, "redacted": 0, "unlinked": 0, "expired_unprocessed": 0}

    unlinked = session.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.subject_tenant_id.is_(None),
            WebhookEvent.payload_redacted_at.is_(None),
        )
        .order_by(WebhookEvent.received_at)
        .limit(_PRUNE_BATCH)
    ).scalars().all()
    for event in unlinked:
        resolved = resolve_subject_tenant(
            session,
            provider=event.provider,
            payload=event.payload if isinstance(event.payload, dict) else {},
            order_id=event.salla_order_id,
        )
        if resolved is None:
            # An install hook, a webhook for an order we refused, a batch
            # carrying two customers, a number that never became a channel.
            # There is no tenant to attribute it to and inventing one would be
            # worse than the clock, which reaches it anyway.
            counts["unlinked"] += 1
            continue
        event.subject_tenant_id = uuid.UUID(resolved)
        counts["linked"] += 1

    cutoff = now - timedelta(days=retention_days)
    expiring = session.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.payload_redacted_at.is_(None),
            WebhookEvent.received_at < cutoff,
        )
        .order_by(WebhookEvent.received_at)
        .limit(_PRUNE_BATCH)
    ).scalars().all()
    for event in expiring:
        if event.processing_status == "received":
            # Still queued after the whole retention window. The retry budget
            # gave up on it weeks ago, so the body is expiring on a row nobody
            # ever finished — the payload goes regardless (it is not more
            # exempt for being stuck) but the operator hears the count, because
            # a silent one of these is a customer whose webhook never landed.
            counts["expired_unprocessed"] += 1
        _redact(event, now=now)
        counts["redacted"] += 1

    session.flush()
    if counts["expired_unprocessed"]:
        logger.warning(
            "webhook prune: %d events were still unprocessed when their raw "
            "body expired after %d days",
            counts["expired_unprocessed"],
            retention_days,
        )
    return counts
