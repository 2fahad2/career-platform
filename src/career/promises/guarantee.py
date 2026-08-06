"""The 72-hour start guarantee — «ضمان البداية».

The refund policy page sells it in one sentence: «ما وصلتك أول فرصة خلال ٧٢
ساعة من تفعيل اشتراكك؟ استرداد كامل أو تمديد الاشتراك — أنت تختار.» The
guarantee line pasted under every subscription says the same thing with the
filter spelled out: «أول فرصة توصلك خلال ٧٢ ساعة من التفعيل — فرصة مرت من
فلتر شروطك أنت … ما وصلت؟ راسلنا على واتساب — ترجع لك فلوسك كاملة أو نمدد لك
المدة. أنت اللي تختار.»

Until this module nothing measured it. `grep 72` found an enrichment TTL and
nothing else: no clock, no state on any record, and — the part that actually
costs money — no signal to the operator when it broke. The customer was the
only alarm the promise had, and a customer who does not complain simply keeps
a broken guarantee, which is the worst possible way for it to fail: silently,
in our favour.

Three decisions are worth stating plainly, because each one is a place where
a different choice would have made the record a lie.

The anchor is **activation, not payment**. `onboarding_sessions.completed_at`
is stamped by `policy.activate` at the same instant the subscription's period
starts, and it is the only timestamp that answers «متى فعّل؟» exactly once per
customer. The subscription row cannot be the anchor: a renewal creates a NEW
row with a NEW `current_period_start` (§16), so keying on it would restart a
START guarantee at every renewal and manufacture a breach for a customer who
has been served happily for months.

Detection is mechanical; **the remedy is not**. The refund or the extension
moves money and the store page is explicit that the CUSTOMER chooses which —
«أنت تختار» — so nothing here refunds, extends, or messages anybody on its
own. What this module does is notice, record, hand the operator the facts he
needs to judge, and then apply whichever remedy he picks. The facts matter as
much as the breach: a window where the gate honestly found nothing («لا فرص
مطابقة») is a different conversation from three days of WhatsApp failures, and
the operator cannot tell those apart from a red line saying «انكسر الضمان».

A breach is **never un-breached by a late delivery**. A first opportunity that
lands on day four is recorded — the operator needs to know service eventually
started — but the status stays BREACHED, because the promise was about the
first seventy-two hours and those are over.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import (
    CustomerChannel,
    DeliveryGuarantee,
    OnboardingSession,
    Subscription,
    SubscriptionEvent,
    Tenant,
    TenantDayState,
)
from career.salla import subscriptions as sub_states
from career.salla.renewal import RENEWABLE_PLANS, current_subscription

logger = logging.getLogger("career.promises")

#: «خلال ٧٢ ساعة من تفعيل اشتراكك» — the store's own number, in the store's
#: own unit. Not «three days»: a day boundary would round the promise in our
#: favour for anyone who activates in the evening.
GUARANTEE_HOURS = 72

#: The clock runs while we are watching, and stops the first time we can
#: answer the customer's question honestly.
WATCHING = "WATCHING"
MET = "MET"
BREACHED = "BREACHED"
#: The operator has applied the remedy the customer chose. Terminal.
SETTLED = "SETTLED"

#: The two remedies the page offers, plus the one it does not: a customer who
#: is told «انكسر الضمان» and answers «خلاص، كمّلوا» has chosen neither, and
#: the operator still has to be able to close the record without pretending
#: money moved or days were added.
REMEDY_REFUND = "refund"
REMEDY_EXTENSION = "extension"
REMEDY_WAIVED = "waived"
REMEDIES = frozenset({REMEDY_REFUND, REMEDY_EXTENSION, REMEDY_WAIVED})

#: The states in which a tenant day means the customer RECEIVED something.
#: PARTIAL_DELIVERY counts only with a non-zero delivered count — a partial
#: that delivered nothing is a failure wearing an amber word (§15.12), and
#: counting it would let the guarantee be «met» by a day nobody was served.
_RECEIVED_STATES = ("DELIVERED", "PARTIAL_DELIVERY")

#: Subscriptions the guarantee is sold under. The line sits «تحت كل اشتراك» —
#: under each SUBSCRIPTION — and «تقييم لمّاح» is a one-shot report with its
#: own separate refund clause (§٥ of the refund page), no period and no daily
#: search, so it can neither breach this nor be remedied by extending it.
GUARANTEED_PLANS = RENEWABLE_PLANS

#: What the customer reads when the operator decides to tell them. NOTHING
#: sends this — see the module docstring. It is a named constant so the owner
#: can change the words without going near the detection, and so the wording
#: is reviewable in a diff instead of being typed differently every time.
GUARANTEE_BREACH_CUSTOMER_AR = (
    "وعدناك بأول فرصة خلال ٧٢ ساعة من تفعيل اشتراكك، وما وفينا 🙏\n"
    "الوعد وعدنا والخيار خيارك: نرجّع لك المبلغ كاملًا، أو نمدد لك المدة "
    "بعدد الأيام اللي راحت عليك\n"
    "قل لنا وش تختار وننفذه على طول"
)


def breach_notice_ar() -> str:
    """The customer-facing sentence for a broken guarantee.

    A function rather than a bare constant so the operator's own decision —
    whether to send it at all, and when — has one place to be wired to, and so
    the day the wording gains a name or an amount it does not have to change
    shape at every call site.
    """
    return GUARANTEE_BREACH_CUSTOMER_AR


@dataclass(frozen=True)
class BreachRow:
    """One breached guarantee, ready for the operator's screen. TEN code only
    (§15.13) — nothing in here can identify a human."""

    code: str
    tenant_id: uuid.UUID
    activated_at: datetime
    deadline_at: datetime
    first_delivery_at: datetime | None
    facts: dict[str, Any]
    remedy: str | None


def _first_delivery_at(
    session: Session, *, tenant_id: uuid.UUID, since: datetime
) -> datetime | None:
    """When did the FIRST opportunity actually reach this customer?

    Read from the honest day state (§15.12) rather than from `deliveries`,
    because the day state is the row that already knows the difference between
    «سُلّم» and «تسليم جزئي لم يصل منه شيء» — and it is written by exactly one
    authority, so there is no second definition of «وصلت» to drift from.
    """
    rows = session.execute(
        select(TenantDayState)
        .where(TenantDayState.tenant_id == tenant_id,
               TenantDayState.state.in_(_RECEIVED_STATES),
               TenantDayState.recorded_at >= since)
        .order_by(TenantDayState.recorded_at)
    ).scalars().all()
    for row in rows:
        if int((row.counts or {}).get("delivered", 0)) > 0:
            return row.recorded_at
    return None


def _window_facts(
    session: Session, *, tenant_id: uuid.UUID, activated_at: datetime,
    deadline_at: datetime,
) -> dict[str, Any]:
    """The PII-free packet the operator judges with.

    A breach line on its own tells him a promise broke and nothing about
    whose fault it was. These four facts are the difference between «the
    market had nothing that matched his filter» — which the product says out
    loud and which he may well answer by extending — and «our WhatsApp was
    failing for three days», which he answers by refunding before being
    asked.
    """
    rows = session.execute(
        select(TenantDayState.state)
        .where(TenantDayState.tenant_id == tenant_id,
               TenantDayState.recorded_at >= activated_at,
               TenantDayState.recorded_at <= deadline_at)
    ).scalars().all()
    day_states: dict[str, int] = {}
    for state in rows:
        day_states[str(state)] = day_states.get(str(state), 0) + 1

    channel = session.execute(
        select(CustomerChannel.opt_out_at)
        .where(CustomerChannel.tenant_id == tenant_id)
    ).first()
    subscription = current_subscription(session, tenant_id)
    # Riyadh weekdays 4 and 5 are Friday and Saturday: §08 delivers Sunday to
    # Thursday, so a guarantee window can legitimately contain days on which
    # nothing was ever going to be sent. That is OUR scheduling choice against
    # a promise we wrote in flat hours, so it is a fact the operator gets, not
    # an excuse the code takes for itself.
    weekend = 0
    day = activated_at
    while day < deadline_at:
        if day.weekday() in (4, 5):
            weekend += 1
        day += timedelta(days=1)
    return {
        "day_states": day_states,
        "opted_out": bool(channel and channel[0] is not None),
        "paused": bool(subscription is not None
                       and subscription.status == sub_states.PAUSED),
        "weekend_days": weekend,
    }


def _guarantee_row(
    session: Session, *, tenant_id: uuid.UUID, subscription_id: uuid.UUID,
    activated_at: datetime,
) -> DeliveryGuarantee:
    row = session.execute(
        select(DeliveryGuarantee)
        .where(DeliveryGuarantee.tenant_id == tenant_id)
    ).scalars().first()
    if row is None:
        row = DeliveryGuarantee(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            activated_at=activated_at,
            deadline_at=activated_at + timedelta(hours=GUARANTEE_HOURS),
            status=WATCHING,
        )
        session.add(row)
        session.flush()
    return row


def _breach_alert_ar(code: str, row: DeliveryGuarantee) -> str:
    """The operator's line. Every line is direction-pure — the TEN code and
    every Latin token stand alone, because a mixed Arabic+Latin run arrives
    reversed on the operator's client."""
    facts = dict(row.facts or {})
    lines = [
        "🛡️ انكسر ضمان الـ٧٢ ساعة",
        code,
        "ما وصلت أول فرصة خلال المهلة من تفعيل اشتراكه",
    ]
    day_states = facts.get("day_states") or {}
    if day_states:
        lines.append("حالات أيامه داخل المهلة:")
        for state, count in sorted(day_states.items()):
            # the state token is Latin — its own line, with the count beside
            # it as a Latin numeral so the whole line is one direction
            lines.append(f"{state} x{count}")
    else:
        lines.append("ولا حالة يوم مسجلة له داخل المهلة — تحقق من التشغيلة")
    if facts.get("weekend_days"):
        lines.append("والمهلة مرت على عطلة نهاية الأسبوع — لا تسليم فيها")
    if facts.get("paused"):
        lines.append("وكان اشتراكه موقوفًا مؤقتًا بطلبه")
    if facts.get("opted_out"):
        lines.append("وكان موقفًا للرسائل")
    lines.append("الخيار للعميل: استرداد كامل أو تمديد المدة — وأنت تنفذه")
    return "\n".join(lines)


def _alert(admin_client: Any, text: str) -> None:
    if admin_client is None:
        return
    try:
        admin_client.send_admin(text)
    except Exception:  # noqa: BLE001 — an unreachable console never blocks
        logger.warning("guarantee alert failed", exc_info=True)


def sweep_delivery_guarantee(
    session: Session, *, now: datetime, admin_client: Any = None,
) -> dict[str, int]:
    """One idempotent pass over every activated customer's start guarantee.

    Runs from the nightly orchestrator BEFORE the weekend early return, so a
    guarantee whose seventy-two hours run out on a Friday is still caught on
    Friday — the promise is in hours and does not observe our delivery week.

    Idempotent by state: a guarantee that is MET or SETTLED is never looked at
    again, and a BREACHED one alerts exactly once (``alerted_at``). Returns
    honest counters for the caller's summary.
    """
    counts = {"watching": 0, "met": 0, "breached": 0, "alerted": 0}
    journeys = session.execute(
        select(OnboardingSession.tenant_id, OnboardingSession.subscription_id,
               OnboardingSession.completed_at)
        .where(OnboardingSession.completed_at.is_not(None))
    ).all()
    for tenant_id, subscription_id, activated_at in journeys:
        subscription = session.get(Subscription, subscription_id)
        if subscription is None or subscription.plan_code not in GUARANTEED_PLANS:
            # A funnel tenant who never bought a pass has no guarantee to
            # break: «تقييم لمّاح» is a report, not a subscription (§٥).
            continue
        row = _guarantee_row(
            session, tenant_id=tenant_id, subscription_id=subscription_id,
            activated_at=activated_at,
        )
        if row.status in (MET, SETTLED):
            continue
        first = _first_delivery_at(
            session, tenant_id=tenant_id, since=row.activated_at
        )
        if first is not None and row.first_delivery_at is None:
            row.first_delivery_at = first
        if first is not None and first <= row.deadline_at:
            row.status = MET
            counts["met"] += 1
            continue
        if now <= row.deadline_at:
            row.status = WATCHING
            counts["watching"] += 1
            continue
        # The deadline is behind us and nothing landed inside it. A delivery
        # that arrived AFTER the deadline is recorded above and changes
        # nothing here: the promise was about the first seventy-two hours.
        if row.status != BREACHED:
            row.status = BREACHED
            row.breached_at = now
            counts["breached"] += 1
        row.facts = _window_facts(
            session, tenant_id=tenant_id, activated_at=row.activated_at,
            deadline_at=row.deadline_at,
        )
        if row.alerted_at is None:
            code = session.execute(
                select(Tenant.code).where(Tenant.id == tenant_id)
            ).scalars().first() or "TEN-????"
            _alert(admin_client, _breach_alert_ar(str(code), row))
            row.alerted_at = now
            counts["alerted"] += 1
    session.flush()
    return counts


def open_breaches(session: Session) -> list[BreachRow]:
    """Every breach still waiting for the owner's decision, oldest first.

    Oldest first for the same reason the ticket screen is: the customer who
    has been owed an answer longest is the one this product may not lose.
    """
    rows = session.execute(
        select(DeliveryGuarantee, Tenant.code)
        .join(Tenant, Tenant.id == DeliveryGuarantee.tenant_id)
        .where(DeliveryGuarantee.status == BREACHED)
        .order_by(DeliveryGuarantee.breached_at)
    ).all()
    return [
        BreachRow(
            code=str(code), tenant_id=row.tenant_id,
            activated_at=row.activated_at, deadline_at=row.deadline_at,
            first_delivery_at=row.first_delivery_at,
            facts=dict(row.facts or {}), remedy=row.remedy,
        )
        for row, code in rows
    ]


def lost_days(row: DeliveryGuarantee, *, now: datetime) -> int:
    """How many days the customer paid for and got nothing.

    Not an invented number: the store already states the principle it comes
    from — «الثلاثين يوم تبدأ من إكمال الإعداد … ما نحسب عليك يوم ما اشتغلنا
    فيه». So the extension is the span from activation to the day service
    actually started (or to today, if it still has not), floored at one,
    because an extension of zero days is not an extension.
    """
    end = row.first_delivery_at or now
    return max(1, (end - row.activated_at).days)


@dataclass(frozen=True)
class RemedyResult:
    """What was actually done — never «done», which is what a console reply
    that reports the CALL rather than the CHANGE ends up saying."""

    outcome: str            # applied | already_settled | unknown | no_period
    days: int = 0
    new_period_end: datetime | None = None


def apply_remedy(
    session: Session, *, tenant_id: uuid.UUID, remedy: str, now: datetime,
) -> RemedyResult:
    """Record the remedy the customer chose, and apply the half we can.

    The refund is Salla's to make and the operator's to authorise: this
    records the choice and touches no money. The store's own refund page says
    the money returns «بنفس وسيلة دفعك عبر سلة», and when he makes it the
    `order.refunded` webhook moves the subscription itself — writing REFUNDED
    from here would race that authority and could stop a service the customer
    is still owed.

    The extension is different: adding days is something this system can do
    correctly and no one else can, so once he picks it, it is applied here —
    through a `subscription_events` row like every other change to a paid
    period, so the money trail keeps its shape.
    """
    if remedy not in REMEDIES:
        return RemedyResult("unknown")
    row = session.execute(
        select(DeliveryGuarantee)
        .where(DeliveryGuarantee.tenant_id == tenant_id)
    ).scalars().first()
    if row is None or row.status != BREACHED:
        return RemedyResult("already_settled" if row is not None else "unknown")

    days = 0
    new_end: datetime | None = None
    if remedy == REMEDY_EXTENSION:
        subscription = current_subscription(session, tenant_id)
        if subscription is None or subscription.current_period_end is None:
            # Nothing to extend, and saying «مددنا» would be a lie the
            # customer discovers on the day their service stops.
            return RemedyResult("no_period")
        days = lost_days(row, now=now)
        new_end = subscription.current_period_end + timedelta(days=days)
        subscription.current_period_end = new_end
        session.add(SubscriptionEvent(
            id=uuid.uuid4(), tenant_id=tenant_id,
            subscription_id=subscription.id,
            event_type="guarantee_extension",
            from_status=subscription.status, to_status=subscription.status,
            salla_order_id=subscription.salla_order_id,
            details={"days": days, "guarantee_id": str(row.id),
                     "period_end": new_end.isoformat()},
        ))
    row.status = SETTLED
    row.remedy = remedy
    row.remedy_at = now
    session.flush()
    return RemedyResult("applied", days=days, new_period_end=new_end)
