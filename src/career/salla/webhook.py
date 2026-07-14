"""Salla webhook intake — the fast path (whitepaper §09).

Verify signature → compute a stable fingerprint → dedupe-insert into
webhook_events → return quickly. NO heavy work here (no model call, no
provisioning inside the request). Provisioning is a separate worker step.

An invalid signature is rejected without a DB write (so forged requests cannot
drive unbounded inserts). A duplicate (same fingerprint) is a no-op → the caller
still returns 200 so Salla stops retrying.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from career.salla.signature import verify_signature
from career.webhooks.intake import persist_deduped_event


class WebhookStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    INVALID_SIGNATURE = "invalid_signature"


@dataclass(frozen=True)
class WebhookResult:
    status: WebhookStatus
    webhook_event_id: str | None = None
    event_fingerprint: str | None = None


def _fingerprint(provider: str, event_type: str, raw_body: bytes) -> str:
    h = hashlib.sha256()
    h.update(provider.encode("utf-8"))
    h.update(b":")
    h.update(event_type.encode("utf-8"))
    h.update(b":")
    h.update(raw_body)
    return h.hexdigest()


def extract_salla_order_id(payload: dict[str, object]) -> str | None:
    """Salla order webhooks carry the order id at data.id."""
    data = payload.get("data")
    if isinstance(data, dict):
        oid = data.get("id")
        if oid is not None:
            return str(oid)
    return None


def receive_webhook(
    session: Session,
    *,
    provider: str,
    event_type: str,
    raw_body: bytes,
    header_signature: str | None,
    secret: str,
) -> WebhookResult:
    if not verify_signature(raw_body, header_signature, secret):
        return WebhookResult(WebhookStatus.INVALID_SIGNATURE)

    fingerprint = _fingerprint(provider, event_type, raw_body)
    try:
        parsed = json.loads(raw_body)
        payload: dict[str, object] = parsed if isinstance(parsed, dict) else {"_raw": True}
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = {"_unparseable": True}

    order_id = extract_salla_order_id(payload)
    event_id = persist_deduped_event(
        session, provider=provider, event_type=event_type,
        fingerprint=fingerprint, payload=payload, order_id=order_id,
    )
    if event_id is None:
        return WebhookResult(WebhookStatus.DUPLICATE, event_fingerprint=fingerprint)
    return WebhookResult(
        WebhookStatus.ACCEPTED, webhook_event_id=event_id, event_fingerprint=fingerprint
    )
