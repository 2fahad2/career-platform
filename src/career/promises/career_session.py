"""The career session — «جلسة مسار واحدة متى طلبتها».

لمّاح+ (449 SAR) sells three things, and this is the one with a human in it:
«**جلسة مسار** واحدة متى طلبتها: وين موقعك، ووين تقدر توصل، وش ينقصك
بالضبط». The same page promises the tier's answering speed — «والرد خلال ٢٤
ساعة» — and the refund policy prices the session at a number that only exists
because somebody's time was actually spent: «تُخصم قيمة الخدمات البشرية اللي
استلمتها فعليًا (جلسة المسار ١٥٠ ريالًا)».

Before this module `grep 'جلسة\\|career_session\\|session_request'` returned
zero. Not «an incomplete implementation» — nothing at all: no way to record
that a customer asked, no way to see that they were still waiting, and no way
to know afterwards whether a session had been delivered. That last gap is the
expensive one, because the refund page deducts 150 riyals for a session the
customer «actually received» and there was no record anywhere of who had
received one. A refund computed from memory is a refund computed wrong.

**The session itself is human and stays human.** Nothing here schedules
anything, sends an invite, or picks a time; the arrangement happens in the
same WhatsApp conversation the tier is sold on. What this builds is the
ledger and the visibility: who asked, how long ago, whether it was scheduled,
whether it happened — and an escalation for a request that has been sitting,
because the failure mode of a human promise is not refusal, it is silence.

One session per subscription PERIOD, per «واحدة». The period is the
subscription row: since §16 every renewal creates its own row per order, one
open-or-completed session per `subscription_id` is exactly «one per period»
with no date arithmetic to get wrong.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import (
    CareerSession,
    CustomerChannel,
    Subscription,
    SupportEvent,
    Tenant,
)
from career.salla.renewal import current_subscription

logger = logging.getLogger("career.promises")

#: Only لمّاح+ carries the session. «basic» and «professional» never sold it,
#: and the analysis product is not a subscription at all.
SESSION_PLANS: frozenset[str] = frozenset({"executive"})

#: «والرد خلال ٢٤ ساعة» — the tier's own promise, and therefore the age at
#: which an unanswered request stops being a queue and becomes a broken word.
RESPONSE_SLA_HOURS = 24

#: «تُخصم قيمة الخدمات البشرية اللي استلمتها فعليًا (جلسة المسار ١٥٠ ريالًا)».
#: Published on the refund page, so it belongs in the record the operator
#: reads while working out what to refund — not in his memory.
SESSION_VALUE_SAR = Decimal("150")

REQUESTED = "REQUESTED"
SCHEDULED = "SCHEDULED"
COMPLETED = "COMPLETED"
CANCELED = "CANCELED"

#: A request that is still owed something. CANCELED and COMPLETED are done
#: with; everything else is a customer waiting.
OPEN_STATES: frozenset[str] = frozenset({REQUESTED, SCHEDULED})

#: `support_events.kind` for a request that outlived the tier's own SLA. It
#: goes into the SAME table the «دعم» escape hatch writes to, so it appears on
#: the tickets screen the operator already reads — a new alert channel nobody
#: has the habit of checking is how a promise rots twice.
OVERDUE_TICKET_KIND = "career_session_overdue"

#: What the operator's own screen shows. Kept here beside the rules they
#: describe rather than in the view, because these sentences ARE the promise.
SESSION_ENTITLED_AR = "📅 جلسة المسار: من حقه، ولا طلبها بعد"
SESSION_NOT_ENTITLED_AR = "📅 جلسة المسار: غير مشمولة في خطته"


@dataclass(frozen=True)
class SessionRow:
    """One session for the operator's screen — TEN code only (§15.13)."""

    code: str
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    status: str
    requested_at: datetime
    scheduled_at: datetime | None
    completed_at: datetime | None
    overdue: bool


def entitled(session: Session, tenant_id: uuid.UUID) -> bool:
    """Does this customer's CURRENT pass carry a career session?

    Asked of the live subscription rather than «any row this tenant ever
    had»: someone who upgraded to لمّاح+ gains it today, and someone who
    renewed back down to لمّاح does not keep it forever because of a period
    that ended.
    """
    subscription = current_subscription(session, tenant_id)
    return subscription is not None and subscription.plan_code in SESSION_PLANS


def _open_or_used(
    session: Session, *, subscription_id: uuid.UUID
) -> CareerSession | None:
    """The one session this period already has, if any. A CANCELED row does
    not count — a cancelled arrangement gives the entitlement back, which is
    what «واحدة متى طلبتها» means when the customer had to call it off."""
    return session.execute(
        select(CareerSession)
        .where(CareerSession.subscription_id == subscription_id,
               CareerSession.status != CANCELED)
        .order_by(CareerSession.requested_at)
    ).scalars().first()


@dataclass(frozen=True)
class RequestResult:
    outcome: str                       # created | already_open | already_used
                                       # | not_entitled | no_subscription
    session: CareerSession | None = None


def request_session(
    session: Session, *, tenant_id: uuid.UUID, now: datetime,
    source: str = "operator",
) -> RequestResult:
    """Record that the customer asked for their career session.

    ``source`` says who put the row here and is deliberately not a boolean:
    today the request reaches us as a sentence typed to a human in the
    WhatsApp conversation the tier is sold on («تكتب لي أنا مباشرة على نفس
    المحادثة»), so the operator records it from the customer's card. The day
    an inbound keyword is wired, it passes ``source="customer"`` and nothing
    else about this ledger changes.

    Every refusal has its own name. «Already asked» and «already had one this
    period» are different answers to the customer, and a caller that cannot
    tell them apart ends up saying «تم» to both.
    """
    subscription = current_subscription(session, tenant_id)
    if subscription is None:
        return RequestResult("no_subscription")
    if subscription.plan_code not in SESSION_PLANS:
        return RequestResult("not_entitled")
    existing = _open_or_used(session, subscription_id=subscription.id)
    if existing is not None:
        return RequestResult(
            "already_used" if existing.status == COMPLETED else "already_open",
            existing,
        )
    row = CareerSession(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        subscription_id=subscription.id,
        status=REQUESTED,
        source=source,
        requested_at=now,
    )
    session.add(row)
    session.flush()
    return RequestResult("created", row)


def _transition(
    session: Session, *, tenant_id: uuid.UUID, to_status: str, now: datetime,
) -> tuple[str, CareerSession | None]:
    subscription = current_subscription(session, tenant_id)
    if subscription is None:
        return "no_subscription", None
    row = _open_or_used(session, subscription_id=subscription.id)
    if row is None:
        return "no_request", None
    if row.status == to_status:
        return "unchanged", row
    if row.status == COMPLETED:
        # A finished session is a fact about time somebody spent, and the
        # refund page prices it. Nothing may quietly walk it backwards.
        return "already_completed", row
    row.status = to_status
    if to_status == SCHEDULED:
        row.scheduled_at = now
    elif to_status == COMPLETED:
        row.completed_at = now
        if row.scheduled_at is None:
            # A session that happened on the spot never passed through
            # «scheduled», and leaving the column NULL would make the SLA
            # report read as «never answered» about the best possible answer.
            row.scheduled_at = now
    return "applied", row


def mark_scheduled(
    session: Session, *, tenant_id: uuid.UUID, now: datetime
) -> tuple[str, CareerSession | None]:
    """«اتفقنا على موعد» — the customer has been answered. This is what stops
    the SLA clock; the session itself has not happened yet."""
    return _transition(session, tenant_id=tenant_id, to_status=SCHEDULED,
                       now=now)


def mark_completed(
    session: Session, *, tenant_id: uuid.UUID, now: datetime
) -> tuple[str, CareerSession | None]:
    """«انتهت الجلسة» — and from this moment the refund page's 150 riyals are
    a real deduction with a row behind them."""
    return _transition(session, tenant_id=tenant_id, to_status=COMPLETED,
                       now=now)


def current_session(
    session: Session, *, tenant_id: uuid.UUID
) -> CareerSession | None:
    """This period's session, whatever state it is in — the card's answer to
    «وش صار في جلسته؟»."""
    subscription = current_subscription(session, tenant_id)
    if subscription is None:
        return None
    return _open_or_used(session, subscription_id=subscription.id)


def open_sessions(session: Session, *, now: datetime) -> list[SessionRow]:
    """Every session still owed something, oldest request first."""
    rows = session.execute(
        select(CareerSession, Tenant.code)
        .join(Tenant, Tenant.id == CareerSession.tenant_id)
        .where(CareerSession.status.in_(sorted(OPEN_STATES)))
        .order_by(CareerSession.requested_at)
    ).all()
    return [
        SessionRow(
            code=str(code), tenant_id=row.tenant_id, session_id=row.id,
            status=row.status, requested_at=row.requested_at,
            scheduled_at=row.scheduled_at, completed_at=row.completed_at,
            overdue=_is_overdue(row, now=now),
        )
        for row, code in rows
    ]


def _is_overdue(row: CareerSession, *, now: datetime) -> bool:
    """Unanswered past the tier's own 24 hours. SCHEDULED is never overdue —
    the customer has been answered, which is what the 24 hours promised; the
    session date itself is an arrangement between two humans."""
    return (
        row.status == REQUESTED
        and now >= row.requested_at + timedelta(hours=RESPONSE_SLA_HOURS)
    )


def escalate_overdue(
    session: Session, *, now: datetime, admin_client: Any = None,
) -> dict[str, int]:
    """Raise a ticket for every request that outlived «الرد خلال ٢٤ ساعة».

    A request nobody answers does not fail loudly — it just sits, and the
    customer decides on their own that the human half of what they paid 449
    riyals for is not real. So it escalates into `support_events`, where it
    joins the queue the operator already works, and stamps ``escalated_at``
    so the ticket is raised once and not once per night.

    The nightly cadence is the honest limit of this: the sweep runs at 04:30
    Riyadh, so a request that crosses 24 hours at noon is escalated the
    following night. The ledger's own age is exact and the console reads it
    live, so the delay is in the ALERT, never in the record.
    """
    counts = {"escalated": 0}
    rows = session.execute(
        select(CareerSession).where(CareerSession.status == REQUESTED,
                                    CareerSession.escalated_at.is_(None))
    ).scalars().all()
    for row in rows:
        if not _is_overdue(row, now=now):
            continue
        channel = session.execute(
            select(CustomerChannel)
            .where(CustomerChannel.tenant_id == row.tenant_id)
            .order_by(CustomerChannel.created_at)
        ).scalars().first()
        if channel is None:
            # support_events cannot exist without a channel, and a لمّاح+
            # customer without one cannot have asked. Say so in the journal
            # rather than crashing the whole sweep on one impossible row.
            logger.error("overdue career session with no channel — not "
                         "escalated")
            continue
        session.add(SupportEvent(
            id=uuid.uuid4(), tenant_id=row.tenant_id, channel_id=channel.id,
            kind=OVERDUE_TICKET_KIND, status="open",
        ))
        row.escalated_at = now
        counts["escalated"] += 1
        code = session.execute(
            select(Tenant.code).where(Tenant.id == row.tenant_id)
        ).scalars().first() or "TEN-????"
        if admin_client is not None:
            try:
                admin_client.send_admin(
                    "📅 طلب جلسة مسار تجاوز مهلة الرد\n"
                    f"{code}\n"
                    "وعدنا مشتركي لمّاح+ بالرد خلال أربع وعشرين ساعة"
                )
            except Exception:  # noqa: BLE001 — the ticket is the durable half
                logger.warning("career session alert failed", exc_info=True)
    session.flush()
    return counts


def completed_sessions_count(session: Session, *, tenant_id: uuid.UUID) -> int:
    """How many sessions this customer has actually received — the number the
    refund page's «تُخصم قيمة الخدمات البشرية اللي استلمتها فعليًا» is
    computed from."""
    rows = session.execute(
        select(CareerSession.id)
        .where(CareerSession.tenant_id == tenant_id,
               CareerSession.status == COMPLETED)
    ).all()
    return len(rows)


def refund_deduction_sar(session: Session, *, tenant_id: uuid.UUID) -> Decimal:
    """The published deduction, computed rather than remembered.

    The refund page promises «ونعرض عليك الأرقام قبل موافقتك», and the numbers
    have to come from somewhere: this is that somewhere.
    """
    return SESSION_VALUE_SAR * completed_sessions_count(
        session, tenant_id=tenant_id
    )


def plan_of(session: Session, tenant_id: uuid.UUID) -> str | None:
    """The live plan code, for callers that only need the label."""
    subscription: Subscription | None = current_subscription(session, tenant_id)
    return subscription.plan_code if subscription is not None else None
