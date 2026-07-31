"""§16 F-RENEW — the second payment continues, it does not start over.

Before this feature a renewing customer got a whole second tenant, a second
founding seat, and «هذا الرقم مرتبط بحساب آخر» if they typed the new token.
Every test below is one of those failures, stated as the behaviour we now
require.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.onboarding import privacy
from career.salla import renewal, seats
from career.salla import subscriptions as sub_states
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import ProvisionStatus, provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
_CATALOG = {"prod_pro": "professional", "prod_basic": "basic",
            "prod_cv": "cv_analysis"}
_PR = {"prod_pro": (Decimal("279.00"), "SAR"),
       "prod_basic": (Decimal("149.00"), "SAR"),
       "prod_cv": (Decimal("29.00"), "SAR")}


def _phone() -> str:
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _buy(
    session: Session, *, product: str = "prod_pro",
    wa: FakeWhatsAppClient | None = None,
    admin: FakeTelegramAdminClient | None = None,
    phone: str | None = None,
):
    """One paid order through the real webhook-side entry point."""
    order_id = f"ORD-{uuid.uuid4()}"
    amount, currency = _PR[product]
    order = SallaOrder(order_id, "paid", product, amount, currency)
    if phone is not None:
        order = SallaOrder(order_id, "paid", product, amount, currency,
                           customer_phone=phone)
    return provision_order(
        session, order_id, salla_client=FakeSallaClient({order_id: order}),
        product_catalog=_CATALOG, expected_pricing=_PR, now=NOW,
    ), order_id


def _customer(session: Session, *, product: str = "prod_pro") -> tuple[str, uuid.UUID, uuid.UUID]:
    """A real ACTIVE customer: paid, activated by token, period running."""
    phone = _phone()
    result, _ = _buy(session, product=product, phone=phone)
    activate(session, token=result.activation_token, from_phone=phone,
             display_name=None, now=NOW, whatsapp_client=FakeWhatsAppClient(),
             admin_client=FakeTelegramAdminClient())
    sub_id = uuid.UUID(str(result.subscription_id))
    session.execute(sql_text(
        "UPDATE subscriptions SET status = 'ACTIVE', current_period_end = :e"
        " WHERE id = :id"),
        {"e": NOW + timedelta(days=5), "id": str(sub_id)})
    session.commit()
    return phone, uuid.UUID(str(result.tenant_id)), sub_id


def _subs(session: Session, tenant_id: uuid.UUID) -> list[tuple[str, str]]:
    rows = session.execute(sql_text(
        "SELECT status, salla_order_id FROM subscriptions WHERE tenant_id = :t"
        " ORDER BY created_at"), {"t": str(tenant_id)}).all()
    return [(r[0], r[1]) for r in rows]


def test_second_purchase_stays_on_the_same_customer(owner_session, clean_billing):
    phone, tenant_id, first_id = _customer(owner_session)

    before = owner_session.execute(sql_text("SELECT count(*) FROM tenants")).scalar_one()
    result, order_id = _buy(owner_session, phone=phone)
    after = owner_session.execute(sql_text("SELECT count(*) FROM tenants")).scalar_one()

    assert result.status is ProvisionStatus.RENEWED
    assert result.tenant_id == str(tenant_id)          # NOT a new person
    assert after == before                             # NOT a new tenant
    assert result.activation_token is None             # NOT a second token

    states = {order: status for status, order in _subs(owner_session, tenant_id)}
    assert states[order_id] == sub_states.ACTIVE
    # exactly one live row: the superseded period is closed honestly
    assert sum(1 for s, _ in _subs(owner_session, tenant_id)
               if s == sub_states.ACTIVE) == 1
    assert owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :id"),
        {"id": str(first_id)}).scalar_one() == sub_states.EXPIRED


def test_renewing_early_keeps_every_day_paid_for(owner_session, clean_billing):
    phone, tenant_id, _ = _customer(owner_session)   # 5 days still to run

    _buy(owner_session, phone=phone)

    current = renewal.current_subscription(owner_session, tenant_id)
    assert current is not None
    # days stack: new period starts where the old one ended, not today
    assert current.current_period_start == NOW + timedelta(days=5)
    assert current.current_period_end == NOW + timedelta(days=35)


def test_an_expired_customer_coming_back_is_a_renewal(owner_session, clean_billing):
    phone, tenant_id, sub_id = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED', current_period_end = :e"
        " WHERE id = :id"),
        {"e": NOW - timedelta(days=40), "id": str(sub_id)})
    owner_session.commit()

    result, _ = _buy(owner_session, phone=phone)

    assert result.status is ProvisionStatus.RENEWED
    current = renewal.current_subscription(owner_session, tenant_id)
    assert current is not None and current.status == sub_states.ACTIVE
    # a lapsed period is NOT stacked — the clock starts now, and the days
    # they were away are not silently handed back to them
    assert current.current_period_start == NOW
    assert current.current_period_end == NOW + timedelta(days=30)


def test_a_paused_customer_is_not_silently_resumed(owner_session, clean_billing):
    phone, tenant_id, sub_id = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'PAUSED' WHERE id = :id"),
        {"id": str(sub_id)})
    owner_session.commit()

    _buy(owner_session, phone=phone)

    current = renewal.current_subscription(owner_session, tenant_id)
    assert current is not None
    assert current.status == sub_states.PAUSED  # they asked for quiet


def test_a_disputed_account_waits_for_a_human(owner_session, clean_billing):
    phone, tenant_id, sub_id = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'CHARGEBACK' WHERE id = :id"),
        {"id": str(sub_id)})
    owner_session.commit()

    result, _ = _buy(owner_session, phone=phone)

    # linked to the same customer (no dead end) but service is NOT restored
    assert result.status is ProvisionStatus.RENEWED
    assert result.tenant_id == str(tenant_id)
    current = renewal.current_subscription(owner_session, tenant_id)
    assert current is not None
    assert current.status == sub_states.PAID_UNCLAIMED
    assert owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :id"),
        {"id": str(sub_id)}).scalar_one() == "CHARGEBACK"  # untouched


def test_a_stranger_is_still_a_brand_new_customer(owner_session, clean_billing):
    _customer(owner_session)
    before = owner_session.execute(sql_text("SELECT count(*) FROM tenants")).scalar_one()

    result, _ = _buy(owner_session, phone=_phone())

    assert result.status is ProvisionStatus.PROVISIONED
    assert result.activation_token is not None
    after = owner_session.execute(sql_text("SELECT count(*) FROM tenants")).scalar_one()
    assert after == before + 1


def test_the_analysis_product_is_never_a_renewal(owner_session, clean_billing):
    phone, tenant_id, _ = _customer(owner_session)

    result, _ = _buy(owner_session, product="prod_cv", phone=phone)

    # the 49-riyal funnel has no period and its own upgrade path (§04)
    assert result.status is ProvisionStatus.PROVISIONED
    assert result.tenant_id != str(tenant_id)


def test_a_renewal_does_not_burn_a_second_founding_seat(owner_session, clean_billing):
    phone, _, _ = _customer(owner_session)
    taken_once = seats.founding_seats(owner_session).taken

    _buy(owner_session, phone=phone)

    assert seats.founding_seats(owner_session).taken == taken_once


def test_status_reply_shows_the_renewed_period_not_the_old_one(
    owner_session, clean_billing
):
    phone, tenant_id, _ = _customer(owner_session)
    _buy(owner_session, phone=phone)

    summary = privacy.subscription_status_summary(
        owner_session, tenant_id=tenant_id, now=NOW,
    )

    assert "نشط" in summary
    assert "35" in summary  # 5 remaining + 30 renewed, not the old 5


def test_status_reply_offers_a_way_back_when_it_ends(owner_session, clean_billing):
    _, tenant_id, sub_id = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED', current_period_end = :e"
        " WHERE id = :id"),
        {"e": NOW - timedelta(days=2), "id": str(sub_id)})
    owner_session.commit()

    summary = privacy.subscription_status_summary(
        owner_session, tenant_id=tenant_id, now=NOW,
        store_url="https://store.example/renew",
    )

    assert "https://store.example/renew" in summary
    # direction-pure: the URL owns its own line
    assert "\nhttps://store.example/renew" in summary


def test_status_reply_without_a_configured_store_still_has_a_next_step(
    owner_session, clean_billing
):
    _, tenant_id, sub_id = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED' WHERE id = :id"),
        {"id": str(sub_id)})
    owner_session.commit()

    summary = privacy.subscription_status_summary(
        owner_session, tenant_id=tenant_id, now=NOW, store_url="",
    )

    assert "دعم" in summary  # never a dead end, link or no link


def test_the_renewing_customer_is_told_and_the_operator_too(
    owner_session, clean_billing
):
    from career.db.models import WebhookEvent
    from career.salla.provisioning import process_pending_webhooks

    phone, _, _ = _customer(owner_session)
    order_id = f"ORD-{uuid.uuid4()}"
    order = SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR",
                       customer_phone=phone)
    owner_session.add(WebhookEvent(
        id=uuid.uuid4(), provider="salla", event_type="order.payment.updated",
        event_fingerprint=f"fp-{order_id}", signature_valid=True,
        salla_order_id=order_id, payload={"data": {"id": order_id}},
        processing_status="received",
    ))
    owner_session.commit()

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    process_pending_webhooks(
        owner_session, salla_client=FakeSallaClient({order_id: order}),
        product_catalog=_CATALOG, expected_pricing=_PR,
        admin_client=admin, whatsapp_client=wa,
        whatsapp_number_e164="+966500000000",
    )

    texts = [m.body for m in wa.sent if m.kind == "text"]
    assert any("تم تجديد اشتراكك" in (t or "") for t in texts)
    # a renewal must NOT re-send the activation template or a token link
    assert not [m for m in wa.sent if m.kind == "template"]
    assert any("تجديد" in m for m in admin.messages)
    assert not any("رابط التفعيل" in m for m in admin.messages)
