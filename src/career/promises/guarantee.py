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

And the measurement is **hourly**, which it was not until 2026-08-07. The
sweep's only caller was the nightly delivery run, which fires at 11:00 Riyadh,
so a guarantee that ran out at 12:05 was noticed at 11:00 the NEXT morning:
twenty-three hours of lateness on the one promise the store leads with. A
clock written in hours cannot be read once a day. :func:`sweep_and_commit` is
the entry point every scheduled caller uses, and the reasoning for the hour —
against the minute, against the deadline itself — is written out there.

What this module still does NOT do is tell the customer. The words exist
(:func:`breach_notice_ar`), the extension exists, and nothing sends anything:
the person whose promise we broke learns it only if he asks. That is a product
decision Fahad has not made — see :func:`breach_notice_ar` for the exact
question — and until he does, the operator's breach alert carries the text and
says out loud that nobody has been told, because a gap nobody can see is how
this one survived being written down twice.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
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

#: How often a scheduled caller runs the sweep — and therefore the worst-case
#: lateness of the entire measurement, since nothing else looks at the clock.
#: One hour is 1.4% of the promise; the nightly-only cadence it replaced was
#: 32%. :func:`sweep_and_commit` argues the number.
SWEEP_INTERVAL_SECONDS = 3600.0

#: One pass at a time, across PROCESSES. Two callers now exist (the hourly
#: worker and the nightly backstop) and they can land in the same second; the
#: pass — not the row — is the unit of work here, so a whole-sweep advisory
#: lock is the honest claim rather than the console's per-row ``FOR UPDATE …
#: SKIP LOCKED`` (that pattern fits a QUEUE, where two workers taking
#: different rows is progress; here the second pass would only redo the first).
#: Transaction-scoped, so it is released by the caller's commit or rollback
#: and cannot outlive a crash. The number is arbitrary and unique in this
#: repository — grep it before reusing it in another advisory lock.
_SWEEP_LOCK_KEY = 72_000_072

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
#: SENDS THIS — the only caller in the tree is the operator's own breach alert,
#: which prints it for him to copy. It is a named constant so the owner can
#: change the words without going near the detection, and so the wording is
#: reviewable in a diff instead of being typed differently every time.
GUARANTEE_BREACH_CUSTOMER_AR = (
    "وعدناك بأول فرصة خلال ٧٢ ساعة من تفعيل اشتراكك، وما وفينا 🙏\n"
    "الوعد وعدنا والخيار خيارك: نرجّع لك المبلغ كاملًا، أو نمدد لك المدة "
    "بعدد الأيام اللي راحت عليك\n"
    "قل لنا وش تختار وننفذه على طول"
)


def breach_notice_ar() -> str:
    """The customer-facing sentence for a broken guarantee. NOT SENT BY US.

    A function rather than a bare constant so the operator's own decision —
    whether to send it at all, and when — has one place to be wired to, and so
    the day the wording gains a name or an amount it does not have to change
    shape at every call site.

    ── THE GAP, and the decision that closes it ─────────────────────────────
    Until 2026-08-07 this had ZERO callers anywhere, tests included: we
    detected the broken promise, we told the operator, we could extend the
    subscription — and the customer was never told a thing. The store page
    answers that with «راسلنا على واتساب», which makes our keeping of the
    guarantee conditional on the customer NOTICING it broke; the customer who
    does not notice keeps a broken promise, silently, in our favour. That is
    the exact failure this whole module was written against, one layer up.

    It now has one caller — :func:`_breach_alert_ar`, which prints this text
    on the OPERATOR's screen so he can send it by hand. That is a stopgap and
    is written down as one. It does not send, does not record that anybody was
    told, and cannot be audited afterwards.

    Wiring a real send is a PRODUCT decision, and it is five questions, not
    one. None of them are the code's to answer:

    1. **Trigger** — automatic on detection, or a «أبلِغ العميل» button on the
       breach card? Automatic tells everyone, including the customer whose
       breach the operator was about to explain in person.
    2. **Channel** — this text is free-form, so it only leaves the building
       while the customer's 24h WhatsApp window is OPEN. A shut window means
       either a new Meta template (submitted, approved, and worded to Meta's
       rules rather than ours) or waiting — and «we told him» quietly becomes
       «we will tell him if he writes to us first», which is the store page's
       promise again with extra steps.
    3. **Content** — does it name the number of days (:func:`lost_days`
       computes them) and does it offer the refund unconditionally? The page
       says «أنت تختار», so the menu is the promise; but a customer whose
       account is already REFUNDED or charged back must not be offered a
       refund a second time (:func:`_window_facts` already flags that case).
    4. **The reply** — «استرداد» or «تمديد» comes back as free text into a
       worker with no branch for it. An automatic notice manufactures inbound
       nobody routes, on the day the customer is least patient with us.
    5. **The silenced customer** — someone who sent «إيقاف» is opted out of
       our messages. Do we still owe him this one? (A broken promise about
       money is not marketing, but that call is Fahad's, not the code's.)

    Until those are answered, the honest state is: detected, remedied on the
    operator's tap, and NOT communicated — said in those words on the alert.
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
        # No ready text here on purpose: the notice below offers «استرداد كامل»
        # and this account's money has already moved. A copy-paste block under
        # a «راجع حالته» line is an invitation to refund the same order twice.
        lines.append("ولا تُرسل له عرض التعويض قبل ما تتأكد وش صار في حسابه")
    else:
        lines.append("الخيار للعميل: استرداد كامل أو تمديد المدة — وأنت تنفذه")
        # THE GAP, on the screen, every single time — see `breach_notice_ar`.
        # Nothing in this system tells the customer his promise broke, so the
        # one place that knows says so out loud and hands over the words. A
        # missing feature that is invisible on the operator's screen is a
        # missing feature nobody schedules; this one survived being written
        # into DEVIATIONS and STORE-PAGES twice without acquiring a caller.
        lines.append("⚠️ والعميل ما أُبلغ — ما في إرسال تلقائي، ولا يدري")
        lines.append("انسخ هذا النص وأرسله له بنفسك:")
        lines.append(breach_notice_ar())
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

    Called HOURLY through :func:`sweep_and_commit` — from the conversation
    worker's housekeeping block, and from the nightly orchestrator as a
    backstop, BEFORE its weekend early return, so a guarantee whose
    seventy-two hours run out on a Friday is still caught on Friday: the
    promise is in hours and does not observe our delivery week.

    Idempotent by state: a guarantee that is MET or SETTLED is never looked at
    again, a BREACHED one alerts once (``alerted_at``) and its facts are
    frozen at the breach. Returns honest counters for the caller's summary.
    Two callers cannot double-apply anything — see :func:`sweep_and_commit`,
    which also explains the advisory lock this pass opens with.

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
    if not session.execute(
        select(func.pg_try_advisory_xact_lock(_SWEEP_LOCK_KEY))
    ).scalar_one():
        # Another process is mid-pass. Standing down loses nothing: the pass
        # that holds the lock is looking at exactly the same rows, and this
        # caller comes back within SWEEP_INTERVAL_SECONDS. It is logged rather
        # than counted because the counters describe WORK, and none was done.
        logger.info("guarantee sweep already in flight — this pass stands down")
        return counts
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
            # Still inside the window: counted, and NOTHING IS WRITTEN.
            #
            # What stood here was `if row.status != WATCHING: row.status =
            # WATCHING`, carrying the reason «assigning it back would send an
            # UPDATE per tenant per pass, which was free once a night and is
            # not free at hourly cadence». That reason was not a fact about
            # this system. SQLAlchemy 2.0 compares the assigned value against
            # the loaded one when it builds the UPDATE, so re-assigning an
            # equal value emits no statement at all — `session.dirty` says the
            # row is dirty and the flush writes nothing. There was no cost to
            # avoid, at any cadence. (Pinned in tests/test_promises.py, driven
            # through this pass rather than described, so the claim cannot
            # rot into a different SQLAlchemy's behaviour unnoticed.)
            #
            # The line is gone rather than re-justified, because the only
            # status that can REACH it is WATCHING — MET and SETTLED returned
            # above — so for every reachable row it was a no-op with a story
            # attached. The one row it could ever have touched is a BREACHED
            # one seen with `now` behind its deadline, i.e. a wall clock that
            # stepped backwards, and there the assignment did something this
            # module promises never happens: un-breach a guarantee. It would
            # have dropped the row off `open_breaches` — the operator's queue
            # of customers owed a decision about money — until the clock
            # caught up. A decided guarantee stays decided.
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


def sweep_and_commit(
    session: Session, *, now: datetime, admin_client: Any = None,
) -> dict[str, int]:
    """The entry point every SCHEDULED caller uses: sweep, commit, never raise.

    ── WHY AN HOUR ─────────────────────────────────────────────────────────
    Until 2026-08-07 the sweep had exactly one caller, ``cv.daily_run
    .sweep_promises``, which rides the delivery day, which rides
    ``career-engine-nightly.timer`` — 11:00 Asia/Riyadh. So a guarantee that
    ran out at 12:05 was measured at 11:00 the next morning: up to ~23 hours
    late on a 72-hour promise, on the sentence the store leads with. The
    detector was correct and the clock reading it was a day coarse.

    An hour is the right grain, and every finer one was considered:

    * **Every minute** is not obviously better and is measurably worse. What
      sits on the other end of this alert is a human deciding whether to move
      money, usually after a conversation with the customer; nothing about
      that decision changes between 12:05 and 13:00. A minute-grained sweep
      would buy 59 minutes of paper precision and pay a full pass over every
      activated customer sixty times an hour for it, forever, so that an
      operator who is asleep can be paged 59 minutes earlier.
    * **At the deadline itself** — a job per guarantee, fired at its own
      deadline — is the version that sounds exact and is the most fragile. It
      needs a scheduler that survives restarts, and a schedule that is LOST
      (a process that died between arming and firing, a row written while the
      scheduler was down) is a promise that is never measured at all, with
      nothing to notice the silence. A poll is self-healing by construction:
      the next pass finds whatever the previous one missed, whatever happened
      in between. That property is worth far more here than the 59 minutes.
    * **Four-hourly** would still be four times the operator's own reaction
      granularity and would leave a breach detected at 04:00 for a deadline
      at 00:05 — and it saves nothing real, because the process that runs
      this is already awake and already sweeping on the hour.

    ── WHERE IT RUNS, and why not a timer of its own ────────────────────────
    In ``scripts/run_worker_loop.py``'s hourly housekeeping block, beside the
    enrichment sweep, the outcome questions, the weekly report and the
    forgotten-ticket release. That process is up permanently under a systemd
    watchdog, it already holds the operator's Telegram client and a session
    factory, and it is — as ``sweep_forgotten_tickets`` puts it about exactly
    this shape of problem — «the one that is awake at 03:00».

    A dedicated ``career-promises.timer`` was the alternative and was
    rejected. It buys one real thing, ``OnFailure=career-alert@`` on a failed
    pass, and costs three: a unit that has to be installed by hand before the
    measurement improves at all (and is silently absent until then, since
    ``scripts/verify_restore.sh`` §6 checks a fixed list of units it is not
    on), a second construction of the settings/engine/Telegram wiring that
    this repository deliberately keeps in ``scripts/`` entry points, and a
    second answer in one codebase to a question already answered four times
    in that block. The failure signal is not lost either: a raised sweep logs
    ERROR, and the journal harvester puts ERROR lines on the watchtower's
    error screen.

    ── WHY THE NIGHTLY STILL CALLS IT ───────────────────────────────────────
    Kept on purpose, as a backstop for the one hour the worker is not there
    (a wedged cycle, a deploy, a host that came back without it), and because
    a worker outage is loud while a nightly one is louder still. It cannot
    double-apply anything, and that is a property of the row rather than of
    the schedule:

    * MET and SETTLED short-circuit before anything is read;
    * ``breached_at`` is stamped only on the WATCHING→BREACHED edge, so a
      second pass over a BREACHED row counts nothing and rewrites nothing;
    * ``facts`` are written only while empty — the packet describes a window
      that is over and must not drift;
    * ``alerted_at`` gates the page, so the operator is told once, ever;
    * and the remedy is not in this path at all. It is applied by the
      operator's own tap through :func:`apply_remedy`, which refuses any row
      that is not BREACHED.

    What those guards do NOT cover is two passes in flight AT THE SAME
    INSTANT: both could read the same WATCHING row before either committed
    and both would page it. That was impossible with one caller and became
    possible with two, so the pass takes ``pg_try_advisory_xact_lock`` and
    the loser stands down — the same lesson `release_forgotten_tickets`
    learned the day it grew its second caller, in the shape that fits here.

    ── THE CONTRACT ─────────────────────────────────────────────────────────
    Commits its own work and NEVER raises. The hourly block is a barrier: an
    escape from it costs that cycle its watchdog pet and, in the nightly, a
    paying customer's delivery day. A failed pass rolls back — which is what
    makes the alert-before-``alerted_at`` ordering inside the sweep safe: the
    page may be duplicated by a crash, never lost.
    """
    zero = {"watching": 0, "met": 0, "breached": 0, "alerted": 0}
    try:
        counts = sweep_delivery_guarantee(
            session, now=now, admin_client=admin_client
        )
        session.commit()
        return counts
    except Exception:  # noqa: BLE001 — a sweep never wedges its caller
        logger.error("guarantee sweep failed — no breach was recorded or "
                     "paged in this pass", exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — a dead session must not loop either
            logger.error("guarantee sweep rollback failed", exc_info=True)
        return zero


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
