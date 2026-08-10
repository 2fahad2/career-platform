"""WhatsApp webhook intake — the fast path (whatsapp §08).

Verify the Meta signature → dedupe the whole POST by body hash (identical retries
collapse) → persist to webhook_events → return 200. Per-message idempotency
(wa_message_id) happens later in the worker. No heavy work in-request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from career.webhooks.intake import persist_deduped_event
from career.whatsapp.signature import verify_meta_signature


class WhatsAppIntakeStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    INVALID_SIGNATURE = "invalid_signature"


@dataclass(frozen=True)
class WhatsAppIntakeResult:
    status: WhatsAppIntakeStatus
    webhook_event_id: str | None = None


#: ── THE FIELDS THAT ARE NOT A CONVERSATION ──────────────────────────────────
#:
#: Meta PUSHES these. We never asked: a read-only
#: ``GET /{app_id}/subscriptions`` on 2026-08-08 answered
#: ``object: whatsapp_business_account`` with ``fields: ['messages']`` and
#: nothing else, so every one of the events below has been un-sent rather than
#: dropped. Five approved templates were moved from UTILITY to MARKETING by
#: Meta without us touching them and nobody found out for weeks, because the
#: only field this app is subscribed to is the one that carries customer
#: messages.
#:
#: Naming them HERE, at intake, is what makes them survivable. `event_type` is
#: the only column an operator can query without reading JSONB, it is what
#: `scripts/replay_lost_events.py --event-type` selects on, and it is what
#: separates «processed, nothing to do» from «processed, and we threw it away».
#: The names are Meta's own ``changes[].field`` strings, verbatim — a
#: translation layer here would be one more place for the two to drift, and the
#: column is String(64) while the longest of these is 29 characters.
#:
#: The worker routes exactly this set (`whatsapp.worker.ACCOUNT_EVENT_FIELDS`);
#: subscribing to a field the worker cannot name is how an event becomes a row
#: nobody reads.
ACCOUNT_EVENT_FIELDS: frozenset[str] = frozenset({
    # a template moved between UTILITY and MARKETING — the push that would have
    # caught the five, and the only one that arrives BEFORE the invoice
    "template_category_update",
    # APPROVED / REJECTED / PAUSED / DISABLED / FLAGGED / … — a send path
    # opening or closing (PAUSED is send error 132015, DISABLED is 132016)
    "message_template_status_update",
    # GREEN → YELLOW → RED, which is the warning that precedes a pause
    "message_template_quality_update",
    # the NUMBER's messaging limit / throughput tier
    "phone_number_quality_update",
    # the ACCOUNT: violation, restriction, ban, deletion
    "account_update",
})


def _derive_event_type(payload: dict[str, Any]) -> str:
    """The name this event gets on the row — a hint; the worker re-parses.

    Two passes, in this order and deliberately:

    1. ``messages`` / ``statuses``, exactly as before. They are the hot path,
       they are what `replay_lost_events.SAFE_EVENT_TYPES` is written against,
       and one POST may carry several changes — a customer message in the batch
       must keep naming the row, or a replay would stop finding it.
    2. otherwise the ``changes[].field`` string itself, when it is one Meta
       pushes about the account rather than about a conversation
       (:data:`ACCOUNT_EVENT_FIELDS`).

    ``other`` is what remains, and it now means what it says: a field Meta
    invented since this list was written. It is still stored, still returns
    200, and is still visible — the worker says so out loud rather than
    marking it processed in silence.
    """
    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                if value.get("messages"):
                    return "messages"
                if value.get("statuses"):
                    return "statuses"
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                field = change.get("field")
                if isinstance(field, str) and field in ACCOUNT_EVENT_FIELDS:
                    return field
    except (AttributeError, TypeError):
        pass
    return "other"


def receive_whatsapp_webhook(
    session: Session, *, raw_body: bytes, header_signature: str | None, app_secret: str
) -> WhatsAppIntakeResult:
    if not verify_meta_signature(raw_body, header_signature, app_secret):
        return WhatsAppIntakeResult(WhatsAppIntakeStatus.INVALID_SIGNATURE)

    fingerprint = "wa:" + hashlib.sha256(raw_body).hexdigest()
    try:
        parsed = json.loads(raw_body)
        payload: dict[str, Any] = parsed if isinstance(parsed, dict) else {"_raw": True}
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = {"_unparseable": True}

    event_id = persist_deduped_event(
        session, provider="whatsapp", event_type=_derive_event_type(payload),
        fingerprint=fingerprint, payload=payload,
        # Reached only past the guard above — and the intake will not write a
        # row without being told so (see `persist_deduped_event`).
        signature_valid=True,
    )
    if event_id is None:
        return WhatsAppIntakeResult(WhatsAppIntakeStatus.DUPLICATE)
    return WhatsAppIntakeResult(WhatsAppIntakeStatus.ACCEPTED, webhook_event_id=event_id)
