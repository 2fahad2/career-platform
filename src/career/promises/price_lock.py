"""The founder price lock — «سعره اليوم مقفول له».

The founding-seat block on the store front page promises three things, and
this is the one with money in it: «سعره اليوم مقفول له: لو ارتفعت الأسعار يوم
من الأيام، ما ترتفع عليه — ما دام تجديده مستمر». The same block defines both
of its own boundaries, and both are implemented here verbatim rather than
interpreted: «الكرسي يُحسب على اشتراكَي لمّاح ولمّاح+. التقييم ما يحسب كرسي»,
and «"تجديد مستمر" يعني: تجدد قبل نهاية اشتراكك أو خلال ٧ أيام بعده».
PRODUCTS-SHEET finishes the sentence for the case where continuity breaks:
«انقطعت أكثر؟ الكرسي ينفتح لغيرك، وترجع بسعر يومها».

`grep price_lock` returned zero before this module — and the absence was not
a missing feature, it was an outage waiting for the first price rise. §09
binds a paid order with a triple match (product, amount, currency) against
ONE global price per product. Raise the store price and the founder who
renews at the locked old amount fails that match; `AMOUNT_MISMATCH` marks the
webhook `failed`, which is TERMINAL — no retry reads it, no sweep revisits
it. The founder pays, receives nothing, keeps no subscription, and the only
trace is an alert saying our own pricing looks misconfigured. The promise
inverted into the worst failure the payment path has.

This deliberately relaxes a financial guard, so every rule below is written
to keep the relaxation as narrow as it can be and still be the promise:

The lock belongs to a **specific tenant and a specific plan**, resolved from
the order phone through the same identity the renewal path uses — never from
anything the buyer can type into an order. It admits **one exact amount**,
not a range, not a maximum discount: an amount matching neither the current
price nor that tenant's locked price still fails closed exactly as before.
The locked amount must be **no higher than today's price**, so the mechanism
can only ever protect a customer from a rise and can never be used to accept
an overpayment. It only ever applies to a **pass plan**, because the analysis
product has no seat and no renewal. And it dies with continuity: a lock whose
gap exceeds the published seven days is stamped lapsed and the order fails
closed — «وترجع بسعر يومها».
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import PriceLock, Subscription
from career.salla.renewal import RENEWABLE_PLANS, tenant_subscriptions
from career.whatsapp.phones import phone_variants

logger = logging.getLogger("career.promises")

#: «تجدد قبل نهاية اشتراكك أو خلال ٧ أيام بعده» — the store's own definition
#: of «تجديد مستمر», in days, published on the page the customer read.
#:
#: Deliberately NOT the lifecycle's own clock, which is more generous: a
#: subscription enters GRACE for 48 hours, expires, and is still sent a
#: recovery nudge seven days after THAT (lifecycle.RECOVERY_DAYS_AFTER), so
#: nine days after the period ends we are still inviting them back. They are
#: welcome back on either side of this line; what they lose after seven days
#: is the locked price, which is the only thing the sentence promised.
LOCK_GRACE_DAYS = 7

#: Pass plans only — «التقييم ما يحسب كرسي».
LOCKABLE_PLANS = RENEWABLE_PLANS


@dataclass(frozen=True)
class AdmitResult:
    """Why an off-price order was accepted, for the audit trail and the log.
    ``admitted`` is the only field a caller may branch on; the rest exists so
    the reason a financial guard stood aside is written down somewhere."""

    admitted: bool
    reason: str
    lock_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None


def _tenant_for_order_phone(
    session: Session, order_phone: str | None
) -> uuid.UUID | None:
    """The tenant behind a paid order's phone, or None.

    The same two-step identity the renewal path uses and for the same reason:
    an activated customer is found by their WhatsApp channel (oldest binding
    wins — it is the one proven by token), and a buyer who paid but never
    activated is found by the order phone on their unclaimed subscription.
    Kept here rather than imported from `renewal.find_renewal` because that
    function answers a bigger question (is this a renewal, of what, from when)
    and this one must be answerable BEFORE the amount is trusted.
    """
    from career.db.models import CustomerChannel
    from career.salla import subscriptions as sub_states

    variants = phone_variants(order_phone)
    if not variants:
        return None
    channel = session.execute(
        select(CustomerChannel.tenant_id).where(
            CustomerChannel.phone_e164.in_(variants),
            CustomerChannel.provider == "whatsapp",
        ).order_by(CustomerChannel.created_at, CustomerChannel.id)
    ).scalars().first()
    if channel is not None:
        return channel
    return session.execute(
        select(Subscription.tenant_id).where(
            Subscription.order_phone_e164.in_(variants),
            Subscription.status == sub_states.PAID_UNCLAIMED,
            Subscription.plan_code.in_(sorted(LOCKABLE_PLANS)),
        ).order_by(Subscription.created_at, Subscription.id)
    ).scalars().first()


def active_lock(
    session: Session, *, tenant_id: uuid.UUID, plan_code: str
) -> PriceLock | None:
    return session.execute(
        select(PriceLock).where(
            PriceLock.tenant_id == tenant_id,
            PriceLock.plan_code == plan_code,
            PriceLock.lapsed_at.is_(None),
        )
    ).scalars().first()


def capture(
    session: Session, *, tenant_id: uuid.UUID, plan_code: str,
    amount: Decimal | None, currency: str | None,
    subscription_id: uuid.UUID, now: datetime,
) -> PriceLock | None:
    """Record what this customer bought at, the first time they buy it.

    Called from provisioning after the triple match has already agreed that
    the amount is today's real price, which is the whole reason this can be
    trusted later: the lock is never captured from an amount the guard has
    not already verified.

    Idempotent by (tenant, plan): a renewal at the locked price does not
    re-capture anything, and a customer moving between passes gets a lock per
    pass — the price they were promised is the price of the thing they bought.
    """
    if plan_code not in LOCKABLE_PLANS or amount is None or not currency:
        return None
    existing = active_lock(session, tenant_id=tenant_id, plan_code=plan_code)
    if existing is not None:
        return existing
    lock = PriceLock(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_code=plan_code,
        amount_sar=Decimal(amount),
        currency=currency.upper(),
        locked_at=now,
        source_subscription_id=subscription_id,
    )
    session.add(lock)
    session.flush()
    return lock


def _continuous(
    session: Session, *, tenant_id: uuid.UUID, now: datetime
) -> bool:
    """«ما دام تجديده مستمر» — measured, not assumed.

    Continuity is asked of the customer's most recent pass period, whatever
    state its row is in: a lapsed customer's row is EXPIRED and its
    ``current_period_end`` is still the honest end of the last thing they
    paid for. A row with no period at all has not started, so nothing has
    lapsed — a second purchase before activation is the most continuous thing
    there is.
    """
    periods = [
        sub.current_period_end
        for sub in tenant_subscriptions(session, tenant_id)
        if sub.plan_code in LOCKABLE_PLANS and sub.current_period_end is not None
    ]
    if not periods:
        return True
    return now <= max(periods) + timedelta(days=LOCK_GRACE_DAYS)


def admit_locked_amount(
    session: Session, *, order_phone: str | None, plan_code: str,
    amount: Decimal, currency: str | None,
    expected_amount: Decimal, expected_currency: str, now: datetime,
) -> AdmitResult:
    """May this off-price paid order be provisioned at the customer's lock?

    Every refusal returns a NAMED reason rather than a bare False, because
    the one thing worse than refusing a founder's renewal is refusing it with
    an alert that says «سعر المتجر لا يطابق التسعيرة» when the real answer is
    «his lock lapsed four days ago».

    Called only after the standard triple match has already failed, so the
    default answer is the refusal that was going to happen anyway; this can
    only ever turn one specific refusal into an acceptance.
    """
    if plan_code not in LOCKABLE_PLANS:
        return AdmitResult(False, "plan_not_lockable")
    if (currency or "").upper() != expected_currency.upper():
        # The lock is about a price rise, never about a change of currency:
        # accepting another currency would mean comparing numbers that are
        # not comparable, which is how «199» quietly becomes 199 of something
        # else.
        return AdmitResult(False, "currency_mismatch")
    tenant_id = _tenant_for_order_phone(session, order_phone)
    if tenant_id is None:
        return AdmitResult(False, "no_tenant")
    lock = active_lock(session, tenant_id=tenant_id, plan_code=plan_code)
    if lock is None:
        return AdmitResult(False, "no_lock", tenant_id=tenant_id)
    if Decimal(amount) != Decimal(lock.amount_sar):
        # NOT «at most the locked amount». An arbitrary lower number is a
        # tampered order, and the guard exists for exactly that.
        return AdmitResult(False, "amount_not_locked", lock_id=lock.id,
                           tenant_id=tenant_id)
    if (lock.currency or "").upper() != expected_currency.upper():
        return AdmitResult(False, "lock_currency_stale", lock_id=lock.id,
                           tenant_id=tenant_id)
    if Decimal(lock.amount_sar) > Decimal(expected_amount):
        # The store got CHEAPER. A lock may only ever protect against a rise;
        # honouring it downward would take more money than the page asks for
        # today, and no sentence anywhere promises that.
        return AdmitResult(False, "lock_above_current", lock_id=lock.id,
                           tenant_id=tenant_id)
    if not _continuous(session, tenant_id=tenant_id, now=now):
        # «انقطعت أكثر؟ الكرسي ينفتح لغيرك، وترجع بسعر يومها». Stamped, so
        # the record says when it died and nobody has to recompute it later.
        lock.lapsed_at = now
        session.flush()
        return AdmitResult(False, "lapsed", lock_id=lock.id,
                           tenant_id=tenant_id)
    logger.info("founder price lock honoured for a renewal — plan %s",
                plan_code)
    return AdmitResult(True, "locked_price", lock_id=lock.id,
                       tenant_id=tenant_id)


#: What the operator is told when a lock is honoured. He is told at all
#: because a renewal that provisions at less than the store's current price is
#: exactly the shape of a mispriced order, and the one thing that must never
#: happen is that he investigates it as a fault — or, worse, that it goes past
#: him unseen and the difference is discovered in an accounting month.
LOCK_HONOURED_ADMIN_AR = (
    "🔒 جدّد مؤسس بسعره المقفول — زوّدناه عادي\n"
    "{code}\n"
    "المبلغ المدفوع:\n{paid}\n"
    "وسعر المتجر اليوم:\n{today}\n"
    "هذا هو وعد قفل السعر وليس خطأ في التسعيرة"
)
