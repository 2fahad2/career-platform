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

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import ActivationToken, Subscription, SubscriptionEvent, Tenant, WebhookEvent
from career.salla import subscriptions as sub_states
from career.salla.client import SallaClient
from career.tokens import hash_token, new_activation_token

# product_id -> plan_code (from the Salla store setup; injected).
logger = logging.getLogger("career.salla")

ProductCatalog = dict[str, str]
#: product id → (expected amount, expected currency) — the §09 triple match.
ExpectedPricing = dict[str, tuple[Decimal, str]]

ACTIVATION_TOKEN_TTL_DAYS = 7


class ProvisionStatus(StrEnum):
    PROVISIONED = "provisioned"
    RENEWED = "renewed"
    ALREADY_PROVISIONED = "already_provisioned"
    NOT_PAID = "not_paid"
    UNKNOWN_PRODUCT = "unknown_product"
    ORDER_NOT_FOUND = "order_not_found"
    NO_ORDER_ID = "no_order_id"
    AMOUNT_MISMATCH = "amount_mismatch"


@dataclass(frozen=True)
class ProvisionResult:
    status: ProvisionStatus
    subscription_id: str | None = None
    tenant_id: str | None = None
    # The raw activation token is returned exactly once, only on a fresh
    # provision, to build the WhatsApp deep link. It is never stored raw.
    activation_token: str | None = None


def _next_tenant_code(session: Session) -> str:
    """AUDIT ك-19: max existing suffix + 1 — the old row-count scheme reissued
    a taken code after ANY tenant deletion, exploding the unique constraint
    and re-poisoning the queue every 3s. TEN-0001 stays reserved for the
    operator: real codes never go below TEN-0002."""
    codes = session.execute(select(Tenant.code)).scalars().all()
    top = 0
    for code in codes:
        tail = code.rsplit("-", 1)[-1]
        if tail.isdigit():
            top = max(top, int(tail))
    return f"TEN-{max(top, 1) + 1:04d}"


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
    expected_pricing: ExpectedPricing | None = None,
    now: datetime | None = None,
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

    # §09 binding pattern: match (product, amount, currency). AUDIT ح-4:
    # FAIL CLOSED — a cataloged product with NO pricing entry is a mismatch
    # (was: check silently skipped, so a tampered/discounted paid order
    # provisioned at any amount when the pricing env var was absent).
    expected = (expected_pricing or {}).get(order.product_id)
    if expected is None:
        logger.error(
            "no expected pricing for cataloged product — failing closed"
        )
        _mark_webhook(owner_session, webhook_event, "failed")
        owner_session.commit()
        return ProvisionResult(ProvisionStatus.AMOUNT_MISMATCH)
    exp_amount, exp_currency = expected
    if (order.amount != exp_amount
            or (order.currency or "").upper() != exp_currency.upper()):
        logger.warning("order amount/currency mismatch — not provisioning")
        _mark_webhook(owner_session, webhook_event, "failed")
        owner_session.commit()
        return ProvisionResult(ProvisionStatus.AMOUNT_MISMATCH)

    from career.salla.activation_link import normalize_order_phone

    order_phone = normalize_order_phone(order.customer_phone)

    # CHANGELOG §16 — is this the same human paying again? Decided BEFORE
    # anything is created: a renewal must not mint a second tenant, a second
    # founding seat, or a token that dies unclaimed.
    from career.salla import renewal as renewals

    target = renewals.find_renewal(
        owner_session, order_phone=order_phone, plan_code=plan_code,
        now=now or datetime.now(UTC),
    )
    if target is not None:
        renewals.close_previous(
            owner_session, target, salla_order_id=order_id
        )
        renewed = Subscription(
            id=uuid.uuid4(),
            tenant_id=target.tenant_id,
            plan_code=plan_code,
            status=target.new_status,
            salla_order_id=order_id,
            amount_sar=order.amount,
            currency=order.currency,
            order_phone_e164=order_phone,
            current_period_start=target.period_start,
            current_period_end=target.period_end,
        )
        owner_session.add(renewed)
        owner_session.flush()
        owner_session.add(
            SubscriptionEvent(
                id=uuid.uuid4(),
                tenant_id=target.tenant_id,
                subscription_id=renewed.id,
                event_type="renewal_provisioned",
                from_status=None,
                to_status=target.new_status,
                salla_order_id=order_id,
                details={
                    "plan_code": plan_code,
                    "previous_subscription_id": str(target.previous_id),
                    "period_end": target.period_end.isoformat(),
                },
            )
        )
        _mark_webhook(owner_session, webhook_event, "processed")
        owner_session.commit()
        return ProvisionResult(
            ProvisionStatus.RENEWED,
            subscription_id=str(renewed.id),
            tenant_id=str(target.tenant_id),
        )

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
        order_phone_e164=order_phone,
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


# AUDIT ك-18: order.status.updated carries the merchant's bank-transfer
# confirmation — provisioning re-verifies status=="paid" via the Salla
# API, so routing it here is safe and idempotent (was: ignored forever,
# so a bank-transfer buyer never provisioned).
_PROVISION_EVENTS = frozenset(
    {"order.payment.updated", "order.created", "order.status.updated"}
)
_LIFECYCLE_EVENTS = frozenset(sub_states._ORDER_EVENT_TO_STATE)


def process_pending_webhooks(
    owner_session: Session,
    *,
    salla_client: SallaClient,
    product_catalog: ProductCatalog,
    limit: int = 100,
    admin_client: Any = None,
    whatsapp_number_e164: str = "",
    whatsapp_client: Any = None,
    expected_pricing: ExpectedPricing | None = None,
) -> list[ProvisionResult]:
    """The Salla worker the webhook 200 defers to. Provisions paid orders and
    applies refund/cancel/chargeback to existing subscriptions. Runs as owner
    (bypasses RLS) since it spans tenants and creates new ones. On a fresh
    provision it emits the §09 activation deep link to the admin channel so
    the operator can hand it to the buyer (until Salla's thank-you page is
    wired to build it directly)."""
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
            try:
                result = provision_order(
                    owner_session, ev.salla_order_id,
                    salla_client=salla_client, product_catalog=product_catalog,
                    webhook_event=ev, expected_pricing=expected_pricing,
                )
            except Exception:  # noqa: BLE001 — AUDIT ك-19: a poisoned event
                # must not jam the whole queue in a 3-second retry loop
                logger.error("provisioning crashed for one event",
                             exc_info=True)
                owner_session.rollback()
                _mark_webhook(owner_session, ev, "failed")
                owner_session.commit()
                if admin_client is not None:
                    try:
                        admin_client.send_admin(
                            "🔴 حدث سلة مسموم عُزل (failed) — راجع السجل"
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning("poison alert failed", exc_info=True)
                continue
            if (result.status == ProvisionStatus.AMOUNT_MISMATCH
                    and admin_client is not None):
                try:
                    admin_client.send_admin(
                        "🔴 طلب مدفوع بمبلغ/عملة مخالفة — لم يُزوَّد، راجع الطلب"
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("mismatch alert failed", exc_info=True)
            results.append(result)
            if result.status == ProvisionStatus.PROVISIONED:
                _announce_provision(
                    owner_session, result,
                    admin_client=admin_client,
                    whatsapp_number_e164=whatsapp_number_e164,
                    whatsapp_client=whatsapp_client,
                )
            elif result.status == ProvisionStatus.RENEWED:
                _announce_renewal(
                    owner_session, result,
                    admin_client=admin_client,
                    whatsapp_client=whatsapp_client,
                )
        elif ev.event_type in _LIFECYCLE_EVENTS:
            _apply_lifecycle(owner_session, ev)
        else:
            _mark_webhook(owner_session, ev, "ignored")
            owner_session.commit()
    return results


def _announce_renewal(
    owner_session: Session,
    result: ProvisionResult,
    *,
    admin_client: Any,
    whatsapp_client: Any,
) -> None:
    """§16 — tell the renewing customer their days are safe, and tell the
    operator which TEN code renewed. No activation token and no welcome
    template: nothing about their account changed, and asking them to
    activate again would be the dead end this feature removes.

    Best-effort, exactly like the fresh-provision announcement: the money is
    already recorded and committed, so a failed send never rolls it back.
    Outside the 24h window the text simply will not land — the customer still
    sees the renewal reflected in «حالة اشتراكي» whenever they write.
    """
    sub = owner_session.get(
        Subscription, uuid.UUID(str(result.subscription_id))
    ) if result.subscription_id else None
    tenant = owner_session.get(
        Tenant, uuid.UUID(str(result.tenant_id))
    ) if result.tenant_id else None
    if sub is None:
        return
    code = tenant.code if tenant else "?"
    period_end = sub.current_period_end
    until = period_end.date().isoformat() if period_end is not None else "—"

    if whatsapp_client is not None and sub.order_phone_e164:
        from career.salla.renewal import RENEWED_CUSTOMER_AR, RENEWED_PAUSED_AR

        head = (
            RENEWED_PAUSED_AR if sub.status == sub_states.PAUSED
            else RENEWED_CUSTOMER_AR
        )
        try:
            whatsapp_client.send_text(sub.order_phone_e164, f"{head}\n{until}")
        except Exception:  # noqa: BLE001
            logger.warning("renewal confirmation send failed", exc_info=True)

    if admin_client is not None:
        review = sub.status == sub_states.PAID_UNCLAIMED
        line = (
            f"⚠️ تجديد على حساب موقوف {code} — الاشتراك مربوط وينتظر مراجعتك "
            "قبل أي استئناف للخدمة"
        ) if review else f"↻ تجديد {code} — الفترة الجديدة تنتهي\n{until}"
        try:
            admin_client.send_admin(line)
        except Exception:  # noqa: BLE001
            logger.warning("renewal admin notice failed", exc_info=True)


def _announce_provision(
    owner_session: Session,
    result: ProvisionResult,
    *,
    admin_client: Any,
    whatsapp_number_e164: str,
    whatsapp_client: Any,
) -> None:
    """CHANGELOG §11 — zero-touch activation: send the APPROVED welcome
    template to the buyer's order phone (their reply from that number claims
    the subscription), and surface the wa.me deep link to the admin channel
    as the support fallback. Best-effort: announcing never blocks billing."""
    sub = owner_session.get(
        Subscription, uuid.UUID(str(result.subscription_id))
    ) if result.subscription_id else None
    tenant = owner_session.get(
        Tenant, uuid.UUID(str(result.tenant_id))
    ) if result.tenant_id else None
    code = tenant.code if tenant else "?"

    if whatsapp_client is not None and sub is not None and sub.order_phone_e164:
        from career.whatsapp.templates import WELCOME_ACTIVATION
        try:
            whatsapp_client.send_template(
                sub.order_phone_e164,
                WELCOME_ACTIVATION.name, WELCOME_ACTIVATION.language,
            )
        except Exception:  # noqa: BLE001
            logger.warning("welcome template send failed", exc_info=True)

    if admin_client is not None and whatsapp_number_e164 and result.activation_token:
        from career.salla.activation_link import build_activation_link
        try:
            link = build_activation_link(
                whatsapp_number_e164=whatsapp_number_e164,
                token=result.activation_token,
            )
            admin_client.send_admin(
                f"🟢 اشتراك جديد {code} — رابط التفعيل الاحتياطي:\n{link}"
            )
        except Exception:  # noqa: BLE001
            logger.warning("activation link surface failed", exc_info=True)


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
