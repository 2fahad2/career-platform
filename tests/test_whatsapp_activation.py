"""Activation (DB) — the C4 exit condition: the token links the order to the
number, and the subscription advances to ONBOARDING."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.salla import subscriptions as st
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import ActivationStatus, activate
from career.whatsapp.client import FakeWhatsAppClient

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
CATALOG = {"prod_pro": "professional"}


def _provision(owner_session: Session, order_id: str) -> str:
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR")
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog=CATALOG)
    assert result.activation_token is not None
    return result.activation_token


def _phone() -> str:
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def test_activation_links_order_to_number(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    token = _provision(owner_session, order_id)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()

    result = activate(owner_session, token=token, from_phone=phone, display_name="Cust",
                      now=NOW, whatsapp_client=wa, admin_client=admin)

    assert result.status is ActivationStatus.ACTIVATED
    with Session(owner_engine) as s:
        # Channel links this exact phone to the tenant of the paid order.
        row = s.execute(
            text("SELECT c.phone_e164, c.verified_at, sub.status, sub.salla_order_id"
                 " FROM customer_channels c JOIN subscriptions sub"
                 " ON sub.id = c.subscription_id WHERE c.tenant_id = :t"),
            {"t": result.tenant_id},
        ).one()
        assert row.phone_e164 == phone
        assert row.verified_at is not None
        assert row.status == st.ONBOARDING          # PAID_UNCLAIMED -> ONBOARDING
        assert row.salla_order_id == order_id        # linked to the paying order
        # Token consumed.
        used = s.execute(
            text("SELECT used_at FROM activation_tokens WHERE tenant_id = :t"),
            {"t": result.tenant_id},
        ).scalar_one()
        assert used is not None
    # Welcome message sent (window is open — they just messaged).
    assert any(m.kind == "text" for m in wa.sent)


def test_reactivation_is_idempotent(
    owner_session: Session, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    token = _provision(owner_session, order_id)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    first = activate(owner_session, token=token, from_phone=phone, display_name=None,
                     now=NOW, whatsapp_client=wa, admin_client=admin)
    second = activate(owner_session, token=token, from_phone=phone, display_name=None,
                      now=NOW, whatsapp_client=wa, admin_client=admin)
    assert first.status is ActivationStatus.ACTIVATED
    assert second.status is ActivationStatus.ALREADY_LINKED
    assert second.channel_id == first.channel_id


def test_invalid_token_rejected(owner_session: Session, clean_billing: None) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    result = activate(owner_session, token="totally-invalid-token-string",
                      from_phone=_phone(), display_name=None, now=NOW,
                      whatsapp_client=wa, admin_client=admin)
    assert result.status is ActivationStatus.INVALID_TOKEN
    assert len(wa.sent) == 1  # a rejection reply
    assert len(admin.messages) == 1  # admin alerted


def test_expired_token_rejected(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    token = _provision(owner_session, order_id)
    # Force the token to be expired.
    with Session(owner_engine) as s:
        s.execute(
            text("UPDATE activation_tokens SET expires_at = :e WHERE subscription_id IN"
                 " (SELECT id FROM subscriptions WHERE salla_order_id = :o)"),
            {"e": NOW - timedelta(days=1), "o": order_id},
        )
        s.commit()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    result = activate(owner_session, token=token, from_phone=_phone(), display_name=None,
                      now=NOW, whatsapp_client=wa, admin_client=admin)
    assert result.status is ActivationStatus.EXPIRED


def test_phone_conflict_rejected(
    owner_session: Session, clean_billing: None
) -> None:
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    # Customer A activates with the phone.
    token_a = _provision(owner_session, f"ORD-{uuid.uuid4()}")
    activate(owner_session, token=token_a, from_phone=phone, display_name=None, now=NOW,
             whatsapp_client=wa, admin_client=admin)
    # Customer B tries to activate from the SAME phone.
    token_b = _provision(owner_session, f"ORD-{uuid.uuid4()}")
    result = activate(owner_session, token=token_b, from_phone=phone, display_name=None,
                      now=NOW, whatsapp_client=wa, admin_client=admin)
    assert result.status is ActivationStatus.CONFLICT
