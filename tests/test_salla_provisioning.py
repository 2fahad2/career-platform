"""Salla provisioning + lifecycle (DB) — the C3 exit-condition behaviors.

One subscription per paid order despite duplicate events; provisioning only on
paid; refund suspends immediately.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.session import SessionLocal
from career.salla import subscriptions as st
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import (
    ProvisionStatus,
    process_pending_webhooks,
    provision_order,
)
from career.salla.signature import compute_signature
from career.salla.webhook import receive_webhook

SECRET = "test_secret_123"
CATALOG = {"prod_pro": "professional", "prod_riyal": "basic"}
# AUDIT ح-4: the gate fails closed for cataloged products without pricing
PRICING = {"prod_pro": (Decimal("279.00"), "SAR"),
           "prod_riyal": (Decimal("1.00"), "SAR")}


def _probe_phone() -> str:
    """A real Salla order ALWAYS carries the buyer's mobile — the whole
    zero-touch activation path keys on it. A test order without one describes
    a shape the world never sends, and that unrealism is how the phone defect
    survived: the suite was green while no real customer could be activated."""
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _order(order_id: str, *, status: str = "paid", product: str = "prod_pro",
           amount: str = "279.00", currency: str = "SAR") -> SallaOrder:
    return SallaOrder(order_id=order_id, status=status, product_id=product,
                      amount=Decimal(amount), currency=currency,
                      customer_phone=_probe_phone())


def _sub_count(owner_engine: Engine, order_id: str) -> int:
    with Session(owner_engine) as s:
        return s.execute(
            text("SELECT count(*) FROM subscriptions WHERE salla_order_id = :o"),
            {"o": order_id},
        ).scalar_one()


def test_paid_order_provisions_one_subscription(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    result = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    assert result.status is ProvisionStatus.PROVISIONED
    assert result.activation_token  # raw token returned once
    assert _sub_count(owner_engine, order_id) == 1

    with Session(owner_engine) as s:
        row = s.execute(
            text("SELECT status, plan_code, amount_sar, currency FROM subscriptions"
                 " WHERE salla_order_id = :o"), {"o": order_id},
        ).one()
        assert row.status == st.PAID_UNCLAIMED
        assert row.plan_code == "professional"
        assert row.currency == "SAR"
        # Activation token stored as a hash of the returned raw token, never raw.
        token_hash = s.execute(
            text("SELECT token_hash FROM activation_tokens at"
                 " JOIN subscriptions sub ON sub.id = at.subscription_id"
                 " WHERE sub.salla_order_id = :o"), {"o": order_id},
        ).scalar_one()
        assert token_hash == hashlib.sha256(result.activation_token.encode()).hexdigest()


def test_duplicate_provision_is_idempotent(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    first = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    second = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    assert first.status is ProvisionStatus.PROVISIONED
    assert second.status is ProvisionStatus.ALREADY_PROVISIONED
    assert second.subscription_id == first.subscription_id
    assert _sub_count(owner_engine, order_id) == 1  # never a second subscription


def test_unpaid_order_not_provisioned(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id, status="pending")})
    result = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    assert result.status is ProvisionStatus.NOT_PAID
    assert _sub_count(owner_engine, order_id) == 0


def test_unknown_product_ignored(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id, product="prod_unknown")})
    result = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    assert result.status is ProvisionStatus.UNKNOWN_PRODUCT
    assert _sub_count(owner_engine, order_id) == 0


def test_order_not_found_fails(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    result = provision_order(
        owner_session, order_id, salla_client=FakeSallaClient({}), product_catalog=CATALOG,
    )
    assert result.status is ProvisionStatus.ORDER_NOT_FOUND
    assert _sub_count(owner_engine, order_id) == 0


def _post_webhook(order_id: str, event: str) -> None:
    body = json.dumps({"event": event, "data": {"id": order_id}}).encode("utf-8")
    sig = compute_signature(body, SECRET)
    s = SessionLocal()
    try:
        receive_webhook(s, provider="salla", event_type=event,
                        raw_body=body, header_signature=sig, secret=SECRET)
    finally:
        s.close()


def test_end_to_end_duplicate_webhook_one_subscription(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    # Two identical paid webhooks arrive (Salla retry) → deduped at intake.
    _post_webhook(order_id, "order.payment.updated")
    _post_webhook(order_id, "order.payment.updated")
    results = process_pending_webhooks(
        owner_session, salla_client=client, product_catalog=CATALOG, expected_pricing=PRICING,
    )
    provisioned = [r for r in results if r.status is ProvisionStatus.PROVISIONED]
    assert len(provisioned) == 1
    assert _sub_count(owner_engine, order_id) == 1


def test_refund_suspends_immediately(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)

    _post_webhook(order_id, "order.refunded")
    process_pending_webhooks(owner_session, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)

    with Session(owner_engine) as s:
        status = s.execute(
            text("SELECT status FROM subscriptions WHERE salla_order_id = :o"),
            {"o": order_id},
        ).scalar_one()
    assert status == st.REFUNDED  # service off immediately


def test_cataloged_product_with_no_pricing_fails_closed(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT ح-4: a paid order for a cataloged product with NO pricing entry
    must NOT provision — it fails to manual review (was: check skipped)."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    result = provision_order(
        owner_session, order_id, salla_client=client,
        product_catalog=CATALOG, expected_pricing={},
    )
    assert result.status is ProvisionStatus.AMOUNT_MISMATCH


def test_bank_transfer_confirmation_event_provisions(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT ك-18: order.status.updated (merchant confirms the transfer)
    must provision — it was ignored forever."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    _post_webhook(order_id, "order.status.updated")
    results = process_pending_webhooks(
        owner_session, salla_client=client, product_catalog=CATALOG,
        expected_pricing=PRICING)
    assert results and results[0].status is ProvisionStatus.PROVISIONED


def test_tenant_code_survives_deletion_gap(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT ك-19: with tenants {2,7} present, the next code is TEN-0008 —
    never a reissued TEN-0003 (row-count scheme) that explodes the unique
    constraint."""
    from career.db.models import Tenant as _T
    for n in (2, 7):
        owner_session.add(_T(id=uuid.uuid4(), code=f"TEN-{n:04d}"))
    owner_session.commit()
    try:
        from career.salla.provisioning import _next_tenant_code
        assert _next_tenant_code(owner_session) == "TEN-0008"
    finally:
        from sqlalchemy import text as _t
        owner_session.execute(_t(
            "DELETE FROM tenants WHERE code IN ('TEN-0002', 'TEN-0007')"))
        owner_session.commit()
