"""Deep-audit round-2 fixes: poisoned-event isolation, resume guard,
standing privacy commands after ACTIVE, atomic ledger savepoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent
from career.onboarding import privacy
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.worker import process_pending_whatsapp

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def _tenant_with_channel(owner_session: Session):
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("279.00"), "SAR")
    })
    token = provision_order(owner_session, order_id, salla_client=client,
                            product_catalog={"prod_pro": "professional"}
                            ).activation_token
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    activate(owner_session, token=token, from_phone=phone, display_name=None,
             now=NOW, whatsapp_client=FakeWhatsAppClient(),
             admin_client=FakeTelegramAdminClient())
    from sqlalchemy import select

    from career.db.models import CustomerChannel
    ch = owner_session.execute(
        select(CustomerChannel).where(CustomerChannel.phone_e164 == phone)
    ).scalar_one()
    return ch


def _wa_event(owner_session: Session, tenant_seed: str, msg: dict) -> WebhookEvent:
    ev = WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="messages",
        event_fingerprint=f"fp-{uuid.uuid4()}",
        payload={"entry": [{"changes": [{"value": {"messages": [msg]}}]}]},
        processing_status="received", signature_valid=True,
    )
    owner_session.add(ev)
    owner_session.commit()
    return ev


def test_poisoned_event_is_isolated_not_retried_forever(
    owner_session: Session, clean_billing: None
) -> None:
    ch = _tenant_with_channel(owner_session)
    poison = {"id": f"wamid.{uuid.uuid4().hex}", "from": ch.phone_e164,
              "type": "text", "text": None}          # text=None → attribute crash
    good = {"id": f"wamid.{uuid.uuid4().hex}", "from": ch.phone_e164,
            "type": "text", "text": {"body": "مرحبا"}}
    ev_bad = _wa_event(owner_session, "a", poison)
    ev_good = _wa_event(owner_session, "b", good)
    counts = process_pending_whatsapp(
        owner_session, whatsapp_client=FakeWhatsAppClient(),
        admin_client=FakeTelegramAdminClient(), now=NOW,
    )
    owner_session.commit()
    statuses = {
        str(row[0]): row[1] for row in owner_session.execute(sql_text(
            "SELECT id, processing_status FROM webhook_events"
            " WHERE id IN (:a, :b)"), {"a": str(ev_bad.id), "b": str(ev_good.id)}
        ).all()
    }
    assert statuses[str(ev_bad.id)] == "failed"       # isolated, not 'received'
    assert statuses[str(ev_good.id)] == "processed"   # sibling unharmed
    assert counts["failed"] == 1
    owner_session.execute(sql_text(
        "DELETE FROM webhook_events WHERE id IN (:a,:b)"),
        {"a": str(ev_bad.id), "b": str(ev_good.id)})
    owner_session.commit()


def test_resume_before_pause_is_a_noop(
    owner_session: Session, clean_billing: None
) -> None:
    try:
        ch = _tenant_with_channel(owner_session)
        sub = privacy.resume_subscription(owner_session, tenant_id=ch.tenant_id)
        assert sub.status == "ONBOARDING"             # untouched — no corruption
        privacy.pause_subscription(owner_session, tenant_id=ch.tenant_id)
        sub = privacy.resume_subscription(owner_session, tenant_id=ch.tenant_id)
        assert sub.status == "ACTIVE"                 # real resume still works
    finally:
        # open transactions wedge the clean_billing teardown (repo lesson)
        owner_session.rollback()
