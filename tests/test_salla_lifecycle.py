"""§05 «التجديد والنهايات»: reminders d27/d29 → GRACE 48h → EXPIRED →
recovery after 7 days — one idempotent sweep, template sends guarded."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.lifecycle import sweep_subscription_lifecycle
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient

_PR = {"prod_pro": (Decimal("279.00"), "SAR"), "prod_basic": (Decimal("149"), "SAR"),
       "prod_cv": (Decimal("29.00"), "SAR")}

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def _active_sub(owner_session: Session, *, period_end: datetime) -> uuid.UUID:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("279.00"), "SAR")
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_pro": "professional"},
                             expected_pricing=_PR)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    activate(owner_session, token=result.activation_token, from_phone=phone,
             display_name=None, now=NOW, whatsapp_client=FakeWhatsAppClient(),
             admin_client=FakeTelegramAdminClient())
    sub_id = uuid.UUID(str(result.subscription_id))
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'ACTIVE', current_period_end = :e"
        " WHERE id = :id"), {"e": period_end, "id": str(sub_id)})
    owner_session.commit()
    return sub_id


def _status(s: Session, sub_id: uuid.UUID) -> str:
    return s.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :id"),
        {"id": str(sub_id)}).scalar_one()


def test_day27_reminder_sent_once(owner_session: Session, clean_billing: None) -> None:
    sub_id = _active_sub(owner_session, period_end=NOW + timedelta(days=2, hours=12))
    wa = FakeWhatsAppClient()
    c1 = sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    c2 = sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    owner_session.commit()
    assert c1["reminded"] == 1 and c2["reminded"] == 0    # idempotent
    templates = [m for m in wa.sent if m.kind == "template"]
    assert len(templates) == 1
    assert templates[0].template_name == "renewal_reminder"
    assert _status(owner_session, sub_id) == "ACTIVE"


def test_period_end_enters_grace_then_48h_expires(
    owner_session: Session, clean_billing: None
) -> None:
    sub_id = _active_sub(owner_session, period_end=NOW - timedelta(hours=1))
    wa = FakeWhatsAppClient()
    sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    owner_session.commit()
    assert _status(owner_session, sub_id) == "GRACE"

    still_grace = NOW + timedelta(hours=40)
    sweep_subscription_lifecycle(owner_session, now=still_grace, whatsapp_client=wa)
    owner_session.commit()
    assert _status(owner_session, sub_id) == "GRACE"      # 48h not elapsed

    after = NOW + timedelta(hours=48)
    sweep_subscription_lifecycle(owner_session, now=after, whatsapp_client=wa)
    owner_session.commit()
    assert _status(owner_session, sub_id) == "EXPIRED"


def test_recovery_message_seven_days_after_expiry_once(
    owner_session: Session, clean_billing: None
) -> None:
    period_end = NOW - timedelta(days=10)
    sub_id = _active_sub(owner_session, period_end=period_end)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED' WHERE id = :id"),
        {"id": str(sub_id)})
    owner_session.commit()
    wa = FakeWhatsAppClient()
    c1 = sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    c2 = sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    owner_session.commit()
    assert c1["recovered"] == 1 and c2["recovered"] == 0
    assert [m.template_name for m in wa.sent if m.kind == "template"] == ["recovery"]
    assert _status(owner_session, sub_id) == "EXPIRED"


def test_cv_analysis_one_shot_is_untouched(
    owner_session: Session, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_cv", Decimal("29.00"), "SAR")
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_cv": "cv_analysis"},
                             expected_pricing=_PR)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'ACTIVE',"
        " current_period_end = :e WHERE id = :id"),
        {"e": NOW - timedelta(days=5), "id": str(result.subscription_id)})
    owner_session.commit()
    counts = sweep_subscription_lifecycle(owner_session, now=NOW)
    owner_session.commit()
    assert counts == {"reminded": 0, "graced": 0, "expired": 0, "recovered": 0,
                      "unclaimed_reminded": 0, "unclaimed_expired": 0}


def test_amount_or_currency_mismatch_never_provisions(
    owner_session: Session, clean_billing: None
) -> None:
    """§09 triple match: paid order with a tampered amount is refused."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("10.00"), "SAR")   # should be 279.00
    })
    result = provision_order(
        owner_session, order_id, salla_client=client,
        product_catalog={"prod_pro": "professional"},
        expected_pricing={"prod_pro": (Decimal("279.00"), "SAR")},
    )
    assert result.status == "amount_mismatch"
    count = owner_session.execute(sql_text(
        "SELECT count(*) FROM subscriptions WHERE salla_order_id = :o"),
        {"o": order_id}).scalar_one()
    assert count == 0

    # the honest price passes
    order2 = f"ORD-{uuid.uuid4()}"
    client2 = FakeSallaClient({
        order2: SallaOrder(order2, "paid", "prod_pro",
                           Decimal("279.00"), "SAR")
    })
    ok = provision_order(
        owner_session, order2, salla_client=client2,
        product_catalog={"prod_pro": "professional"},
        expected_pricing={"prod_pro": (Decimal("279.00"), "SAR")},
    )
    assert ok.status == "provisioned"


def test_unclaimed_subscription_expires_after_claim_deadline(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT ك-20: PAID_UNCLAIMED older than 7 days expires honestly."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("279.00"), "SAR")
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_pro": "professional"},
                             expected_pricing=_PR)
    owner_session.commit()
    # age the subscription 8 days back
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET created_at = now() - interval '8 days'"
        " WHERE id = :i"), {"i": result.subscription_id})
    owner_session.commit()
    counts = sweep_subscription_lifecycle(
        owner_session, now=datetime.now(UTC), whatsapp_client=None)
    owner_session.commit()
    assert counts["unclaimed_expired"] == 1
    status = owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :i"),
        {"i": result.subscription_id}).scalar_one()
    assert status == "EXPIRED"


def test_unclaimed_gets_one_reminder_before_deadline(
    owner_session: Session, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("279.00"), "SAR",
                             customer_phone="0555000111")
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_pro": "professional"},
                             expected_pricing=_PR)
    owner_session.commit()
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET created_at = now() - interval '6 days'"
        " WHERE id = :i"), {"i": result.subscription_id})
    owner_session.commit()
    wa = FakeWhatsAppClient()
    counts = sweep_subscription_lifecycle(
        owner_session, now=datetime.now(UTC), whatsapp_client=wa)
    owner_session.commit()
    assert counts["unclaimed_reminded"] == 1
    assert any(m.kind == "template" for m in wa.sent)
    # idempotent — second sweep sends nothing
    counts2 = sweep_subscription_lifecycle(
        owner_session, now=datetime.now(UTC), whatsapp_client=wa)
    assert counts2["unclaimed_reminded"] == 0
