"""Provisioning — turn a paid Salla order into a subscription (whitepaper §09).

Runs as a worker AFTER the webhook 200. It re-verifies the order via the Salla
API (never trusts the webhook amounts/status), provisions ONLY when
payment=paid, and is idempotent: the same order never creates a second
subscription (subscriptions.salla_order_id is unique, plus a pre-check).

Provisioning runs with the owner engine because it creates the tenant itself
(the tenant does not exist yet, so RLS cannot bind it). It creates:
tenant → subscription(PAID_UNCLAIMED) → activation token (raw returned once,
only its hash stored).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import ActivationToken, Subscription, SubscriptionEvent, Tenant, WebhookEvent
from career.salla import subscriptions as sub_states
from career.salla.client import SallaClient
from career.tokens import hash_token, new_activation_token

# product_id -> plan_code (from the Salla store setup; injected).
ProductCatalog = dict[str, str]

ACTIVATION_TOKEN_TTL_DAYS = 7


class ProvisionStatus(StrEnum):
    PROVISIONED = "provisioned"
    ALREADY_PROVISIONED = "already_provisioned"
    NOT_PAID = "not_paid"
    UNKNOWN_PRODUCT = "unknown_product"
    ORDER_NOT_FOUND = "order_not_found"
    NO_ORDER_ID = "no_order_id"


@dataclass(frozen=True)
class ProvisionResult:
    status: ProvisionStatus
    subscription_id: str | None = None
    tenant_id: str | None = None
    # The raw activation token is returned exactly once, only on a fresh
    # provision, to build the WhatsApp deep link. It is never stored raw.
    activation_token: str | None = None


def _next_tenant_code(session: Session) -> str:
    # TEN-0001 is reserved for the operator (canary); real customers start above.
    count = session.execute(select(func.count()).select_from(Tenant)).scalar_one()
    return f"TEN-{count + 1:04d}"


def _mark_webhook(session: Session, webhook_event: WebhookEvent | None, status: str) -> None:
    if webhook_event is not None:
        webhook_event.processing_status = status
        webhook_event.processed_at = func.now()
        webhook_event.attempt_count = webhook_event.attempt_count + 1


def provision_order(
    owner_session: Session,
    order_id: str | None,
    *,
    salla_client: SallaClient,
    product_catalog: ProductCatalog,
    webhook_event: WebhookEvent | None = None,
) -> ProvisionResult:
    if not order_id:
        _mark_webhook(owner_session, webhook_event, "ignored")
        owner_session.commit()
        return ProvisionResult(ProvisionStatus.NO_ORDER_ID)

    # Idempotency: this order already provisioned?
    existing = owner_session.execute(
        select(Subscription).where(Subscription.salla_order_id == order_id)
    ).scalar_one_or_none()
    if existing is not None:
        _mark_webhook(owner_session, webhook_event, "skipped_duplicate")
        owner_session.commit()
        return ProvisionResult(
            ProvisionStatus.ALREADY_PROVISIONED,
            subscription_id=str(existing.id),
            tenant_id=str(existing.tenant_id),
        )

    # Re-verify the order against Salla — authoritative source (never the webhook).
    order = salla_client.get_order(order_id)
    if order is None:
        _mark_webhook(owner_session, webhook_event, "failed")
        owner_session.commit()
        return ProvisionResult(ProvisionStatus.ORDER_NOT_FOUND)
    if order.status != "paid":
        _mark_webhook(owner_session, webhook_event, "ignored")
        owner_session.commit()
        return ProvisionResult(ProvisionStatus.NOT_PAID)

    plan_code = product_catalog.get(order.product_id)
    if plan_code is None:
        _mark_webhook(owner_session, webhook_event, "ignored")
        owner_session.commit()
        return ProvisionResult(ProvisionStatus.UNKNOWN_PRODUCT)

    # Provision: tenant → subscription(PAID_UNCLAIMED) → activation token.
    tenant = Tenant(id=uuid.uuid4(), code=_next_tenant_code(owner_session))
    owner_session.add(tenant)
    owner_session.flush()

    subscription = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        plan_code=plan_code,
        status=sub_states.PAID_UNCLAIMED,
        salla_order_id=order_id,
        amount_sar=order.amount,
        currency=order.currency,
    )
    owner_session.add(subscription)
    owner_session.flush()

    owner_session.add(
        SubscriptionEvent(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            subscription_id=subscription.id,
            event_type="provisioned",
            from_status=None,
            to_status=sub_states.PAID_UNCLAIMED,
            salla_order_id=order_id,
            details={"plan_code": plan_code},
        )
    )

    raw_token = new_activation_token()
    owner_session.add(
        ActivationToken(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            subscription_id=subscription.id,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(days=ACTIVATION_TOKEN_TTL_DAYS),
        )
    )

    _mark_webhook(owner_session, webhook_event, "processed")
    owner_session.commit()
    return ProvisionResult(
        ProvisionStatus.PROVISIONED,
        subscription_id=str(subscription.id),
        tenant_id=str(tenant.id),
        activation_token=raw_token,
    )


_PROVISION_EVENTS = frozenset({"order.payment.updated", "order.created"})
_LIFECYCLE_EVENTS = frozenset(sub_states._ORDER_EVENT_TO_STATE)


def process_pending_webhooks(
    owner_session: Session,
    *,
    salla_client: SallaClient,
    product_catalog: ProductCatalog,
    limit: int = 100,
) -> list[ProvisionResult]:
    """The Salla worker the webhook 200 defers to. Provisions paid orders and
    applies refund/cancel/chargeback to existing subscriptions. Runs as owner
    (bypasses RLS) since it spans tenants and creates new ones."""
    events = list(
        owner_session.execute(
            select(WebhookEvent)
            .where(WebhookEvent.processing_status == "received")
            .order_by(WebhookEvent.received_at)
            .limit(limit)
        ).scalars().all()
    )
    results: list[ProvisionResult] = []
    for ev in events:
        if ev.event_type in _PROVISION_EVENTS:
            results.append(
                provision_order(
                    owner_session, ev.salla_order_id,
                    salla_client=salla_client, product_catalog=product_catalog,
                    webhook_event=ev,
                )
            )
        elif ev.event_type in _LIFECYCLE_EVENTS:
            _apply_lifecycle(owner_session, ev)
        else:
            _mark_webhook(owner_session, ev, "ignored")
            owner_session.commit()
    return results


def _apply_lifecycle(owner_session: Session, ev: WebhookEvent) -> None:
    """Refund/cancel/chargeback → suspend the matching subscription immediately."""
    sub = owner_session.execute(
        select(Subscription).where(Subscription.salla_order_id == ev.salla_order_id)
    ).scalar_one_or_none()
    if sub is None:
        _mark_webhook(owner_session, ev, "ignored")  # nothing to suspend
        owner_session.commit()
        return
    sub_states.apply_order_lifecycle(
        owner_session, sub, ev.event_type, salla_order_id=ev.salla_order_id
    )
    _mark_webhook(owner_session, ev, "processed")
    owner_session.commit()
