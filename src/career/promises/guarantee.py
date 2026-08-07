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
    DeliveryGuarantee,
    InboundMessage,
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


def _paused_inside(
    session: Session, *, tenant_id: uuid.UUID, activated_at: datetime,
    deadline_at: datetime,
) -> bool:
    """Did the customer's own «وقف مؤقت» land inside the window?

    AUDIT 2026-08-06. This used to be ``current_subscription(...).status ==
    PAUSED`` — the status at SWEEP time, which is days or weeks after the
    window it claims to describe, and the alert built on it says «كان اشتراكه
    موقوفًا»: past tense over a present-tense reading. A customer who paused on
    day one and resumed on day five therefore read as ``paused: false``, so
    the single fact that most changes the operator's answer — «he asked us to
    stop delivering, and we are about to refund him for not delivering» — was
    absent from precisely the case it exists for. The other direction is worse
    in a quieter way: a customer who paused a month later read as paused
    inside a window his pause had nothing to do with.

    A pause is a state CHANGE and `subscriptions.transition` records every one
    of them, so the window is read from the events rather than guessed from
    the present. Still deliberately coarse — one pause anywhere in seventy-two
    hours is «كان موقوفًا» — because this is a fact handed to a human, not an
    entitlement calculation.

    Tenant-wide on purpose, though the row carries a ``subscription_id``.
    An upgrade inside the window (§C8 keeps the customer's days) creates a NEW
    subscription row, so a pause that lands after it names an id the guarantee
    was never anchored to; filtering would answer «ما وقف» about a customer
    who did. The looser read can only over-report — «somebody paused this
    tenant's service inside the window» — which is a fact the operator wanted
    anyway, and delivery is per TENANT, so it is the true one.
    """
    return session.execute(
        select(SubscriptionEvent.id).where(
            SubscriptionEvent.tenant_id == tenant_id,
            SubscriptionEvent.to_status == sub_states.PAUSED,
            SubscriptionEvent.created_at >= activated_at,
            SubscriptionEvent.created_at <= deadline_at,
        ).limit(1)
    ).first() is not None


#: `inbound_messages.classification` for the customer's own «إيقاف» and
#: «استئناف» (whatsapp/worker) — the two rows that carry a TIMESTAMP for a
#: switch whose column only ever remembers the latest flip.
_STOP = "stop"
_RESUME = "resume"


def _silence_inside(
    session: Session, *, tenant_id: uuid.UUID, activated_at: datetime,
    deadline_at: datetime,
) -> tuple[bool, bool]:
    """Was the customer silencing us inside the window — and did he walk into
    it already silent? Returns ``(inside, before)``.

    AUDIT 2026-08-06. This used to be ``opt_out_at is not None and opt_out_at
    <= deadline_at``: a column, read at sweep time, with no lower bound — the
    same snapshot mistake :func:`_paused_inside` was rewritten to remove, and
    it lied in both directions.

    * `whatsapp/worker` sets `opt_out_at` on «إيقاف» and puts it back to NULL
      on «استئناف», so it remembers exactly ONE flip. A customer who stopped
      at hour one and resumed at hour seventy-four therefore read as
      ``opted_out: False`` for a window he was silent through — the case most
      likely to explain a breach, missing from the packet that explains it.
    * With no lower bound, a silence from BEFORE the window (a buyer who
      replies, says «إيقاف» during onboarding and activates days later) was
      reported as a fact about the window, and the alert said «داخل المهلة»
      about something nothing inside the window supports.

    So it is read from `inbound_messages`, which timestamps every stop and
    resume, and split into the two different sentences it always was: one
    stop landing inside the window, and the state he entered the window in.
    Both are handed to the operator; neither pretends to be the other.
    """
    rows = session.execute(
        select(InboundMessage.classification, InboundMessage.received_at)
        .where(InboundMessage.tenant_id == tenant_id,
               InboundMessage.classification.in_((_STOP, _RESUME)),
               InboundMessage.received_at <= deadline_at)
        .order_by(InboundMessage.received_at)
    ).all()
    inside = any(
        kind == _STOP and at >= activated_at for kind, at in rows
    )
    earlier = [kind for kind, at in rows if at < activated_at]
    return inside, bool(earlier) and earlier[-1] == _STOP


def _window_facts(
    session: Session, *, tenant_id: uuid.UUID, activated_at: datetime,
    deadline_at: datetime,
) -> dict[str, Any]:
    """The PII-free packet the operator judges with.

    A breach line on its own tells him a promise broke and nothing about
    whose fault it was. These facts are the difference between «the market had
    nothing that matched his filter» — which the product says out loud and
    which he may well answer by extending — and «our WhatsApp was failing for
    three days», which he answers by refunding before being asked.

    Every one of them is a fact about the WINDOW, not about the moment the
    sweep happens to run. That distinction is not pedantry: this packet is
    read weeks later, beside a decision to move money, and a snapshot dressed
    up as history is how an operator ends up refunding a customer who paused
    the service himself — or refusing one because of something he did long
    after the promise had already broken.
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

    silenced_inside, silenced_before = _silence_inside(
        session, tenant_id=tenant_id, activated_at=activated_at,
        deadline_at=deadline_at,
    )
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
        # Two facts, because they are two different sentences to the operator:
        # «he silenced us inside the window» and «he walked into it silent».
        "opted_out": silenced_inside,
        "opted_out_before_window": silenced_before,
        "paused": _paused_inside(
            session, tenant_id=tenant_id, activated_at=activated_at,
            deadline_at=deadline_at,
        ),
        "weekend_days": weekend,
        # The status the money is in AT THE ALERT, and the one fact here that
        # is not a fact about the window: a customer who was already refunded
        # or charged back must not be offered «استرداد كامل» a second time.
        # The breach is still recorded — it really did break — but the
        # operator is told the account is closed before he reads a remedy menu.
        #
        # It is frozen with the rest of the packet, so it is exact for the
        # alert and stale for every later reader; a refund that arrives the
        # next morning would leave this saying ACTIVE for as long as the
        # breach sits open. :func:`open_breaches` therefore re-reads it live
        # and keeps this value as the historical one — the freeze protects
        # the WINDOW facts, and this was never one of them.
        "subscription_status": (
            subscription.status if subscription is not None else None
        ),
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
        lines.append("وكان اشتراكه موقوفًا مؤقتًا بطلبه داخل المهلة")
    if facts.get("opted_out"):
        lines.append("وكان موقفًا للرسائل داخل المهلة")
    elif facts.get("opted_out_before_window"):
        # A different fact and therefore a different sentence: he did not
        # silence us during the window, he arrived already silent.
        lines.append("ودخل المهلة وهو موقف للرسائل من قبلها")
    if facts.get("subscription_status") in sub_states.TERMINAL_STATES:
        # A refunded or charged-back account has already had its money moved,
        # and the remedy menu below would invite the operator to move it
        # again. The breach stays on the record — it broke — but he reads
        # «closed» before he reads «choose a remedy».
        lines.append("لكن اشتراكه منتهٍ ماليًا الآن — راجع حالته قبل أي تعويض")
    else:
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
    again, a BREACHED one alerts once (``alerted_at``) and its facts are
    frozen at the breach. Returns honest counters for the caller's summary.

    One customer, one row, one anchor — and that holds because
    ``onboarding_sessions`` carries ``uq_onboarding_sessions_tenant_id``. The
    journeys query below has no ORDER BY and does not need one: a tenant
    cannot appear twice, so there is no «which activation anchors this
    guarantee» to get wrong. If that constraint ever goes, this loop starts
    silently picking an arbitrary anchor per night, and «ضمان البداية» —
    a customer starts once — stops being true of the row.

    What this sweep does NOT do is decide that a breach is excusable. A
    customer who cancelled at hour twenty still breaches at hour seventy-two:
    the promise was made and not kept, and hiding that would leave the one
    number the operator prices the product from quietly flattering. What the
    facts do instead is tell him the account is already closed (see
    :func:`_window_facts`), so he does not offer a refund to somebody who has
    already had one.
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
        if not row.facts:
            # Written ONCE, and that is the point rather than an optimisation.
            # The facts describe a window that is over, so there is nothing
            # later for them to learn — but there is plenty for them to catch:
            # this ran on every sweep, for every breach nobody had settled
            # yet, so an opt-out or a pause that happened weeks afterwards
            # kept being folded back into the account of a window it had
            # nothing to do with, and the packet the operator judged with
            # drifted every night away from what actually happened.
            # (The empty test also backfills a row breached before this,
            # which had no facts at all.)
            row.facts = _window_facts(
                session, tenant_id=tenant_id, activated_at=row.activated_at,
                deadline_at=row.deadline_at,
            )
        if row.alerted_at is None:
            code = session.execute(
                select(Tenant.code).where(Tenant.id == tenant_id)
            ).scalars().first() or "TEN-????"
            # Sent BEFORE the stamp is durable, and that ordering is chosen,
            # not overlooked. `daily_run.sweep_promises` rolls this session
            # back on any failure, so a sweep that dies after this line
            # re-pages the same breach tomorrow — a duplicate, which is loud,
            # obvious and costs the operator a glance. The other ordering
            # trades that for a breach that was never announced and whose
            # record says it was, and this whole module exists because the
            # customer was the only alarm the promise had. `escalate_overdue`
            # stamps first for the opposite reason: its durable half is a
            # support ticket, so the alert there is the redundant copy.
            _alert(admin_client, _breach_alert_ar(str(code), row))
            row.alerted_at = now
            counts["alerted"] += 1
    session.flush()
    return counts


def open_breaches(session: Session) -> list[BreachRow]:
    """Every breach still waiting for the owner's decision, oldest first.

    Oldest first for the same reason the ticket screen is: the customer who
    has been owed an answer longest is the one this product may not lose.

    The facts are the frozen ones — they describe a window that is over —
    with ONE exception, and it is the one fact that was never about the
    window: `subscription_status`. Frozen, it says what the account was on
    the night of the breach, and this screen is read days later beside a
    button that moves money; a refund that landed yesterday would still read
    ACTIVE here. It is re-read live, and the frozen value is kept beside it
    under its own name so the history is not lost.
    """
    rows = session.execute(
        select(DeliveryGuarantee, Tenant.code)
        .join(Tenant, Tenant.id == DeliveryGuarantee.tenant_id)
        .where(DeliveryGuarantee.status == BREACHED)
        .order_by(DeliveryGuarantee.breached_at)
    ).all()
    out: list[BreachRow] = []
    for row, code in rows:
        facts = dict(row.facts or {})
        if "subscription_status" in facts:
            facts["subscription_status_at_breach"] = facts["subscription_status"]
        subscription = current_subscription(session, row.tenant_id)
        facts["subscription_status"] = (
            subscription.status if subscription is not None else None
        )
        out.append(BreachRow(
            code=str(code), tenant_id=row.tenant_id,
            activated_at=row.activated_at, deadline_at=row.deadline_at,
            first_delivery_at=row.first_delivery_at,
            facts=facts, remedy=row.remedy,
        ))
    return out


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
