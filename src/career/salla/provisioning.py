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
import time
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
from career.salla.client import (
    SallaApiError,
    SallaAuthError,
    SallaClient,
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


def _reconcile_existing(
    owner_session: Session,
    existing: Subscription,
    order_id: str,
    *,
    salla_client: SallaClient,
    webhook_event: WebhookEvent | None,
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
        sub_states.apply_order_lifecycle(
            owner_session, existing, event_type, salla_order_id=order_id,
        )
        _mark_webhook(owner_session, webhook_event, "processed")
        owner_session.commit()
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
        logger.warning("order amount/currency mismatch — not provisioning")
        _mark_webhook(owner_session, webhook_event, "failed")
        owner_session.commit()
        return ProvisionResult(ProvisionStatus.AMOUNT_MISMATCH)

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
        _alert(admin_client_hint, (
            "⚠️ طلب مدفوع بلا رقم جوال صالح — التفعيل التلقائي معطّل لهذا "
            "المشتري\nأرسل له رابط التفعيل يدويًا من القناة"
        ))

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

#: Salla's own token delivery. We do not consume it (storing a live credential
#: from a webhook payload is a decision with its own security design), but it
#: must never pass in silence: it is the ONLY moment a replacement credential
#: is ever offered to us.
_AUTHORIZE_EVENT = "app.store.authorize"

#: After Salla refuses or fails us, stop hammering it. The worker loop polls
#: every 3 seconds; without a pause a dead token means twenty pointless calls
#: a minute and twenty operator alerts. One minute is short enough that a
#: renewed token starts working almost immediately and long enough that the
#: alert reads as one problem, not a flood.
SALLA_BACKOFF_SECONDS = 60.0
_NOTIFY_INTERVALS = {"salla_down": SALLA_BACKOFF_SECONDS,
                     "token_expiring": 12 * 3600.0,
                     "authorize": 12 * 3600.0}
_last_notified: dict[str, float] = {}
_backoff_until: float = 0.0


def reset_salla_backoff() -> None:
    """Clear the process-local backoff and alert timers (tests, and a manual
    kick after the operator renews the token)."""
    global _backoff_until
    _backoff_until = 0.0
    _last_notified.clear()


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
    _alert(admin_client, (
        f"⚠️ توكن سلة يخلص خلال {days_left} يومًا — جدّده قبل ما يقف البيع"
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
                    waiting = int(owner_session.execute(
                        select(func.count(WebhookEvent.id)).where(
                            WebhookEvent.processing_status == "received"
                        )
                    ).scalar_one())
                    if isinstance(exc, SallaAuthError):
                        _alert(admin_client,
                               "🔴 سلة ترفض بيانات دخولنا — التوكن منتهٍ أو "
                               "ملغى\nالطلبات المدفوعة محفوظة ولم يُفقد منها "
                               "شيء، وتُعالج تلقائيًا بمجرد تجديد التوكن\n"
                               f"أحداث تنتظر الآن: {waiting}"
                               + _token_expiry_line())
                    else:
                        _alert(admin_client,
                               "🟠 سلة لا تستجيب — الطلبات المدفوعة محفوظة "
                               "وسنعيد المحاولة تلقائيًا\n"
                               f"أحداث تنتظر الآن: {waiting}")
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
    if whatsapp_client is not None and sub.order_phone_e164:
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
        if state is WindowState.OPEN:
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
        line = (
            f"⚠️ تجديد على حساب موقوف {code} — الاشتراك مربوط وينتظر مراجعتك "
            "قبل أي استئناف للخدمة"
        ) if review else f"↻ تجديد {code} — الفترة الجديدة تنتهي\n{until}"
        if not review and not told:
            line += "\nنافذة واتساب مقفلة — ما وصلت العميل رسالة تأكيد"
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
