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
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import ActivationToken, Subscription, SubscriptionEvent, Tenant, WebhookEvent
from career.salla import subscriptions as sub_states
from career.salla.client import (
    SallaApiError,
    SallaAuthError,
    SallaClient,
    SallaOrder,
)
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
    #: an already-provisioned order came back canceled/refunded — service off
    SERVICE_STOPPED = "service_stopped"
    #: Salla could not be asked (dead token, 5xx, timeout). The event is left
    #: 'received' and retried; NOTHING about it is final.
    DEFERRED = "deferred"
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


#: How many times to re-read max(code) and try again after a collision.
_TENANT_CODE_ATTEMPTS = 5


def _create_tenant(session: Session) -> Tenant:
    """Allocate the next TEN code and insert, surviving a collision.

    max(code) + 1 is a read-then-write, so two provisioning passes that
    overlap — a manual catch-up run beside the live loop, a restart overlap,
    a second host after scale-out — both read the same maximum and both try
    the same code. One INSERT wins; the other raised UniqueViolation, which
    the poison guard turned into a webhook marked 'failed' FOREVER: the losing
    buyer paid and got no tenant, no subscription and no way back short of
    hand-editing the database.

    A collision is not a poisoned payload, it is a race — so it is retried.
    Each attempt re-reads the maximum inside a SAVEPOINT, so a failed INSERT
    rolls back only itself and leaves the surrounding transaction usable.
    """
    from sqlalchemy.exc import IntegrityError

    last: Exception | None = None
    for _ in range(_TENANT_CODE_ATTEMPTS):
        try:
            with session.begin_nested():
                tenant = Tenant(
                    id=uuid.uuid4(), code=_next_tenant_code(session)
                )
                session.add(tenant)
                session.flush()
            return tenant
        except IntegrityError as exc:   # another pass took this code
            last = exc
            logger.warning("tenant code collided — retrying with a fresh max")
    raise last if last is not None else RuntimeError("tenant code exhausted")


def _mark_webhook(session: Session, webhook_event: WebhookEvent | None, status: str) -> None:
    if webhook_event is not None:
        webhook_event.processing_status = status
        webhook_event.processed_at = func.now()
        webhook_event.attempt_count = webhook_event.attempt_count + 1


#: Authoritative Salla status → the order lifecycle event it means. The client
#: has always computed these two (client._CANCELED / _REFUNDED) and nothing
#: had ever read them.
_STATUS_TO_ORDER_EVENT: dict[str, str] = {
    "canceled": "order.canceled",
    "refunded": "order.refunded",
}


def _code_of(session: Session, tenant_id: uuid.UUID) -> str:
    """TEN code for the admin channel (§15.13 — never a phone, never a name)."""
    tenant = session.get(Tenant, tenant_id)
    return tenant.code if tenant is not None else "?"


def _record_provision_audit(
    owner_session: Session,
    *,
    tenant_id: uuid.UUID,
    subscription_id: uuid.UUID,
    order: SallaOrder,
    plan_code: str,
    renewal: bool,
) -> None:
    """Constant 4, written down: this order was re-verified against the Salla
    API and it said ``paid``.

    The constant has always been enforced — ``provision_order`` calls
    ``salla_client.get_order`` and refuses anything that is not ``paid`` — but
    nothing recorded that the re-check HAPPENED for a given order. The
    subscription row proves a subscription exists; ``subscription_events``
    proves what state it moved through; neither can distinguish a provision
    that re-verified from one that believed a webhook. For a money control the
    difference is the whole control, and «we always do» is not evidence.

    In the caller's transaction on purpose. This row is created microseconds
    before ``owner_session.commit()``, in the same transaction as the
    subscription, the activation token and the webhook's ``processed`` mark. If
    that commit fails there is no subscription to have audited; binding the two
    is what stops the trail from claiming a sale the database does not have.

    No PII: the tenant is carried as a uuid the operator only ever sees as a
    TEN code, and the details are an order id, a plan and an amount.
    """
    from career.audit import ACTION_SUBSCRIPTION_PROVISIONED, record_audit

    try:
        record_audit(
            owner_session,
            tenant_id=tenant_id,
            actor="salla_worker",
            action=ACTION_SUBSCRIPTION_PROVISIONED,
            resource_type="subscription",
            resource_id=subscription_id,
            details={
                "salla_order_id": str(order.order_id),
                "plan_code": plan_code,
                "amount": str(order.amount),
                "currency": str(order.currency or ""),
                # The claim being recorded, not decoration: the status came
                # back from the Salla API, not from the webhook body.
                "reverified_status": str(order.status),
                "renewal": renewal,
            },
        )
    except Exception:  # noqa: BLE001 — a landed sale is never lost over a log
        logger.error("subscription provisioned but NOT audited", exc_info=True)


def _record_refund_audit(
    owner_session: Session,
    *,
    subscription: Subscription,
    event_type: str,
    source: str,
) -> None:
    """The money went back and the service stopped — recorded where it happens.

    One action for all three reversals (refund, cancellation, chargeback) with
    the event name in the details, because they are one fact from the only side
    that matters: the customer is no longer paying and we are no longer serving
    them. Splitting it into three actions would split the answer to «show me
    every reversal» across three queries and guarantee one of them is forgotten.

    Written from BOTH reversal paths, which is the point — ``_apply_lifecycle``
    handles an event that arrives under its own name, and ``_reconcile_existing``
    handles the one this store actually sends (``order.status.updated`` carrying
    a refunded status). A trail that only covered the first would be blind to
    the shape that occurs in production.

    In the caller's transaction, for the same reason as the provision row: it
    commits with the state change it describes.
    """
    from career.audit import ACTION_SUBSCRIPTION_REFUNDED, record_audit

    try:
        record_audit(
            owner_session,
            tenant_id=subscription.tenant_id,
            actor="salla_worker",
            action=ACTION_SUBSCRIPTION_REFUNDED,
            resource_type="subscription",
            resource_id=subscription.id,
            details={
                "salla_order_id": str(subscription.salla_order_id or ""),
                "event_type": event_type,
                "amount_charged": str(subscription.amount_sar or ""),
                "status_after": str(subscription.status),
                # Which path saw it — see the docstring.
                "source": source,
            },
        )
    except Exception:  # noqa: BLE001 — stopping service is never blocked
        logger.error("service stopped for a reversal but NOT audited",
                     exc_info=True)


def _reconcile_existing(
    owner_session: Session,
    existing: Subscription,
    order_id: str,
    *,
    salla_client: SallaClient,
    webhook_event: WebhookEvent | None,
    admin_client_hint: Any = None,
) -> ProvisionResult:
    """An event about an order we have already provisioned.

    This used to be a one-line short circuit: mark ``skipped_duplicate`` and
    return, without ever asking Salla anything. That made the money path
    asymmetric — ``paid`` was re-verified against the authoritative API while
    ``canceled``/``refunded`` was believed only if it arrived under its own
    event name. The one order event this store has actually delivered is
    ``order.status.updated``, and it is routed to provisioning; a cancellation
    or a refund carried by it therefore stopped nothing. The customer kept
    their founding seat, kept entering the nightly query families and kept
    receiving paid deliveries — with their money already returned.

    So the authoritative status is fetched for the duplicate too, and the same
    state machine that serves ``order.refunded`` is applied. The idempotency
    guarantee is untouched: no second subscription is created here under any
    status, and a subscription already in a terminal state is left alone.
    """
    order = salla_client.get_order(order_id)
    ids = {
        "subscription_id": str(existing.id),
        "tenant_id": str(existing.tenant_id),
    }
    if order is None:
        # We cannot see the order that our own subscription claims to come
        # from. Nothing to do safely, and nothing is destroyed by saying so.
        logger.error("provisioned order is not visible in salla anymore")
        _mark_webhook(owner_session, webhook_event, "failed")
        owner_session.commit()
        return ProvisionResult(ProvisionStatus.ORDER_NOT_FOUND, **ids)

    event_type = _STATUS_TO_ORDER_EVENT.get(order.status)
    if event_type is not None and existing.status not in sub_states.TERMINAL_STATES:
        charged = existing.amount_sar
        sub_states.apply_order_lifecycle(
            owner_session, existing, event_type, salla_order_id=order_id,
        )
        _record_refund_audit(
            owner_session, subscription=existing, event_type=event_type,
            source="status_reconcile",
        )
        _mark_webhook(owner_session, webhook_event, "processed")
        owner_session.commit()
        # Stopping service on ANY refund is the safe default — we must never
        # keep serving somebody whose money went back. But a PARTIAL refund
        # (a goodwill gesture, a prorated adjustment) is indistinguishable
        # from a full one here: the reconciler reads the order STATUS only,
        # and nothing in the payload tells us how much came back. So we stop,
        # and we say so loudly with both amounts, because the one case this
        # gets wrong — a customer who got 50 riyals back as an apology and
        # lost their whole subscription — is invisible otherwise and only the
        # operator can put it right.
        refunded = event_type == "order.refunded"
        _alert(admin_client_hint, (
            "🔻 أُوقفت الخدمة للعميل\n"
            f"{_code_of(owner_session, existing.tenant_id)}\n"
            f"السبب:\n{event_type}\n"
            f"المبلغ المدفوع أصلًا:\n{charged if charged is not None else '؟'}\n"
            f"مبلغ الطلب الآن:\n{order.amount}\n"
            + ("إن كان الاسترداد جزئيًا فالإيقاف غير مقصود — أعِد تفعيله يدويًا"
               if refunded else "إلغاء من المتجر — راجعه إن لم يكن بطلب العميل")
        ))
        return ProvisionResult(ProvisionStatus.SERVICE_STOPPED, **ids)

    _mark_webhook(owner_session, webhook_event, "skipped_duplicate")
    owner_session.commit()
    return ProvisionResult(ProvisionStatus.ALREADY_PROVISIONED, **ids)


def provision_order(
    owner_session: Session,
    order_id: str | None,
    *,
    salla_client: SallaClient,
    product_catalog: ProductCatalog,
    webhook_event: WebhookEvent | None = None,
    expected_pricing: ExpectedPricing | None = None,
    now: datetime | None = None,
    admin_client_hint: Any = None,
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
        return _reconcile_existing(
            owner_session, existing, order_id,
            salla_client=salla_client, webhook_event=webhook_event,
            admin_client_hint=admin_client_hint,
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
        # A PAID order for a product id we do not recognise. The old branch
        # dropped it in total silence — no tenant, no message, no alert — so a
        # mistyped id in the catalog meant the operator learned about it from
        # the customer complaining, and the event was already 'ignored' and
        # unretryable by then. A cataloged product with missing PRICING fails
        # loudly; this branch was the asymmetric one.
        logger.error("paid order for an uncataloged product — nothing served")
        _alert(admin_client_hint, (
            "🔴 طلب مدفوع لمنتج غير معروف في الكتالوج — ما تم تزويد شيء\n"
            f"معرّف المنتج:\n{order.product_id}\n"
            "راجع كتالوج المنتجات ثم أعد تشغيل الطلب يدويًا"
        ))
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
        # Before refusing: is this a FOUNDER renewing at the price the store
        # page locked for them? «سعره اليوم مقفول له: لو ارتفعت الأسعار يوم من
        # الأيام، ما ترتفع عليه — ما دام تجديده مستمر». Until price_locks
        # existed, the day prices rose was the day every founder renewal died
        # here — and AMOUNT_MISMATCH marks the webhook `failed`, which is
        # TERMINAL: they would have paid and received nothing, with the only
        # trace an alert blaming our own pricing. The guard is relaxed for
        # exactly one amount belonging to exactly one tenant on exactly one
        # plan; everything else still fails closed on the line below.
        from career.promises import price_lock
        from career.salla.activation_link import normalize_order_phone

        admitted = price_lock.admit_locked_amount(
            owner_session,
            order_phone=normalize_order_phone(order.customer_phone),
            plan_code=plan_code, amount=order.amount, currency=order.currency,
            expected_amount=exp_amount, expected_currency=exp_currency,
            now=now or datetime.now(UTC),
        )
        if not admitted.admitted:
            logger.error("order amount/currency mismatch — not provisioning")
            # The single highest-cost failure in the system — the buyer paid
            # and will be refused — was the ONE branch with no alert, while
            # the uncataloged-product and unusable-phone branches beside it
            # both shout. The most likely trigger is our own configuration:
            # the store sells at a price the environment does not expect.
            reason = (
                # The one refusal that is NOT a misconfiguration, and the
                # operator must not be sent to «correct» a price that is
                # already correct: this customer's lock died with their gap.
                "سبب الرفض: انقطع تجديده أكثر من المهلة فسقط قفل سعره — "
                "يرجع بسعر اليوم"
                if admitted.reason == "lapsed" else
                "الأغلب أن سعر المتجر لا يطابق التسعيرة في الإعدادات — "
                "صحّحها ثم أعد تشغيل الطلب"
            )
            _alert(admin_client_hint, (
                "🔴 طلب مدفوع بمبلغ أو عملة لا تطابق المتوقع — لم يُزوَّد\n"
                f"معرّف المنتج:\n{order.product_id}\n"
                f"المبلغ الوارد:\n{order.amount} {order.currency}\n"
                f"المتوقع:\n{exp_amount} {exp_currency}\n"
                + reason
            ))
            _mark_webhook(owner_session, webhook_event, "failed")
            owner_session.commit()
            return ProvisionResult(ProvisionStatus.AMOUNT_MISMATCH)
        # Provisioning at less than today's price is exactly the shape of a
        # mispriced order, so the operator hears it the moment it happens —
        # otherwise he either investigates a promise as a fault, or discovers
        # the difference in an accounting month with no explanation attached.
        _alert(admin_client_hint, price_lock.LOCK_HONOURED_ADMIN_AR.format(
            # admitted ⟹ a tenant was resolved; the fallback exists so a
            # future refactor cannot turn a missing code into a crash on the
            # one path that has already accepted somebody's money.
            code=(_code_of(owner_session, admitted.tenant_id)
                  if admitted.tenant_id is not None else "?"),
            paid=f"{order.amount} {order.currency}",
            today=f"{exp_amount} {exp_currency}",
        ))

    from career.salla.activation_link import normalize_order_phone

    order_phone = normalize_order_phone(order.customer_phone)
    if order_phone is None:
        # Zero-touch activation, the welcome template and the renewal match
        # ALL key on this column. Provisioning still proceeds — the money is
        # real and the typed-token path still works — but the operator has to
        # know, because the buyer will otherwise sit waiting for a message
        # that is never coming.
        logger.error("paid order carries no usable phone — zero-touch is off "
                     "for this buyer")
        # The old line said «أرسل له رابط التفعيل يدويًا من القناة» — from the
        # channel, where the link no longer is and must never be again. An
        # instruction pointing at a capability we deliberately removed is worse
        # than none: it sends the operator hunting through chat history at the
        # exact moment a paying customer is stuck.
        _alert(admin_client_hint, (
            "⚠️ طلب مدفوع بلا رقم جوال صالح — التفعيل التلقائي معطّل لهذا "
            "المشتري\nأصدر له رابط تفعيل من لوحة التحكم وسلّمه له مباشرة"
        ))

    # CHANGELOG §16 — is this the same human paying again? Decided BEFORE
    # anything is created: a renewal must not mint a second tenant, a second
    # founding seat, or a token that dies unclaimed.
    from career.promises import price_lock as price_locks
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
        # A renewal onto a pass this customer has never held is their FIRST
        # purchase of that pass, so it is where its lock is born (an upgrade
        # from لمّاح to لمّاح+ locks لمّاح+ at what they actually paid). A
        # renewal on the same pass finds the lock already there and captures
        # nothing — including the renewal that was just admitted BY that lock,
        # which must never re-record itself as a new price.
        price_locks.capture(
            owner_session, tenant_id=target.tenant_id, plan_code=plan_code,
            amount=order.amount, currency=order.currency,
            subscription_id=renewed.id, now=now or datetime.now(UTC),
        )
        # a renewal on a different pass must actually change the service
        renewals.resync_policy_limit(
            owner_session, tenant_id=target.tenant_id, plan_code=plan_code,
        )
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
        _record_provision_audit(
            owner_session, tenant_id=target.tenant_id,
            subscription_id=renewed.id, order=order, plan_code=plan_code,
            renewal=True,
        )
        # ── he paid again while his messages are switched off (§08 + D2.1) ──
        #
        # Activation raises this ticket too, but activation only happens on the
        # §04 upgrade or a re-buy — the RARE route. THIS is the common one: a
        # customer who silences us without cancelling his BILLING renews for as
        # long as his card lasts, and nothing downstream ever notices. The
        # night that follows closes SKIPPED_OPTED_OUT, which `engine/cli` lists
        # as an honest day state, so the run exits 0 — correctly, for a state
        # the customer chose — and the only alert he ever produced was the
        # opt-out page a month earlier.
        #
        # Imported rather than re-decided: same ticket kind, same dedupe on an
        # OPEN ticket for the CHANNEL (not the tenant — the §04 upgrade moves
        # the subscription to another tenant, and the channel is the identity
        # that survives it), same write-then-page ordering. Only the tail
        # differs, because on a renewal we have said nothing to him and cannot.
        #
        # INSIDE this transaction, before the commit below: the ticket and the
        # money land together or not at all, so there is no window in which the
        # renewal exists and the reason nobody knows about it does too.
        if order_phone is not None:
            from career.whatsapp.activation_flow import (
                RENEWAL_OPTED_OUT_TAIL,
                ticket_if_silenced,
            )
            ticket_if_silenced(
                owner_session, phone=order_phone,
                ten_code=_code_of(owner_session, target.tenant_id),
                admin_client=admin_client_hint,
                now=now or datetime.now(UTC), tail=RENEWAL_OPTED_OUT_TAIL,
            )
        _mark_webhook(owner_session, webhook_event, "processed")
        owner_session.commit()
        return ProvisionResult(
            ProvisionStatus.RENEWED,
            subscription_id=str(renewed.id),
            tenant_id=str(target.tenant_id),
        )

    # Provision: tenant → subscription(PAID_UNCLAIMED) → activation token.
    tenant = _create_tenant(owner_session)

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

    # «سعره اليوم مقفول له» — this IS «سعره اليوم», recorded at the only
    # moment it is unambiguous and only after the triple match above has
    # already confirmed the amount is the store's real price.
    price_locks.capture(
        owner_session, tenant_id=tenant.id, plan_code=plan_code,
        amount=order.amount, currency=order.currency,
        subscription_id=subscription.id, now=now or datetime.now(UTC),
    )

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

    _record_provision_audit(
        owner_session, tenant_id=tenant.id, subscription_id=subscription.id,
        order=order, plan_code=plan_code, renewal=False,
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

#: Salla's own token delivery, and the ONLY moment a replacement credential is
#: ever offered to us: «Easy Mode» never displays the token for anyone to copy.
#:
#: This used to be defined and never used, so the event fell through to the
#: final ``else`` and was marked ``ignored`` — terminal, re-read by nothing.
#: The comment that stood here called consuming it «a decision with its own
#: security design» and deferred it; the deferral then cost precisely what it
#: warned about, because the token captured by hand on 2026-07-15 expired on
#: 2026-07-29 with no refresh path and paid orders stopped provisioning.
#: CHANGELOG §29 settles the design (signature first, env file not database,
#: zero leakage, honest failure) and this module implements it in
#: :func:`_apply_authorize`.
_AUTHORIZE_EVENT = "app.store.authorize"

#: The other two app-lifecycle events, and what they actually carry —
#: read off the live rows this store delivered on 2026-07-15 rather than
#: assumed (``webhook_events`` in staging, keys only):
#:
#: * ``app.installed`` → data: id, app_name, app_type, app_description,
#:   app_scopes, installation_date, store_type. **No credential, nothing to
#:   act on** — it is the click before the authorize event, which arrives about
#:   a second later carrying everything. It keeps falling to the ``else`` and
#:   is marked ``ignored``, which is the honest status for an event we
#:   deliberately do nothing with.
#: * ``app.uninstalled`` → data: the same app fields plus installation_date,
#:   uninstallation_date and a ``refunded`` boolean. Also no credential — and
#:   that IS the news: the token we hold is dead from that moment, so every
#:   paid order from then on will wait forever. Nothing else in the system
#:   would say so.
_UNINSTALLED_EVENT = "app.uninstalled"

#: The one value of ``webhook_events.provider`` this worker owns.
#:
#: INCIDENT 2026-08-03: the sweep below selected purely on
#: ``processing_status == 'received'``, and ``webhook_events`` is one table
#: shared by every intake in webhooks/intake.py — today Salla and WhatsApp.
#: WhatsApp rows therefore entered the Salla routing, matched neither the
#: provision nor the lifecycle event names, and landed in the final ``else``
#: that marks a row ``ignored``: terminal, and re-read by nothing. Four
#: ``whatsapp/statuses`` events were destroyed that way between 08:05:41 and
#: 08:05:43 while the WhatsApp worker was inside an LLM turn — both workers
#: poll every three seconds, so whichever reached the row first won it.
#:
#: The predicate belongs in the SELECT and not in a filter over the fetched
#: rows: the batch is what ``limit`` measures, so a foreign backlog must not
#: be able to occupy it (see the limit note in process_pending_webhooks), and
#: a row this worker never selects is a row it can never mark.
SALLA_PROVIDER = "salla"

#: After Salla refuses or fails us, stop hammering it. The worker loop polls
#: every 3 seconds; without a pause a dead token means twenty pointless calls
#: a minute and twenty operator alerts. One minute is short enough that a
#: renewed token starts working almost immediately and long enough that the
#: alert reads as one problem, not a flood.
SALLA_BACKOFF_SECONDS = 60.0
_NOTIFY_INTERVALS = {"salla_down": SALLA_BACKOFF_SECONDS,
                     "token_expiring": 12 * 3600.0,
                     "authorize": 12 * 3600.0,
                     # A credential for another store is NOT a write failure and
                     # must not share the write failure's budget: one pending
                     # identity decision would otherwise silence a real disk
                     # error for twelve hours, and vice versa. Its own key, its
                     # own interval, and — unlike the others — text that changes
                     # as it ages (see _foreign_store_alert).
                     "authorize_foreign": 12 * 3600.0}
_last_notified: dict[str, float] = {}
_backoff_until: float = 0.0


@dataclass
class _StoreDecision:
    """One unresolved «whose credential is this?» question, while it lasts.

    Process-local and deliberately so: it holds nothing that must survive a
    restart. The row in ``webhook_events`` is the durable record — this is only
    what turns a repeated alert into an ageing one, exactly as
    ``scripts/alert_unit_failure.sh`` keeps first-seen/kills/last-sent beside a
    verdict so the second message can say «still», not say it again. That
    script needs a file because each alert is a new PROCESS; here the worker is
    one long-lived process and the alert timers beside this are already
    process-local, so a second mechanism would only be a second thing to get
    wrong. A restart re-bases the incident, which is honest: the operator is
    also the one who restarts.
    """

    #: (held store, offered store) — a change in either is a NEW question.
    stores: tuple[str | None, str | None]
    first_seen: float
    #: Distinct webhook rows refused for this pair. The retry loop touches the
    #: same row every three seconds, so counting attempts would report the
    #: worker's diligence; counting rows reports how many deliveries are
    #: actually waiting for a decision.
    events: set[str] = field(default_factory=set)
    alerts_sent: int = 0


_pending_store_decision: _StoreDecision | None = None


def reset_salla_backoff() -> None:
    """Clear the process-local backoff and alert timers (tests, and a manual
    kick after the operator renews the token)."""
    global _backoff_until, _pending_store_decision
    _backoff_until = 0.0
    _last_notified.clear()
    _pending_store_decision = None


def _due(key: str) -> bool:
    """Rate-limit an operator alert without ever suppressing the first one."""
    now = time.monotonic()
    last = _last_notified.get(key)
    if last is not None and now - last < _NOTIFY_INTERVALS[key]:
        return False
    _last_notified[key] = now
    return True


def _alert(admin_client: Any, text: str) -> None:
    if admin_client is None:
        return
    try:
        admin_client.send_admin(text)
    except Exception:  # noqa: BLE001 — alerting never blocks the money path
        logger.warning("salla operator alert failed", exc_info=True)


def _token_expiry_line() -> str:
    """The recorded expiry, if the operator wrote one down. Read lazily so
    tests and offline paths need no settings."""
    try:
        from career.config import get_settings

        recorded = (get_settings().salla_token_expires_at or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return f"\nانتهاء صلاحية التوكن المسجل:\n{recorded}" if recorded else ""


#: How long before the recorded expiry the operator starts hearing about it.
#: A token dies quietly — the first symptom is a paid order that provisions
#: nothing — so the warning has to arrive while renewing it is still routine.
TOKEN_EXPIRY_WARN_DAYS = 5


def _warn_if_token_expiring(admin_client: Any) -> None:
    """Say it BEFORE the money path breaks, not after.

    The recorded expiry is a plain date the operator writes down; there is no
    refresh flow yet (deferred by his own decision). If it is absent or
    unparseable we say nothing — inventing an alarm from a blank field would
    train him to ignore the channel. Rate-limited to twice a day by the
    shared ``_due`` timer, and an expiry already in the past keeps warning,
    because that is precisely when every new order is being deferred.
    """
    from datetime import date

    try:
        from career.config import get_settings

        recorded = (get_settings().salla_token_expires_at or "").strip()
        if not recorded:
            return
        expires = date.fromisoformat(recorded[:10])
    except Exception:  # noqa: BLE001 — a malformed note is not an incident
        return

    days_left = (expires - date.today()).days
    if days_left > TOKEN_EXPIRY_WARN_DAYS or not _due("token_expiring"):
        return
    if days_left < 0:
        _alert(admin_client, (
            "🔴 توكن سلة منتهي — الطلبات المدفوعة تنتظر ولا تُزوَّد\n"
            "جدّده وبيمشي كل شي المحفوظ تلقائيًا"
            f"{_token_expiry_line()}"
        ))
        return
    # The count on its own line (§16). A Latin numeral wedged between «خلال»
    # and «يومًا» reverses the whole sentence in the operator's client, and a
    # warning he cannot read is a warning that did not happen.
    _alert(admin_client, (
        "⚠️ توكن سلة قارب على الانتهاء — جدّده قبل ما يقف البيع\n"
        "الأيام المتبقية:\n"
        f"{days_left}"
        f"{_token_expiry_line()}"
    ))


def _quarantine(session: Session, ev: WebhookEvent, admin_client: Any) -> None:
    """Isolate ONE genuinely bad event so it cannot jam the queue (AUDIT ك-19).

    The counterpart of :func:`_defer_webhook`, and the distinction between
    them is the whole point of this module's error handling: ``failed`` is
    terminal and nothing in this codebase undoes it, so it is reserved for an
    event we could actually look at and could not process. A credential or a
    network problem never lands here — that order is fine and waits.
    """
    session.rollback()
    _mark_webhook(session, ev, "failed")
    session.commit()
    logger.error("provisioning crashed for one event", exc_info=True)
    _alert(admin_client, "🔴 حدث سلة مسموم عُزل — راجع السجل")


def _defer_webhook(session: Session, ev: WebhookEvent) -> None:
    """Leave a paid order exactly where it is.

    The whole point: ``processing_status`` stays 'received', which is the only
    status ``process_pending_webhooks`` selects, so the event is picked up
    again on the next pass and provisions itself the moment Salla answers.
    Nothing here is terminal, nothing is marked processed, nothing is thrown
    away — only the attempt counter moves, so the operator can see an order
    that has been waiting.
    """
    ev.attempt_count = ev.attempt_count + 1


#: The units that hold a Salla access token in memory. Both build their client
#: at boot (``HttpSallaClient`` bakes the token into an Authorization header),
#: so a freshly stored credential does not reach a running process. Restarting
#: is the operator's action — never this worker's.
_RESTART_HINT = "systemctl restart career-worker career-admin-bot"


def _verified(ev: WebhookEvent) -> bool:
    """Is this row allowed to have its payload read at all?

    **Called before every read of an authorize payload, and that ordering is
    not stylistic.** Every other event in this module carries a claim about
    money that we re-verify against the Salla API before acting; this one
    carries a live CREDENTIAL that nothing downstream can re-verify, so an
    unsigned body is not doubtful data — it is an attempted credential
    injection, and the only safe moment to refuse it is before we have looked
    at it. Parse first and «check later» would already have handed an attacker
    the shape of the branch: a token substring in an exception message, a
    length in a log line, a stored value one early-return away.

    Two conditions, because two things can be wrong. ``provider`` guards
    against a WhatsApp (or future provider) row reaching Salla's credential
    path — ``webhook_events`` is one shared table and that exact confusion
    destroyed four rows on 2026-08-03. ``signature_valid`` is the HMAC verdict
    recorded by ``webhooks/intake.py``, which is the ONLY writer of these rows
    and only ever writes True after ``salla/webhook.receive_webhook`` has
    verified the raw body against ``SALLA_WEBHOOK_SECRET``. Re-reading the
    recorded verdict here costs nothing and means the invariant survives a
    future intake path — a replay tool, a backfill, a fixture — that inserts
    rows without going through that door.
    """
    return ev.provider == SALLA_PROVIDER and bool(ev.signature_valid)


#: A Salla store id as every live row has ever carried it: a merchant number
#: (``1275699954``, ``855028708``), delivered as a JSON int. The pattern is
#: wider than «digits» so a future account id with a dash or a dot is still
#: read, and narrow enough that nothing which is not an identifier can pass:
#: no spaces, no newline (the env file is one KEY=VALUE per line), no braces
#: from a dict that ``str()`` would happily render into a stored value.
_STORE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

#: Every place a STORE id may be read from, in order.
#:
#: Today all six live rows carry ``merchant`` at the top level, so the first
#: path is the one that fires. The others are here because the whole point of
#: the guard below is that the payload SHAPE can move under us, and a fix that
#: only ever looks in one place converts a renamed key into a stopped sale
#: path. Every entry is a key whose NAME can only mean a store, so reading it
#: is not a guess: ``merchant`` may also arrive as Salla's merchant OBJECT
#: (``{"id": …, "domain": …}``), which is why a dict is followed to its ``id``.
#:
#: ``data.id`` is deliberately NOT here, and that omission is the important
#: one: it is the APP id (1410006361 on every delivery). Reading it would give
#: every credential from every store the same «store id» — a label that is
#: always present, always equal, and always wrong, which is worse than the
#: absence this guard is built to catch.
_STORE_ID_PATHS = (
    ("merchant",),
    ("store_id",),
    ("data", "merchant"),
    ("data", "store_id"),
)


def _offered_store_id(payload: dict[str, Any]) -> str | None:
    """Which store is this credential for? ``None`` when nothing can say.

    ``None`` is a verdict here, not a default. Salla signs every store's
    authorize with the SAME app secret, so a valid signature proves the body
    came from Salla and says nothing about whose grant is inside it; the store
    id is the only discriminator there is. Handing ``None`` to
    ``tokens.store_credentials`` reaches ``StoreIdentity.UNCLAIMED``, which
    means «the refresh path, rotating the grant we already hold» — a sentence
    that is true of the refresh timer and false of a webhook.
    """
    for path in _STORE_ID_PATHS:
        node: Any = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node = node.get("id")
        if isinstance(node, bool) or not isinstance(node, (int, str)):
            continue
        candidate = str(node).strip()
        if _STORE_ID_RE.match(candidate):
            return candidate
    return None


def _operator_confirmed(writer: Any) -> bool:
    """Has a human already signed for an identity this code cannot prove?

    ``scripts/consume_stored_authorize.py`` runs this very function with the
    operator's ``--allow-store-change`` bound onto ``tokens.store_credentials``
    as a ``functools.partial`` — documented there as «THE ONE ADDITION»,
    precisely so the consumption sequence is never duplicated. That flag
    already means «I accept an identity that cannot be proved»: the script
    treats ``UNKNOWN`` — a stored credential whose store was never recorded —
    as a crossing needing exactly this confirmation. A payload that names no
    store asks the same question from the other side, so it takes the same
    answer, and the operator sees both stores (or their absence) printed
    before he types it.

    Anything that is not that binding reads as «not confirmed»: the worker,
    a test double, any future caller. The guard fails closed.
    """
    bound = getattr(writer, "keywords", None) or {}
    return bool(bound.get("allow_store_change"))


#: What the operator actually types to resolve a store decision. Its own line
#: in every alert: a Latin command inside an Arabic sentence arrives scrambled
#: in his client (§16), and a command he has to re-assemble is a command he
#: will not run at 03:00.
_CONSUME_HINT = (
    ".venv/bin/python scripts/consume_stored_authorize.py "
    "--db-host 127.0.0.1 --db-port 5433 --event-id {event_id} "
    "--apply --allow-store-change"
)


def _foreign_store_alert(
    held_store: str | None, offered_store: str | None, event_id: Any,
    state: _StoreDecision,
) -> str:
    """The refusal, in the operator's words, and it ages.

    THIS IS NOT A WRITE FAILURE. The disk is fine, the permissions are fine,
    and the retry the worker keeps making can never succeed on its own —
    nothing changes until a human decides. Sending him to
    «راجع مساحة القرص» (the write-failure text this branch used to inherit)
    costs him a trip to the server to find nothing wrong, and it repeated
    unchanged twice a day for as long as he did not act.

    Three stages, and the reason there are three is that this state PERSISTS
    until somebody chooses:

    * **new** — what happened, what was not lost, and the command that accepts
      it. Sent immediately: a credential arriving for another store is often
      the operator himself, one minute ago, clicking install on the demo
      store, and that is the minute the message is worth most.
    * **reminder** — after twelve hours, and it says it is a reminder, how
      long the question has been open and how many deliveries are waiting.
      Identical text would have taught him to stop reading it.
    * **still open** — from the second reminder on (a full day of no
      decision), the message stops assuming the decision is merely pending and
      names the OTHER way out: removing the app from that store, which is the
      right answer when the install was a mistake and the one thing the first
      message does not say.

    Twelve hours is kept rather than shortened. Nothing is broken while this
    is open — the live credential is intact and paid orders keep provisioning
    — so this is a question, not an outage, and a question that shouts every
    hour is how the channel gets muted for the alert that IS an outage.
    """
    held_line = held_store or "غير مسجّل"
    offered_line = offered_store or "غير معروف"
    command = _CONSUME_HINT.format(event_id=event_id)
    hours = int((time.monotonic() - state.first_seen) // 3600)

    if state.alerts_sent == 0:
        return (
            "🟠 وصل توكن سلة لمتجر غير المتجر الذي نخدمه — ما انحفظ وما ضاع\n"
            "المتجر المحفوظ:\n"
            f"{held_line}\n"
            "المتجر في الحدث الواصل:\n"
            f"{offered_line}\n"
            "ما تغيّر شيء في ملف الأسرار، والبيع ماشي بالتوكن الحالي.\n"
            "التوكن الواصل محفوظ في سجل الحدث ولا يضيع، والقرار قرارك.\n"
            "إذا كنت تبي المنصة تشتغل على المتجر الواصل، شغّل على السيرفر:\n"
            f"{command}"
        )
    if state.alerts_sent == 1:
        return (
            "🔁 ما زال توكن متجر ثانٍ ينتظر قرارك — هذا تذكير وليس حدثًا "
            "جديدًا\n"
            "المتجر المحفوظ:\n"
            f"{held_line}\n"
            "المتجر في الحدث الواصل:\n"
            f"{offered_line}\n"
            "معلّق منذ ساعات عددها:\n"
            f"{hours}\n"
            "عدد الأحداث المنتظرة:\n"
            f"{len(state.events)}\n"
            "ولا شيء متعطل: التوكن الحالي سليم والطلبات تُزوَّد.\n"
            "لقبول المتجر الواصل، شغّل على السيرفر:\n"
            f"{command}"
        )
    return (
        "🟠 قرار متجر سلة ما زال مفتوحًا ولن ينحل من نفسه\n"
        "المتجر المحفوظ:\n"
        f"{held_line}\n"
        "المتجر في الحدث الواصل:\n"
        f"{offered_line}\n"
        "معلّق منذ ساعات عددها:\n"
        f"{hours}\n"
        "عدد الأحداث المنتظرة:\n"
        f"{len(state.events)}\n"
        "أمامك طريقان، وكلاهما ينهي التكرار:\n"
        "الأول: تقبل المتجر الواصل بهذا الأمر على السيرفر:\n"
        f"{command}\n"
        "الثاني: إذا كان التثبيت غلط، أزل التطبيق من ذاك المتجر وينتهي "
        "الموضوع.\n"
        "وإلى أن تختار، الحدث محفوظ كما هو وهذه الرسالة تتكرر."
    )


def _apply_authorize(
    owner_session: Session, ev: WebhookEvent, admin_client: Any
) -> None:
    """Consume Salla's Easy-Mode credential delivery (CHANGELOG §29).

    Six outcomes, and each one leaves the row in the status that is true:

    * **unsigned** → ``failed`` and a red alert. Terminal: a forged body will
      not become genuine on retry.
    * **no credential in the payload** → ``failed`` and a red alert. Also
      terminal, and also loud, because it means Salla changed the shape of the
      one event the sale path depends on.
    * **no store id in the payload** → ``failed`` and a red alert. Terminal
      for the same reason and one more: see the guard below.
    * **a credential for another store** → the row is left ``received``, the
      credential stays in it, and the operator gets a message about an
      identity decision — not about the disk.
    * **stored / already stored / superseded** → ``processed``.
    * **could not be written** → the row is left ``received``, exactly like a
      paid order Salla could not confirm. It is retried on the next pass and
      nothing about it is final. §29's fourth condition is this line: a
      persistence failure must never be marked processed as if it had worked.

    No branch logs, alerts or raises with a token in it. The values reach
    exactly one place — the 0600 env file — and are registered with the
    redaction filter on the way (``salla.tokens``).
    """
    global _pending_store_decision
    if not _verified(ev):
        # Nothing above this line has touched ev.payload, and nothing below
        # this branch does either.
        logger.error("unsigned salla app.store.authorize refused — the "
                     "credential in it was never read")
        _mark_webhook(owner_session, ev, "failed")
        owner_session.commit()
        _alert(admin_client, (
            "🔴 وصل حدث توكن سلة بلا توقيع صحيح — رُفض ولم يُقرأ\n"
            "لم يُحفظ منه شيء. إن كنت ثبّت التطبيق الآن فأعد التثبيت، "
            "وإلا فهذه محاولة حقن اعتماد"
        ))
        return

    from career.salla import tokens as salla_tokens

    payload = ev.payload if isinstance(ev.payload, dict) else {}
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    # Field names read off the live 2026-07-15 delivery, not from memory:
    # data.access_token / data.refresh_token / data.expires (an ABSOLUTE unix
    # epoch — 1785334959 decoded to the very day the operator recorded by
    # hand), and the store is `merchant` at the TOP level. `data.id` is the
    # APP id, not the store's, which is why it is not read here.
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_at = salla_tokens.parse_expiry(data.get("expires"))
    store_id = _offered_store_id(payload)

    if store_id is None and not _operator_confirmed(
        salla_tokens.store_credentials
    ):
        # A CREDENTIAL WE CANNOT ATTRIBUTE IS NOT STORED. It used to be: the
        # missing key produced store_id=None, which is StoreIdentity.UNCLAIMED
        # — «the refresh path, rotating the grant we already hold» — so a
        # signed body carrying any store's token replaced the live credential
        # of the store we sell from, marked the row processed and sent the
        # GREEN «a new token landed» alert. One payload change was the whole
        # distance to that, because the signature does not carry an identity:
        # Salla signs every store's authorize with the same app secret.
        #
        # TERMINAL, not deferred, and that is the argument the retry settles:
        # the row keeps the payload it arrived with, so every future pass
        # reads the same absent key and takes the same branch. A deferral
        # would be a three-second loop that cannot ever succeed, an
        # attempt_count climbing forever, and a repeating alert with no new
        # information in it — which is precisely the pathology fixed for the
        # foreign-store branch below. `failed` is what `_quarantine` reserves
        # for «we could look at it and could not process it», and this is that
        # case exactly. Nothing is destroyed by saying so: `failed` is not
        # deletion, the credential is still in the row, and
        # consume_stored_authorize.py reaches a non-pending row by name
        # (`--event-id`) — which is why the alert carries the id.
        logger.error("salla authorize event named no store — the credential in "
                     "it was not stored")
        _mark_webhook(owner_session, ev, "failed")
        owner_session.commit()
        # Said conditionally because «nothing was replaced» is only reassuring
        # while something is held: on a fresh host the same event means the
        # sale path is still not open, and telling him it is fine would be the
        # green alert's lie in another colour.
        kept_line = (
            "الاعتماد المحفوظ عندنا ما تغيّر، والبيع ماشي عليه.\n"
            if salla_tokens.current().present else
            "وما عندنا اعتماد محفوظ أصلًا — البيع ما زال متوقفًا.\n"
        )
        _alert(admin_client, (
            "🔴 وصل توكن سلة بلا رقم متجر — ما انحفظ ولا استبدل شيئًا\n"
            "التوقيع يثبت أن سلة أرسلته، لكنه لا يثبت لأي متجر هو، ورقم "
            "المتجر هو الدليل الوحيد.\n"
            f"{kept_line}"
            "التوكن الواصل محفوظ في سجل الحدث:\n"
            f"{ev.id}\n"
            "إذا كنت متأكدًا أنه لمتجرك، شغّل على السيرفر:\n"
            f"{_CONSUME_HINT.format(event_id=ev.id)}\n"
            "وإلا فأعد تثبيت التطبيق على المتجر ليصل تفويض جديد."
        ))
        return

    try:
        outcome = salla_tokens.store_credentials(
            access_token if isinstance(access_token, str) else "",
            refresh_token=(
                refresh_token if isinstance(refresh_token, str) else None
            ),
            expires_at=expires_at,
            store_id=store_id,
        )
    except salla_tokens.CredentialError:
        # The payload is not going to improve. Say what it means, not what it
        # contained: this is the sale path, and nothing else reports it.
        logger.error("salla authorize event carried no usable credential",
                     exc_info=True)
        _mark_webhook(owner_session, ev, "failed")
        owner_session.commit()
        _alert(admin_client, (
            "🔴 وصل حدث تفويض سلة بلا توكن صالح — البيع ما زال متوقفًا\n"
            "أعد تثبيت التطبيق على المتجر لإطلاق الحدث من جديد"
        ))
        return
    except salla_tokens.ForeignStoreCredential:
        # NOT a write failure, however it is classified. `ForeignStoreCredential`
        # subclasses `CredentialWriteError` on purpose — a new StoreOutcome
        # member would fall through to the GREEN «a new token landed» alert,
        # which is the one message that must never be sent when nothing was
        # written — and the row handling it inherits is right: keep it
        # `received`, keep the credential in it, count the attempt. Only the
        # WORDS were wrong, and they sent the operator to check disk space for
        # an identity decision that no disk can take.
        owner_session.rollback()
        _defer_webhook(owner_session, ev)
        owner_session.commit()
        held_store = salla_tokens.current().store_id
        state = _pending_store_decision
        if state is None or state.stores != (held_store, store_id):
            # A different pair is a different question: re-based rather than
            # continued, so «still open for 30 hours» can never be said about
            # something first seen a minute ago.
            state = _StoreDecision(stores=(held_store, store_id),
                                   first_seen=time.monotonic())
            _pending_store_decision = state
        state.events.add(str(ev.id))
        logger.warning("salla authorize event refused — it belongs to another "
                       "store; the credential is kept in the row and the "
                       "operator decides")
        if _due("authorize_foreign"):
            _alert(admin_client,
                   _foreign_store_alert(held_store, store_id, ev.id, state))
            state.alerts_sent += 1
        return
    except Exception:  # noqa: BLE001 — write failure, ours not Salla's
        # The credential is still sitting in this row. Leave it there.
        owner_session.rollback()
        _defer_webhook(owner_session, ev)
        owner_session.commit()
        logger.error("salla credential could not be persisted — the event is "
                     "kept and will be retried", exc_info=True)
        if _due("authorize"):
            _alert(admin_client, (
                "🔴 وصل توكن سلة ولم نتمكن من حفظه — الحدث محفوظ وسنعيد "
                "المحاولة\n"
                "راجع مساحة القرص وصلاحيات ملف الأسرار على السيرفر"
            ))
        return

    _mark_webhook(owner_session, ev, "processed")
    owner_session.commit()

    if outcome is salla_tokens.StoreOutcome.STALE:
        # A redelivered install event arriving after something newer was
        # stored. Nothing was written, and that is the correct result — but it
        # is worth one quiet line, because «Salla resent it and nothing
        # happened» must not be something the operator has to infer.
        logger.info("older salla authorize event ignored — a newer credential "
                    "is already stored")
        return
    if outcome is salla_tokens.StoreOutcome.UNCHANGED:
        logger.info("salla authorize event redelivered — stored credential "
                    "unchanged")
        return

    # A credential landed, so whatever store question was open is answered:
    # the next refusal is a new incident and says «new», not «still».
    _pending_store_decision = None

    left = salla_tokens.days_left()
    expiry_line = (
        f"\nتنتهي صلاحيته:\n{expires_at.date().isoformat()}" if expires_at else ""
    )
    # A negative number would print a LATIN minus inside an Arabic line — the
    # exact shape that scrambles the operator's client (§16, and the storm
    # alert learned it the hard way). An already-expired delivery is nearly
    # impossible and gets words instead of a signed number.
    # ...and «يومًا» trailing the number put the Latin digits back INSIDE an
    # Arabic line, which is the same defect one word further along. The unit
    # goes in the label, the number stands alone.
    left_line = (
        "\n⚠️ التوكن الواصل منتهي الصلاحية أصلًا" if left is not None and left < 0
        else f"\nالمتبقي بالأيام:\n{left}" if left is not None else ""
    )
    store_line = f"\nالمتجر:\n{store_id}" if store_id else ""
    _alert(admin_client, (
        "🟢 وصل توكن سلة جديد وانحفظ في ملف الأسرار"
        f"{store_line}{expiry_line}{left_line}\n"
        "لن تستعمله الخدمات قبل إعادة تشغيلها:\n"
        f"{_RESTART_HINT}"
    ))


def _apply_uninstalled(
    owner_session: Session, ev: WebhookEvent, admin_client: Any
) -> None:
    """The app was removed from the store: our credential is dead as of now.

    **Nothing is deleted.** Wiping the stored token here was considered and
    rejected: Salla redelivers app events (four arrived for one install on
    2026-07-15), so an uninstall redelivered AFTER a reinstall would destroy
    the credential the reinstall had just delivered — turning a recovered sale
    path back into a dead one, from a webhook about an event that was already
    over. The token stops working by itself the moment the merchant uninstalls;
    there is nothing for us to revoke, and a stale copy in a 0600 file is a
    smaller problem than a wiped live one.

    So the whole action is to be LOUD. Continuing to take orders on a store
    that has removed us is selling something we cannot deliver, and no other
    part of the system reports this: the symptom would be paid orders quietly
    deferring behind 401s.
    """
    if not _verified(ev):
        logger.error("unsigned salla app.uninstalled refused")
        _mark_webhook(owner_session, ev, "failed")
        owner_session.commit()
        return
    logger.error("the salla app was uninstalled — our access token is dead "
                 "and paid orders can no longer be provisioned")
    _mark_webhook(owner_session, ev, "processed")
    owner_session.commit()
    _alert(admin_client, (
        "🔴 أُزيل التطبيق من متجر سلة — التوكن مات والبيع واقف\n"
        "أي طلب مدفوع من الآن ينتظر ولا يُزوَّد. أعد تثبيت التطبيق ليصلنا "
        "توكن جديد تلقائيًا"
    ))


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
    wired to build it directly).

    **A paid order is never destroyed by an infrastructure failure.** Renewing
    the access token is the operator's job; the failure mode when it lapses is
    ours, and it used to be the worst one available: get_order raised 401, the
    blanket poison guard marked the webhook ``failed`` — the one status nothing
    in this codebase can undo — and told the operator a *poisoned payload* had
    been quarantined, pointing the investigation at the buyer's order instead
    of at the credential. No tenant, no subscription, no activation, no welcome
    message, and no way back short of hand-editing the database. Retryable
    failures (401/403, 429, 5xx, timeouts) now leave the event untouched and
    say plainly what is wrong; the orders sit there and provision themselves
    when the token is renewed.

    ``limit`` counts SALLA events, which is the only reading that means
    anything: while the query spanned the whole table a WhatsApp backlog could
    fill the batch and push every paid order past the cut on every three-second
    pass, so the order that could not be seen was also the order that could not
    be provisioned. Scoped to one provider, a hundred is a hundred real orders.
    """
    global _backoff_until

    _warn_if_token_expiring(admin_client)
    if time.monotonic() < _backoff_until:
        # Salla just refused or failed us. Asking again this second helps
        # nobody and buries the alert; the events are all still 'received'.
        return []

    events = list(
        owner_session.execute(
            select(WebhookEvent)
            .where(
                WebhookEvent.provider == SALLA_PROVIDER,
                WebhookEvent.processing_status == "received",
            )
            .order_by(WebhookEvent.received_at)
            .limit(limit)
        ).scalars().all()
    )
    results: list[ProvisionResult] = []
    for ev in events:
        if ev.event_type in (_AUTHORIZE_EVENT, _UNINSTALLED_EVENT):
            # First branch on purpose: the credential event must not be able
            # to sit behind anything, and it produces no ProvisionResult —
            # it is not an order, it is what makes orders possible.
            handler = (
                _apply_authorize if ev.event_type == _AUTHORIZE_EVENT
                else _apply_uninstalled
            )
            try:
                handler(owner_session, ev, admin_client)
            except Exception:  # noqa: BLE001 — one bad row never jams the queue
                # NOT _quarantine: that marks the row ``failed``, which is
                # terminal, and this row may be holding the only copy of a
                # live credential. An unexpected crash here is ours, so the
                # event is kept and retried exactly like a deferred order.
                owner_session.rollback()
                _defer_webhook(owner_session, ev)
                owner_session.commit()
                logger.error("salla app-lifecycle event crashed — kept for "
                             "retry", exc_info=True)
                if _due("authorize"):
                    _alert(admin_client,
                           "🔴 تعذّرت معالجة حدث تفويض/إزالة من سلة — الحدث "
                           "محفوظ وسنعيد المحاولة، راجع السجل")
            continue
        if ev.event_type in _PROVISION_EVENTS:
            try:
                result = provision_order(
                    owner_session, ev.salla_order_id,
                    salla_client=salla_client, product_catalog=product_catalog,
                    webhook_event=ev, expected_pricing=expected_pricing,
                    admin_client_hint=admin_client,
                )
            except SallaApiError as exc:
                if not exc.retryable:
                    _quarantine(owner_session, ev, admin_client)
                    continue
                # The order is fine. WE are broken. Touch nothing.
                owner_session.rollback()
                _defer_webhook(owner_session, ev)
                owner_session.commit()
                _backoff_until = time.monotonic() + SALLA_BACKOFF_SECONDS
                results.append(ProvisionResult(ProvisionStatus.DEFERRED))
                logger.error("salla unreachable — paid orders deferred, not "
                             "failed", exc_info=True)
                if _due("salla_down"):
                    # Salla's backlog, not the table's. Counting every waiting
                    # row told the operator that the WhatsApp queue depth was
                    # the number of paid orders stuck behind a dead token —
                    # hundreds, when the true answer was one.
                    waiting = int(owner_session.execute(
                        select(func.count(WebhookEvent.id)).where(
                            WebhookEvent.provider == SALLA_PROVIDER,
                            WebhookEvent.processing_status == "received",
                        )
                    ).scalar_one())
                    # The count is the whole point of these two alerts — «is
                    # this one stuck order or my entire morning?» — and it was
                    # the part his client scrambled, because Latin digits sat
                    # inside the Arabic sentence. Its own line (§16).
                    if isinstance(exc, SallaAuthError):
                        _alert(admin_client,
                               "🔴 سلة ترفض بيانات دخولنا — التوكن منتهٍ أو "
                               "ملغى\nالطلبات المدفوعة محفوظة ولم يُفقد منها "
                               "شيء، وتُعالج تلقائيًا بمجرد تجديد التوكن\n"
                               "أحداث تنتظر الآن:\n"
                               f"{waiting}"
                               + _token_expiry_line())
                    else:
                        _alert(admin_client,
                               "🟠 سلة لا تستجيب — الطلبات المدفوعة محفوظة "
                               "وسنعيد المحاولة تلقائيًا\n"
                               "أحداث تنتظر الآن:\n"
                               f"{waiting}")
                # every remaining event would hit the same wall
                break
            except Exception:  # noqa: BLE001 — AUDIT ك-19: a poisoned event
                # must not jam the whole queue in a 3-second retry loop
                _quarantine(owner_session, ev, admin_client)
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
            # AUDIT ك-19 again, on the refund path: this branch sat OUTSIDE
            # the poison guard, so one unexpected transition stopped every
            # later webhook — new paid orders included.
            try:
                _apply_lifecycle(owner_session, ev)
            except Exception:  # noqa: BLE001
                logger.error("lifecycle apply crashed for one event",
                             exc_info=True)
                owner_session.rollback()
                _mark_webhook(owner_session, ev, "failed")
                owner_session.commit()
                if admin_client is not None:
                    try:
                        admin_client.send_admin(
                            "🔴 حدث استرداد/إلغاء تعذّر تطبيقه وعُزل — راجع "
                            "الطلب يدويًا"
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning("lifecycle poison alert failed",
                                       exc_info=True)
        else:
            _mark_webhook(owner_session, ev, "ignored")
            owner_session.commit()
    return results


#: The renewal notice's tail when the customer has switched our messages OFF —
#: a different fact from a shut 24h window, and one he cannot undo by writing
#: to us. Direction-pure lines (§15.13 + the bidi rule); no phone, ever.
RENEWED_SILENCED_ADMIN_AR = (
    "⛔ موقف الرسائل بطلبه — ما وصلته رسالة ولا راح توصله فرص\n"
    "ما تنفتح إلا لما يرسل «تشغيل الرسائل» بنفسه — كلّمه بقناة ثانية\n"
    "التذكرة مفتوحة في شاشة التذاكر، وقرار الفلوس لك"
)


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

    # A free-form text only lands inside the 24h window, and must never reach
    # someone who opted out. Most renewals happen ON THE STOREFRONT, so the
    # window is usually shut — tell the operator that plainly instead of
    # pretending the customer was informed.
    told = False
    silenced = False
    if sub.order_phone_e164:
        from career.db.models import CustomerChannel
        from career.salla.renewal import (
            RENEWED_CUSTOMER_AR,
            RENEWED_PAUSED_AR,
            RENEWED_UNDER_REVIEW_AR,
        )
        from career.whatsapp.phones import phone_variants
        from career.whatsapp.window import WindowState, window_state

        channel = owner_session.execute(
            select(CustomerChannel).where(
                CustomerChannel.phone_e164.in_(
                    phone_variants(sub.order_phone_e164)
                ),
                CustomerChannel.provider == "whatsapp",
            ).order_by(CustomerChannel.created_at, CustomerChannel.id)
        ).scalars().first()
        state = window_state(
            last_inbound_at=channel.last_inbound_at,
            opt_out_at=channel.opt_out_at,
            now=datetime.now(UTC),
        ) if channel is not None else None
        # Read WITHOUT the WhatsApp client, deliberately: whether the customer
        # can be reached at all is a fact about him, not about which transports
        # this caller happened to wire, and the operator's line below is wrong
        # if it depends on that.
        silenced = state is WindowState.OPTED_OUT
        # A disputed account renews into SUSPENDED and waits for a human, so
        # «وبنكمل عادي» would be a promise of a daily service that is not
        # going to arrive. Tell them the truth: the payment landed, and
        # someone is looking at it.
        if sub.status == sub_states.SUSPENDED:
            head = RENEWED_UNDER_REVIEW_AR
        elif sub.status == sub_states.PAUSED:
            head = RENEWED_PAUSED_AR
        else:
            head = RENEWED_CUSTOMER_AR
        # OPTED_OUT never reaches here (window_state returns it before OPEN),
        # and that is the whole customer-facing answer on this path: he is sent
        # NOTHING. The activation welcome could go to a silenced number because
        # it was a direct reply to a message he had just typed; a renewal is a
        # charge from the storefront, so the same send would be us breaking his
        # instruction to make ourselves feel informed.
        if whatsapp_client is not None and state is WindowState.OPEN:
            try:
                whatsapp_client.send_text(
                    sub.order_phone_e164, f"{head}\n{until}"
                )
                told = True
            except Exception:  # noqa: BLE001
                logger.warning("renewal confirmation send failed",
                               exc_info=True)

    if admin_client is not None:
        review = sub.status == sub_states.SUSPENDED
        # Every line direction-pure (§16): the TEN code and the date each get
        # a line of their own. This notice fires on EVERY renewal, so it was
        # the one alert Fahad was guaranteed to receive scrambled — «↻ تجديد
        # TEN-0002 — الفترة الجديدة تنتهي» reverses in his client, and the
        # code he needs in order to know WHOSE renewal this is is the part
        # that moves. The fresh-provision notice beside it (_announce_provision)
        # has always been written this way; the renewal path was simply never
        # brought in line with it.
        line = (
            "⚠️ تجديد على حساب موقوف\n"
            f"{code}\n"
            "الاشتراك مربوط وينتظر مراجعتك قبل أي استئناف للخدمة"
        ) if review else (
            "↻ تجديد\n"
            f"{code}\n"
            "الفترة الجديدة تنتهي\n"
            f"{until}"
        )
        if not review and not told:
            # AUDIT D2.1. Both cases used to read «نافذة واتساب مقفلة — ما
            # وصلت العميل رسالة تأكيد», which is TRUE of a silenced customer
            # and tells the operator the wrong thing: a shut window reopens the
            # moment the customer writes anything, and this one never reopens
            # by itself — only he can lift it, and he has to be asked on some
            # other channel. Reported as the ordinary case, it was invisible.
            line += "\n" + (
                RENEWED_SILENCED_ADMIN_AR if silenced
                else "نافذة واتساب مقفلة — ما وصلت العميل رسالة تأكيد"
            )
        try:
            admin_client.send_admin(line)
        except Exception:  # noqa: BLE001
            logger.warning("renewal admin notice failed", exc_info=True)


class LinkIssueStatus(StrEnum):
    ISSUED = "issued"
    #: No tenant by that code, or none of their orders is waiting to be claimed.
    NO_UNCLAIMED_ORDER = "no_unclaimed_order"
    #: The order is already claimed — there is nothing left to hand over, and
    #: minting a link for a live account would be handing out a way in.
    ALREADY_ACTIVATED = "already_activated"


@dataclass(frozen=True)
class ActivationLinkIssue:
    status: LinkIssueStatus
    #: A LIVE credential — whoever holds it binds their phone to a paid
    #: subscription. Hand it to the buyer and to nobody else: never log it,
    #: never write it anywhere with a history.
    link: str | None = None
    tenant_code: str | None = None
    expires_at: datetime | None = None


#: An operator-issued link is handed over inside a conversation that is
#: happening right now, so it has no reason to outlive that conversation. The
#: seven days a thank-you-page link needs are seven days of standing risk here.
OPERATOR_LINK_TTL_MINUTES = 60


def issue_activation_link(
    owner_session: Session,
    *,
    tenant_code: str,
    whatsapp_number_e164: str,
    now: datetime | None = None,
) -> ActivationLinkIssue:
    """Mint a fresh activation link for one waiting order, on request.

    Until this existed, every fresh provision posted its raw activation token
    into the admin Telegram channel as a ``wa.me/…?text=تفعيل <token>`` deep
    link. The database was careful — only the SHA-256 hash is stored — and
    then the live value was written to a chat log that keeps it forever, for
    every sale, valid for seven days. Anyone who could read that channel could
    bind ANY phone to a paid subscription, and the secret-redaction filter
    could not save us: the token rides in a ``text=`` query parameter and
    matches no shape rule, so it went through untouched.

    The capability the operator actually needs is not a token in a log, it is
    the ability to get a buyer activated when zero-touch cannot. So the link is
    no longer broadcast and stored — it is ISSUED, on demand, one at a time,
    and it is a different token every time. That inverts the exposure: instead
    of a standing credential per sale it is a deliberate act with a name, an
    audit row, and an hour to live.

    Rotation is what makes on-demand issuance possible at all — the token
    minted at provisioning cannot be shown again, because we only kept its
    hash, which is exactly the property worth keeping. Any outstanding unused
    token for the order is retired first, and not only for tidiness:
    ``activate_by_order_phone`` looks up the subscription's single unused token
    with ``scalar_one_or_none``, so a second live token would raise inside the
    WhatsApp worker and break the zero-touch claim for that customer. Retiring
    is written as ``used_at``, which the schema gives us and which makes the
    old link answer «رمز التفعيل مستخدم مسبقًا» — a fail-closed answer, and an
    honest one. A ``revoked_at`` column would say it better and is worth a
    migration the day anything else needs the distinction.

    Returns the raw link exactly once, to the caller, and never touches a
    logger or the admin channel with it.
    """
    from career.audit import record_audit
    from career.salla.activation_link import build_activation_link

    now = now or datetime.now(UTC)
    tenant = owner_session.execute(
        select(Tenant).where(Tenant.code == tenant_code)
    ).scalars().first()
    if tenant is None:
        return ActivationLinkIssue(LinkIssueStatus.NO_UNCLAIMED_ORDER)

    subscription = owner_session.execute(
        select(Subscription).where(
            Subscription.tenant_id == tenant.id,
            Subscription.status == sub_states.PAID_UNCLAIMED,
        ).order_by(Subscription.created_at.desc())
    ).scalars().first()
    if subscription is None:
        claimed = owner_session.execute(
            select(Subscription.id).where(Subscription.tenant_id == tenant.id)
        ).first()
        return ActivationLinkIssue(
            LinkIssueStatus.ALREADY_ACTIVATED if claimed is not None
            else LinkIssueStatus.NO_UNCLAIMED_ORDER,
            tenant_code=tenant.code,
        )

    outstanding = list(owner_session.execute(
        select(ActivationToken).where(
            ActivationToken.subscription_id == subscription.id,
            ActivationToken.used_at.is_(None),
        )
    ).scalars().all())
    for old in outstanding:
        old.used_at = now

    raw_token = new_activation_token()
    expires_at = now + timedelta(minutes=OPERATOR_LINK_TTL_MINUTES)
    owner_session.add(
        ActivationToken(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            subscription_id=subscription.id,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
        )
    )
    record_audit(
        owner_session,
        tenant_id=tenant.id,
        actor="operator",
        action="activation_link_issued",
        resource_type="subscription",
        resource_id=subscription.id,
        details={
            "retired_tokens": len(outstanding),
            "ttl_minutes": OPERATOR_LINK_TTL_MINUTES,
        },
    )
    owner_session.commit()
    return ActivationLinkIssue(
        LinkIssueStatus.ISSUED,
        link=build_activation_link(
            whatsapp_number_e164=whatsapp_number_e164, token=raw_token
        ),
        tenant_code=tenant.code,
        expires_at=expires_at,
    )


def _announce_provision(
    owner_session: Session,
    result: ProvisionResult,
    *,
    admin_client: Any,
    whatsapp_number_e164: str,
    whatsapp_client: Any,
) -> None:
    """CHANGELOG §11 — zero-touch activation: send the APPROVED welcome
    template to the buyer's order phone, so their reply from that number claims
    the subscription, and tell the operator a sale landed. Best-effort:
    announcing never blocks billing.

    The announcement used to carry the raw activation deep link as a «support
    fallback». It carries no credential now — see issue_activation_link, which
    is where that fallback moved. What the operator needs from this message is
    only whether the buyer can claim the order by themselves; when they cannot,
    the message says so and names the one action that fixes it.
    """
    sub = owner_session.get(
        Subscription, uuid.UUID(str(result.subscription_id))
    ) if result.subscription_id else None
    tenant = owner_session.get(
        Tenant, uuid.UUID(str(result.tenant_id))
    ) if result.tenant_id else None
    code = tenant.code if tenant else "?"

    zero_touch = sub is not None and bool(sub.order_phone_e164)
    if whatsapp_client is not None and zero_touch and sub is not None:
        from career.whatsapp.delivery import record_out
        from career.whatsapp.templates import WELCOME_ACTIVATION
        try:
            mid = whatsapp_client.send_template(
                sub.order_phone_e164,
                WELCOME_ACTIVATION.name, WELCOME_ACTIVATION.language,
            )
        except Exception:  # noqa: BLE001
            logger.warning("welcome template send failed", exc_info=True)
        else:
            # The sharpest corner of the unbilled-template hole. This send
            # happens BEFORE any CustomerChannel exists — that is the entire
            # point of zero-touch: the buyer's reply is what creates the
            # channel — so `delivery_messages.channel_id NOT NULL` made it
            # literally unrecordable, and an order that is paid and never
            # activated therefore cost us one Meta template and appeared in the
            # bill as nothing at all. 100% of that order's template spend,
            # invisible. 0028 made the column nullable for this row and the
            # claim reminder that chases it.
            #
            # Its own commit: provision_order has already committed the money,
            # and this announcement runs after it. Accounting never undoes a
            # sale, so a failure here is logged and swallowed like every other
            # best-effort step in this function.
            try:
                record_out(
                    owner_session, tenant_id=sub.tenant_id, channel_id=None,
                    kind="template", wa_message_id=mid,
                    template_name=WELCOME_ACTIVATION.name,
                    now=datetime.now(UTC),
                )
                owner_session.commit()
            except Exception:  # noqa: BLE001 — never blocks a landed sale
                owner_session.rollback()
                logger.error("welcome template sent but NOT recorded — the "
                             "WhatsApp bill will under-report by one",
                             exc_info=True)

    if admin_client is not None:
        # Every line direction-pure (§16): the TEN code stands alone.
        line = (
            f"🟢 اشتراك جديد\n{code}\n"
            "أرسلنا له رسالة التفعيل — أي رد منه على نفس رقم الطلب يفعّل "
            "اشتراكه تلقائيًا"
        ) if zero_touch else (
            f"🟢 اشتراك جديد\n{code}\n"
            "⚠️ الطلب بلا رقم جوال صالح — التفعيل التلقائي معطّل لهذا المشتري\n"
            "أصدر له رابط تفعيل من لوحة التحكم وسلّمه له مباشرة"
        )
        try:
            admin_client.send_admin(line)
        except Exception:  # noqa: BLE001
            logger.warning("provision announcement failed", exc_info=True)


def _apply_lifecycle(owner_session: Session, ev: WebhookEvent) -> None:
    """Refund/cancel/chargeback → suspend the matching subscription immediately."""
    sub = owner_session.execute(
        select(Subscription).where(Subscription.salla_order_id == ev.salla_order_id)
    ).scalar_one_or_none()
    if sub is None:
        _mark_webhook(owner_session, ev, "ignored")  # nothing to suspend
        owner_session.commit()
        return
    # Read BEFORE the transition: apply_order_lifecycle is idempotent and
    # leaves an already-terminal subscription untouched, so without this the
    # audit trail would grow a fresh «the money went back» row every time a
    # reversal webhook was redelivered. One reversal, one row.
    already_reversed = sub.status in sub_states.TERMINAL_STATES
    sub_states.apply_order_lifecycle(
        owner_session, sub, ev.event_type, salla_order_id=ev.salla_order_id
    )
    if not already_reversed:
        _record_refund_audit(
            owner_session, subscription=sub, event_type=ev.event_type,
            source="lifecycle_webhook",
        )
    _mark_webhook(owner_session, ev, "processed")
    owner_session.commit()
