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
    # SUSPENDED, not PAID_UNCLAIMED: the claim sweep would have expired a
    # paid renewal after 7 days with no alert
    assert current.status == sub_states.SUSPENDED
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
    # they messaged us just now, so the 24h window is open and a free-form
    # confirmation actually lands
    owner_session.execute(sql_text(
        "UPDATE customer_channels SET last_inbound_at = now() WHERE"
        " phone_e164 = :p"), {"p": phone})
    owner_session.commit()
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


# ── the adversarial review's findings, each pinned as a requirement ──────────


def test_a_refund_on_a_superseded_order_never_jams_the_queue(
    owner_session, clean_billing
):
    """The worst one: close_previous parks the old row in EXPIRED, and a
    refund for THAT order used to raise InvalidTransition straight out of the
    webhook worker — every later webhook, new paid orders included, stopped
    being processed."""
    from career.db.models import WebhookEvent
    from career.salla.provisioning import process_pending_webhooks

    phone, tenant_id, first_id = _customer(owner_session)
    first_order = owner_session.execute(sql_text(
        "SELECT salla_order_id FROM subscriptions WHERE id = :i"),
        {"i": str(first_id)}).scalar_one()
    _buy(owner_session, phone=phone)   # renewal retires the first row

    order = SallaOrder(first_order, "refunded", "prod_pro",
                       Decimal("279.00"), "SAR", customer_phone=phone)
    owner_session.add(WebhookEvent(
        id=uuid.uuid4(), provider="salla", event_type="order.refunded",
        event_fingerprint=f"fp-ref-{first_order}", signature_valid=True,
        salla_order_id=first_order, payload={"data": {"id": first_order}},
        processing_status="received",
    ))
    owner_session.commit()

    process_pending_webhooks(   # must not raise
        owner_session, salla_client=FakeSallaClient({first_order: order}),
        product_catalog=_CATALOG, expected_pricing=_PR,
        admin_client=FakeTelegramAdminClient(), whatsapp_client=FakeWhatsAppClient(),
        whatsapp_number_e164="+966500000000",
    )

    assert owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :i"),
        {"i": str(first_id)}).scalar_one() == sub_states.REFUNDED
    # the event is closed, not left poisoning the queue forever
    assert owner_session.execute(sql_text(
        "SELECT processing_status FROM webhook_events WHERE salla_order_id = :o"
        " AND event_type = 'order.refunded'"),
        {"o": first_order}).scalar_one() != "received"


def test_a_disputed_renewal_is_not_swept_away_by_the_claim_deadline(
    owner_session, clean_billing
):
    """It used to be written PAID_UNCLAIMED, so the claim sweep nagged an
    already-activated customer at day 5 and silently EXPIRED their paid
    renewal at day 7 — the operator's review window was secretly a week."""
    from career.salla.lifecycle import sweep_subscription_lifecycle

    phone, tenant_id, sub_id = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'CHARGEBACK' WHERE id = :id"),
        {"id": str(sub_id)})
    owner_session.commit()
    result, _ = _buy(owner_session, phone=phone)
    renewed_id = uuid.UUID(str(result.subscription_id))

    wa = FakeWhatsAppClient()
    sweep_subscription_lifecycle(
        owner_session, now=NOW + timedelta(days=9), whatsapp_client=wa,
    )
    owner_session.commit()

    assert owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :i"),
        {"i": str(renewed_id)}).scalar_one() == sub_states.SUSPENDED
    assert not wa.sent


def test_a_renewed_customer_is_never_told_we_miss_them(
    owner_session, clean_billing
):
    """The retired row keeps its old period_end, so the sweep used to walk an
    ACTIVE paying customer through their PREVIOUS period again — a «we miss
    you» template plus a renew link, ~9 days after every renewal, forever."""
    from career.salla.lifecycle import sweep_subscription_lifecycle

    phone, tenant_id, _ = _customer(owner_session)   # period ends NOW+5
    _buy(owner_session, phone=phone)

    wa = FakeWhatsAppClient()
    sweep_subscription_lifecycle(
        owner_session, now=NOW + timedelta(days=16), whatsapp_client=wa,
        store_url="https://store.example/renew",
    )
    owner_session.commit()

    assert not wa.sent
    current = renewal.current_subscription(owner_session, tenant_id)
    assert current is not None and current.status == sub_states.ACTIVE


def test_the_confirmation_is_not_shouted_into_a_closed_window(
    owner_session, clean_billing
):
    """Most renewals happen on the storefront, so the 24h window is shut. A
    free-form send would simply be rejected — the operator must be told that
    the customer was NOT reached, not left assuming they were."""
    from career.db.models import WebhookEvent
    from career.salla.provisioning import process_pending_webhooks

    phone, _, _ = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE customer_channels SET last_inbound_at = :t WHERE phone_e164 = :p"),
        {"t": NOW - timedelta(days=3), "p": phone})
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

    assert not wa.sent
    assert any("نافذة واتساب مقفلة" in m for m in admin.messages)


def test_renewing_on_a_bigger_pass_actually_changes_the_service(
    owner_session, clean_billing
):
    """Entitlements are snapshotted onto the search policy at onboarding and
    never re-read — so an upgrade used to cost more and deliver exactly the
    same number of jobs."""
    phone, tenant_id, sub_id = _customer(owner_session, product="prod_basic")
    owner_session.execute(sql_text(
        "INSERT INTO search_policies (id, tenant_id, version, status,"
        " approved_paths, cities, daily_job_limit)"
        " VALUES (:i, :t, 1, 'active', '{}'::jsonb, '{}'::jsonb, 1)"),
        {"i": str(uuid.uuid4()), "t": str(tenant_id)})
    owner_session.commit()

    _buy(owner_session, product="prod_pro", phone=phone)   # basic → professional

    limit = owner_session.execute(sql_text(
        "SELECT daily_job_limit FROM search_policies WHERE tenant_id = :t"
        " AND status = 'active'"), {"t": str(tenant_id)}).scalar_one()
    assert limit == 2   # the professional entitlement, not the old basic 1
    owner_session.execute(sql_text(
        "DELETE FROM search_policies WHERE tenant_id = :t"), {"t": str(tenant_id)})
    owner_session.commit()


def test_a_pass_customer_can_buy_the_analysis_without_hitting_a_wall(
    owner_session, clean_billing
):
    """The mirror of the §04 upgrade: a paying pass customer buying the
    29-riyal analysis was answered «هذا الرقم مرتبط بحساب آخر» — blocked from
    a product they had just paid for."""
    from career.whatsapp.activation_flow import ActivationStatus

    phone, tenant_id, _ = _customer(owner_session)
    result, _ = _buy(owner_session, product="prod_cv", phone=phone)

    outcome = activate(
        owner_session, token=result.activation_token, from_phone=phone,
        display_name=None, now=NOW, whatsapp_client=FakeWhatsAppClient(),
        admin_client=FakeTelegramAdminClient(),
    )

    assert outcome.status is not ActivationStatus.CONFLICT
    assert outcome.tenant_id == str(tenant_id)   # rides along on their tenant
    plans = sorted(p for _, p in [
        (s, r) for s, r in owner_session.execute(sql_text(
            "SELECT status, plan_code FROM subscriptions WHERE tenant_id = :t"),
            {"t": str(tenant_id)}).all()
    ])
    assert "cv_analysis" in plans and "professional" in plans


def test_paying_twice_before_activating_costs_neither_a_tenant_nor_a_day(
    owner_session, clean_billing
):
    """The last open finding: with no channel yet there was nothing to match
    on, so a second purchase opened a SECOND tenant, burned a second founding
    seat and minted a token that dies at the claim deadline — thirty paid days
    destroyed in silence."""
    from career.db.models import Subscription
    from career.salla import seats

    phone = _phone()
    before_tenants = owner_session.execute(sql_text(
        "SELECT count(*) FROM tenants")).scalar_one()
    first, _ = _buy(owner_session, phone=phone)
    seats_after_first = seats.founding_seats(owner_session).taken

    second, _ = _buy(owner_session, phone=phone)   # paid again, still unclaimed

    assert second.status is ProvisionStatus.RENEWED
    assert second.tenant_id == first.tenant_id
    assert second.activation_token is None
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM tenants")).scalar_one() == before_tenants + 1
    assert seats.founding_seats(owner_session).taken == seats_after_first

    # their original token still works — no second activation to chase
    outcome = activate(
        owner_session, token=first.activation_token, from_phone=phone,
        display_name=None, now=NOW, whatsapp_client=FakeWhatsAppClient(),
        admin_client=FakeTelegramAdminClient(),
    )
    assert outcome.status is not None
    tenant_id = uuid.UUID(str(first.tenant_id))

    # and both payments become days: the merge happens when the period is
    # stamped, so sixty days — not thirty with thirty quietly expired
    activated = owner_session.get(
        Subscription, uuid.UUID(str(first.subscription_id)))
    assert activated is not None
    activated.current_period_start = NOW
    activated.current_period_end = NOW + timedelta(days=30)
    merged = renewal.merge_prepaid_orders(
        owner_session, tenant_id=tenant_id, activated=activated, now=NOW,
    )
    owner_session.commit()

    assert merged == 1
    assert activated.current_period_end == NOW + timedelta(days=60)
    assert owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :i"),
        {"i": str(second.subscription_id)}).scalar_one() == sub_states.EXPIRED
    # the money trail survives the merge
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM subscription_events WHERE subscription_id = :i"
        " AND event_type = 'merged_into_activation'"),
        {"i": str(second.subscription_id)}).scalar_one() == 1
