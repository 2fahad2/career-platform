"""WhatsApp inbound worker (DB) — end-to-end routing from a webhook payload.

Activation, STOP (immediate opt-out), support escalation, OTHER→descend,
delivery status callbacks, per-message idempotency, and no PII in the admin
channel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, WebhookEvent
from career.salla import subscriptions as st
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.delivery import DELIVERY_COMPLETED, deliver_adaptive
from career.whatsapp.templates import DAILY_UTILITY
from career.whatsapp.worker import process_pending_whatsapp

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
CATALOG = {"prod_pro": "professional"}


def _payload(messages: list[dict[str, Any]] | None = None,
             statuses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "messages": messages or [], "statuses": statuses or [],
    }}]}]}


def _text_msg(wamid: str, phone: str, body: str) -> dict[str, Any]:
    return {"id": wamid, "from": phone, "type": "text", "text": {"body": body}}


def _insert_event(owner_session: Session, payload: dict[str, Any]) -> None:
    owner_session.add(WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="messages",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload=payload, processing_status="received",
    ))
    owner_session.commit()


def _provision_token(owner_session: Session) -> str:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR")
    })
    pricing = {k: (Decimal("279.00"), "SAR") for k in CATALOG}
    tok = provision_order(owner_session, order_id, salla_client=client,
                          product_catalog=CATALOG,
                          expected_pricing=pricing).activation_token
    assert tok is not None
    return tok


def _phone() -> str:
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _run(owner_session: Session, wa: FakeWhatsAppClient, admin: FakeTelegramAdminClient,
         *, now: datetime = NOW) -> dict[str, int]:
    return process_pending_whatsapp(owner_session, whatsapp_client=wa, admin_client=admin, now=now)


def test_activation_via_worker_and_idempotency(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    token = _provision_token(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()

    _insert_event(owner_session, _payload([_text_msg("wamid-1", phone, f"تفعيل {token}")]))
    counts = _run(owner_session, wa, admin)
    assert counts["messages"] == 1

    with Session(owner_engine) as s:
        status = s.execute(
            text("SELECT sub.status FROM subscriptions sub JOIN customer_channels c"
                 " ON c.subscription_id = sub.id WHERE c.phone_e164 = :p"), {"p": phone},
        ).scalar_one()
        assert status == st.ONBOARDING

    # A redelivered message with the same wamid (different POST) must not re-activate.
    _insert_event(owner_session, _payload([_text_msg("wamid-1", phone, f"تفعيل {token}")]))
    _run(owner_session, wa, admin)
    with Session(owner_engine) as s:
        channels = s.execute(
            text("SELECT count(*) FROM customer_channels WHERE phone_e164 = :p"), {"p": phone},
        ).scalar_one()
        inbound = s.execute(
            text("SELECT count(*) FROM inbound_messages WHERE wa_message_id = 'wamid-1'")
        ).scalar_one()
    assert channels == 1
    assert inbound == 1


def _activate_phone(owner_session: Session, wa: FakeWhatsAppClient,
                    admin: FakeTelegramAdminClient) -> str:
    token = _provision_token(owner_session)
    phone = _phone()
    activate(owner_session, token=token, from_phone=phone, display_name=None, now=NOW,
             whatsapp_client=wa, admin_client=admin)
    return phone


def test_stop_opts_out_immediately(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    _insert_event(owner_session, _payload([_text_msg("wamid-stop", phone, "إيقاف الرسائل")]))
    _run(owner_session, wa, admin)
    with Session(owner_engine) as s:
        opt_out = s.execute(
            text("SELECT opt_out_at FROM customer_channels WHERE phone_e164 = :p"), {"p": phone},
        ).scalar_one()
    assert opt_out is not None  # opted out immediately


def test_support_escalates_without_pii(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    admin.messages.clear()  # ignore any activation-time admin noise
    _insert_event(owner_session, _payload([_text_msg("wamid-sup", phone, "دعم")]))
    _run(owner_session, wa, admin)
    with Session(owner_engine) as s:
        open_support = s.execute(
            text("SELECT count(*) FROM support_events WHERE status = 'open'")
        ).scalar_one()
    assert open_support == 1
    assert len(admin.messages) == 1
    # No PII: the phone number never appears in the admin channel (§15.13).
    digits = phone.lstrip("+")
    assert all(digits not in m for m in admin.messages)
    assert any("TEN-" in m for m in admin.messages)


def test_other_message_descends_pending_delivery(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    ch = owner_session.execute(
        select(CustomerChannel).where(CustomerChannel.phone_e164 == phone)
    ).scalar_one()
    # Make the window closed and queue a pending delivery.
    ch.last_inbound_at = NOW - timedelta(hours=25)
    owner_session.commit()
    bundle = {"parts": [{"kind": "text", "body": "#1 job"}]}
    deliver_adaptive(owner_session, ch, bundle, run_date=NOW.date(),
                     whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    # Customer taps (OTHER) → descend.
    _insert_event(owner_session, _payload([_text_msg("wamid-tap", phone, "عرض")]))
    _run(owner_session, wa, admin, now=NOW + timedelta(minutes=1))
    with Session(owner_engine) as s:
        status = s.execute(text("SELECT status FROM deliveries")).scalar_one()
    assert status == DELIVERY_COMPLETED


def test_delivery_status_callback_updates_message(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate_phone(owner_session, wa, admin)
    # The activation welcome recorded a delivery_message with the fake wamid.
    welcome_wamid = wa.sent[-1].message_id
    _insert_event(owner_session, _payload(statuses=[{"id": welcome_wamid, "status": "delivered"}]))
    _run(owner_session, wa, admin)
    with Session(owner_engine) as s:
        status = s.execute(
            text("SELECT status FROM delivery_messages WHERE wa_message_id = :m"),
            {"m": welcome_wamid},
        ).scalar_one()
    assert status == "delivered"
