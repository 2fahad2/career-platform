"""Salla webhook intake (DB) — signature, dedupe, fast persist."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.session import SessionLocal
from career.salla.signature import compute_signature
from career.salla.webhook import WebhookStatus, receive_webhook

SECRET = "test_secret_123"


def _body(order_id: str, event: str = "order.payment.updated") -> bytes:
    return json.dumps({"event": event, "data": {"id": order_id}}).encode("utf-8")


def _webhook_row(owner_engine: Engine, fingerprint: str) -> dict[str, object] | None:
    with Session(owner_engine) as s:
        row = s.execute(
            text("SELECT signature_valid, salla_order_id, processing_status"
                 " FROM webhook_events WHERE event_fingerprint = :fp"),
            {"fp": fingerprint},
        ).first()
    return dict(row._mapping) if row else None


def test_valid_webhook_accepted_and_persisted(
    owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    body = _body(order_id)
    sig = compute_signature(body, SECRET)
    session = SessionLocal()
    try:
        result = receive_webhook(
            session, provider="salla", event_type="order.payment.updated",
            raw_body=body, header_signature=sig, secret=SECRET,
        )
    finally:
        session.close()
    assert result.status is WebhookStatus.ACCEPTED
    row = _webhook_row(owner_engine, result.event_fingerprint or "")
    assert row is not None
    assert row["signature_valid"] is True
    assert row["salla_order_id"] == order_id
    assert row["processing_status"] == "received"


def test_duplicate_webhook_deduped(owner_engine: Engine, clean_billing: None) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    body = _body(order_id)
    sig = compute_signature(body, SECRET)

    def once() -> WebhookStatus:
        s = SessionLocal()
        try:
            return receive_webhook(
                s, provider="salla", event_type="order.payment.updated",
                raw_body=body, header_signature=sig, secret=SECRET,
            ).status
        finally:
            s.close()

    assert once() is WebhookStatus.ACCEPTED
    assert once() is WebhookStatus.DUPLICATE  # same body → same fingerprint

    with Session(owner_engine) as s:
        count = s.execute(
            text("SELECT count(*) FROM webhook_events WHERE salla_order_id = :o"),
            {"o": order_id},
        ).scalar_one()
    assert count == 1


def test_invalid_signature_rejected_no_write(
    owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    body = _body(order_id)
    session = SessionLocal()
    try:
        result = receive_webhook(
            session, provider="salla", event_type="order.payment.updated",
            raw_body=body, header_signature="forged", secret=SECRET,
        )
    finally:
        session.close()
    assert result.status is WebhookStatus.INVALID_SIGNATURE
    with Session(owner_engine) as s:
        count = s.execute(
            text("SELECT count(*) FROM webhook_events WHERE salla_order_id = :o"),
            {"o": order_id},
        ).scalar_one()
    assert count == 0  # forged requests never persist
