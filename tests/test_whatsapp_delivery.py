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


# ── the tap must never be spent on an attempt that delivered nothing ─────────

_GROUPED = {
    "grouped": True,
    "header": "فرصك اليوم",
    "jobs": [{
        "group": "g1",
        "card": {"body": "#1 IT Operations Manager @ NEOM"},
        "document": {"ref": "tenants/x/cv1.pdf", "filename": "CV.pdf",
                     "caption": "CV #1"},
    }],
}


class _DeadDocuments(FakeWhatsAppClient):
    """Text lands, every document is rejected — the live 27 July shape."""

    def send_document(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("graph HTTP 400 (code 131053)")


def test_a_tap_that_delivered_nothing_stays_claimable(
    owner_session: Session, clean_billing: None
) -> None:
    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    later = NOW + timedelta(minutes=5)
    ch.last_inbound_at = later
    descended = descend_pending_delivery(
        owner_session, ch, whatsapp_client=dead, now=later)
    owner_session.commit()

    assert descended is not None
    # nothing landed → the customer's one chance is NOT spent
    assert descended.status == DELIVERY_PENDING
    assert descended.completed_at is None
    assert descended.bundle["attempts"] == 1


def test_the_operator_can_resend_what_never_arrived(
    owner_session: Session, clean_billing: None
) -> None:
    from career.whatsapp.delivery import resend_pending_delivery

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    later = NOW + timedelta(minutes=5)
    ch.last_inbound_at = later
    descend_pending_delivery(owner_session, ch, whatsapp_client=dead, now=later)
    owner_session.commit()

    # the operator retries once the cause is fixed — with a working client
    healthy = FakeWhatsAppClient()
    again = resend_pending_delivery(
        owner_session, tenant_id=ch.tenant_id, whatsapp_client=healthy,
        now=later + timedelta(minutes=30),
    )
    owner_session.commit()

    assert again is not None
    assert again.status == DELIVERY_COMPLETED
    assert [m.kind for m in healthy.sent] == ["text", "text", "document"]


def test_retries_are_bounded_and_then_close_honestly(
    owner_session: Session, clean_billing: None
) -> None:
    from career.whatsapp.delivery import (
        DELIVERY_PARTIAL,
        MAX_DISPATCH_ATTEMPTS,
        resend_pending_delivery,
    )

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    delivery = None
    for i in range(MAX_DISPATCH_ATTEMPTS):
        delivery = resend_pending_delivery(
            owner_session, tenant_id=ch.tenant_id, whatsapp_client=dead,
            now=NOW + timedelta(hours=i + 1),
        )
        owner_session.commit()

    assert delivery is not None
    assert delivery.status == DELIVERY_PARTIAL      # honest, not pending forever
    assert delivery.bundle["attempts"] == MAX_DISPATCH_ATTEMPTS
    # and there is nothing left to resend
    assert resend_pending_delivery(
        owner_session, tenant_id=ch.tenant_id, whatsapp_client=dead,
        now=NOW + timedelta(days=1),
    ) is None


def test_a_resend_never_reaches_someone_who_opted_out(
    owner_session: Session, clean_billing: None
) -> None:
    from career.whatsapp.delivery import resend_pending_delivery

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    ch.opt_out_at = NOW + timedelta(minutes=1)
    owner_session.commit()

    healthy = FakeWhatsAppClient()
    assert resend_pending_delivery(
        owner_session, tenant_id=ch.tenant_id, whatsapp_client=healthy,
        now=NOW + timedelta(hours=1),
    ) is None
    assert not healthy.sent
