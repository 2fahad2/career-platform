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


def _derive_event_type(payload: dict[str, Any]) -> str:
    """messages / statuses / other — a hint; the worker re-parses regardless."""
    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                if value.get("messages"):
                    return "messages"
                if value.get("statuses"):
                    return "statuses"
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
    )
    if event_id is None:
        return WhatsAppIntakeResult(WhatsAppIntakeStatus.DUPLICATE)
    return WhatsAppIntakeResult(WhatsAppIntakeStatus.ACCEPTED, webhook_event_id=event_id)
