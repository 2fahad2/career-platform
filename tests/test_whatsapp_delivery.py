"""Adaptive delivery (DB) — open→direct, closed→template-then-descend, opted-out→none."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, DeliveryMessage
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.delivery import (
    DELIVERY_COMPLETED,
    DELIVERY_NO_SEND,
    DELIVERY_PENDING,
    deliver_adaptive,
    descend_pending_delivery,
)
from career.whatsapp.templates import DAILY_UTILITY

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
CATALOG = {"prod_pro": "professional"}
_PR = {k: (Decimal("279.00"), "SAR") for k in CATALOG}
BUNDLE = {"parts": [
    {"kind": "text", "body": "#1 IT Operations Manager @ NEOM"},
    {"kind": "document", "ref": "tenants/x/cv1.pdf", "filename": "CV.pdf", "caption": "CV #1"},
]}


def _channel(owner_session: Session, *, last_inbound_at: datetime | None, opt_out: bool = False
             ) -> CustomerChannel:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR")
    })
    token = provision_order(owner_session, order_id, salla_client=client,
                            product_catalog=CATALOG,
                            expected_pricing=_PR).activation_token
    assert token is not None
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    activate(owner_session, token=token, from_phone=phone, display_name=None, now=NOW,
             whatsapp_client=FakeWhatsAppClient(), admin_client=FakeTelegramAdminClient())
    ch = owner_session.execute(
        select(CustomerChannel).where(CustomerChannel.phone_e164 == phone)
    ).scalar_one()
    ch.last_inbound_at = last_inbound_at
    if opt_out:
        ch.opt_out_at = NOW
    owner_session.commit()
    return ch


def _dm_count(owner_engine: Engine, delivery_id: uuid.UUID, kind: str) -> int:
    with Session(owner_engine) as s:
        return len(s.execute(
            select(DeliveryMessage).where(
                DeliveryMessage.delivery_id == delivery_id, DeliveryMessage.kind == kind
            )
        ).scalars().all())


def test_open_window_sends_directly(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    ch = _channel(owner_session, last_inbound_at=NOW)  # open
    wa = FakeWhatsAppClient()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()
    assert delivery.status == DELIVERY_COMPLETED
    kinds = [m.kind for m in wa.sent]
    assert kinds == ["text", "document"]  # bundle parts sent directly
    assert _dm_count(owner_engine, delivery.id, "text") == 1
    assert _dm_count(owner_engine, delivery.id, "document") == 1


def test_closed_window_sends_template_then_descends(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))  # closed
    wa = FakeWhatsAppClient()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()
    # Only the morning template goes out; the bundle is held.
    assert delivery.status == DELIVERY_PENDING
    assert [m.kind for m in wa.sent] == ["template"]
    assert delivery.template_message_id is not None

    # Customer taps → window opens → descend the held bundle.
    later = NOW + timedelta(minutes=5)
    ch.last_inbound_at = later
    descended = descend_pending_delivery(owner_session, ch, whatsapp_client=wa, now=later)
    owner_session.commit()
    assert descended is not None
    assert descended.status == DELIVERY_COMPLETED
    assert [m.kind for m in wa.sent] == ["template", "text", "document"]


def test_opted_out_sends_nothing(
    owner_session: Session, clean_billing: None
) -> None:
    ch = _channel(owner_session, last_inbound_at=NOW, opt_out=True)
    wa = FakeWhatsAppClient()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()
    assert delivery.status == DELIVERY_NO_SEND
    assert wa.sent == []
