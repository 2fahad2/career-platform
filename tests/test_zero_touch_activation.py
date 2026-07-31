"""CHANGELOG §11 zero-touch activation: paid order → welcome template to the
buyer's phone → ANY reply from that phone claims the subscription."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent
from career.salla.activation_link import normalize_order_phone
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import process_pending_webhooks
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.worker import _handle_message

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def test_normalize_order_phone_variants() -> None:
    assert normalize_order_phone("+966501234567") == "+966501234567"
    assert normalize_order_phone("966501234567") == "+966501234567"
    assert normalize_order_phone("0501234567") == "+966501234567"
    assert normalize_order_phone("00966501234567") == "+966501234567"
    assert normalize_order_phone("12345") is None
    assert normalize_order_phone(None) is None


def test_paid_order_sends_welcome_and_any_reply_claims_it(
    owner_session: Session, clean_billing: None
) -> None:
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    order_id = f"ORD-{uuid.uuid4()}"
    salla = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("279.00"), "SAR", customer_phone=phone)
    })
    ev = WebhookEvent(
        id=uuid.uuid4(), provider="salla", event_type="order.created",
        event_fingerprint=f"fp-{uuid.uuid4()}", salla_order_id=order_id,
        payload={}, processing_status="received", signature_valid=True,
    )
    owner_session.add(ev)
    owner_session.commit()

    wa = FakeWhatsAppClient()
    results = process_pending_webhooks(
        owner_session, salla_client=salla,
        product_catalog={"prod_pro": "professional"},
        expected_pricing={"prod_pro": (Decimal("279.00"), "SAR")},
        admin_client=FakeTelegramAdminClient(),
        whatsapp_number_e164="+15551594303", whatsapp_client=wa,
    )
    owner_session.commit()
    assert results and results[0].status == "provisioned"
    # the approved welcome template went to the BUYER automatically
    templates = [m for m in wa.sent if m.kind == "template"]
    assert templates and templates[0].to_phone == phone
    assert templates[0].template_name == "welcome_activation"
    stored = owner_session.execute(sql_text(
        "SELECT order_phone_e164 FROM subscriptions WHERE salla_order_id = :o"),
        {"o": order_id}).scalar_one()
    assert stored == phone

    # ANY reply from that phone claims the subscription — no token typed
    wa2 = FakeWhatsAppClient()
    _handle_message(
        owner_session,
        {"id": f"wamid.{uuid.uuid4().hex}", "from": phone,
         "type": "text", "text": {"body": "مرحبا"}},
        whatsapp_client=wa2, admin_client=FakeTelegramAdminClient(), now=NOW,
    )
    owner_session.commit()
    status = owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE salla_order_id = :o"),
        {"o": order_id}).scalar_one()
    assert status == "ONBOARDING"
    bound = owner_session.execute(sql_text(
        "SELECT count(*) FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone}).scalar_one()
    assert bound == 1
    assert any("تم التفعيل" in (m.body or "") for m in wa2.sent)


def test_unknown_phone_without_paid_order_still_gets_help(
    owner_session: Session, clean_billing: None
) -> None:
    wa = FakeWhatsAppClient()
    _handle_message(
        owner_session,
        {"id": f"wamid.{uuid.uuid4().hex}", "from": "+966599999999",
         "type": "text", "text": {"body": "مرحبا"}},
        whatsapp_client=wa, admin_client=FakeTelegramAdminClient(), now=NOW,
    )
    assert any("رمز التفعيل" in (m.body or "") for m in wa.sent)


# ── the shape Meta actually sends (closure audit, 31 July) ──────────────────


def test_the_reply_activates_in_the_shape_meta_really_sends(
    owner_session, clean_billing
) -> None:
    """CHANGELOG §11 promises the buyer «ردّ بأي رسالة للمتابعة». Salla phones
    are stored normalized WITH a leading «+»; Meta delivers `from` WITHOUT one.
    The lookup compared them exactly, so the promise could not fire for a
    single real customer — and the existing test hid it by feeding the same
    «+»-prefixed string as both sides.
    """
    import uuid as _uuid
    from decimal import Decimal

    from sqlalchemy import text as _sql

    from career.salla.client import FakeSallaClient, SallaOrder
    from career.salla.provisioning import provision_order
    from career.telegram.admin import FakeTelegramAdminClient
    from career.whatsapp.activation_flow import activate_by_order_phone
    from career.whatsapp.client import FakeWhatsAppClient

    digits = f"96650{_uuid.uuid4().int % 10_000_000:07d}"
    order_id = f"ORD-{_uuid.uuid4()}"
    order = SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR",
                       customer_phone=f"+{digits}")     # Salla shape
    result = provision_order(
        owner_session, order_id,
        salla_client=FakeSallaClient({order_id: order}),
        product_catalog={"prod_pro": "professional"},
        expected_pricing={"prod_pro": (Decimal("279.00"), "SAR")},
    )
    assert result.activation_token is not None
    owner_session.commit()

    claimed = activate_by_order_phone(          # Meta shape: no leading «+»
        owner_session, from_phone=digits, display_name=None,
        now=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        whatsapp_client=FakeWhatsAppClient(),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()

    assert claimed is not None, "the buyer's instructed reply must claim it"
    assert owner_session.execute(_sql(
        "SELECT status FROM subscriptions WHERE id = :i"),
        {"i": str(result.subscription_id)}).scalar_one() == "ONBOARDING"
