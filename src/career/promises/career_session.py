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

AUDIT 2026-08-07 — the tier's OTHER human promise now lives here too, at the
bottom of the file: «تواصل مباشر معي … اكتب لي وقت ما تحتاج». It was sold in
the same paragraph, built to the same shape (a ticket, an operator, no
automation) and was reachable by nobody for the same reason — see
:func:`escalate_direct_message`. Same tier, same human, same ledger; a second
module would only have split one promise across two files.

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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import (
    CareerSession,
    CustomerChannel,
    Subscription,
    SupportEvent,
    Tenant,
)
from career.salla import subscriptions as sub_states
from career.salla.renewal import current_subscription

logger = logging.getLogger("career.promises")

#: Only لمّاح+ carries the session. «basic» and «professional» never sold it,
#: and the analysis product is not a subscription at all.
SESSION_PLANS: frozenset[str] = frozenset({"executive"})

#: «والرد خلال ٢٤ ساعة» — the tier's own promise, and therefore the age at
#: which an unanswered request stops being a queue and becomes a broken word.
RESPONSE_SLA_HOURS = 24

#: How often a scheduled caller runs :func:`escalate_overdue`, and therefore
#: the worst-case lateness of the entire measurement — nothing else in this
#: system looks at the clock on a career-session request. One hour is 4% of
#: the promise; the nightly-only cadence it replaced on 2026-08-07 was 96%,
#: which is a 24-hour promise very nearly measured after it had expired.
#: :func:`escalate_overdue_and_commit` argues the number.
SLA_SWEEP_INTERVAL_SECONDS = 3600.0

#: One pass at a time, across PROCESSES. Two scheduled callers now exist (the
#: hourly worker and the nightly backstop) and they can land in the same
#: second; the PASS — not the row — is the unit of work, so a whole-sweep
#: advisory lock is the honest claim rather than a per-row ``FOR UPDATE …
#: SKIP LOCKED`` (that pattern fits a QUEUE, where two workers taking
#: different rows is progress; here the second pass would only redo the first,
#: never extend it). Transaction-scoped, so the caller's commit or rollback
#: releases it and a crash cannot leave it held.
#:
#: DELIBERATELY NOT `guarantee._SWEEP_LOCK_KEY` (72_000_072), and the two must
#: never be merged into one «promises sweep» lock. They measure different
#: promises against different tables with not one row in common, and both are
#: driven from the SAME two schedules — the worker's hourly block and
#: `cv.daily_run.sweep_promises` — so a shared key would make each pass able
#: to silence the other for a reason that has nothing to do with it: a slow
#: 72-hour sweep in the worker would stand the nightly's 24-hour pass down,
#: and the backstop that exists precisely for the hour the worker is wrong
#: would decline to run BECAUSE the worker was running. Standing down is only
#: free when the pass holding the lock is doing this pass's work; the whole
#: value of the claim is that it means exactly that.
_SLA_SWEEP_LOCK_KEY = 24_000_024

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
    what «واحدة متى طلبتها» means when the customer had to call it off.

    AUDIT 2026-08-06 — that second sentence describes a rule nothing can
    currently reach, and saying so is the point. No function in this module
    writes CANCELED, and `telegram.console` offers request / schedule /
    complete and no fourth button, so the status exists in the catalog (and in
    the partial unique index that depends on it) with no writer. Two things
    follow, and both are load-bearing:

    * a request recorded in error cannot be cleared by any shipped path, which
      is the strongest argument against ever triggering :func:`request_session`
      from an inbound KEYWORD — a false positive would be silent to the
      customer, would consume their period's entitlement, and would have no
      undo;
    * the day a cancel path is added it MUST decide what stops
      cancel→request→cancel from being an unbounded loop around «واحدة». That
      is a money question (the refund page prices a session at 150 SAR), so it
      belongs to the owner and not to whoever wires the button.
    """
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
    # The SAME row the card is showing him (see :func:`current_session`) —
    # never `_open_or_used` on this period alone. A request carried over from
    # the period before is on his screen right now; opening a second one
    # behind it would answer «سجّلنا الطلب» to an operator who is looking at
    # one, and leave two open promises for a customer who made one.
    existing = current_session(session, tenant_id=tenant_id)
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
    """One step on the session the OPERATOR IS LOOKING AT.

    AUDIT 2026-08-06 — this resolved through
    ``_open_or_used(subscription_id=current_subscription.id)`` while the card
    resolved through :func:`current_session`, which falls back across periods.
    After a renewal the two disagreed inside one screen: the card paged him
    about an overdue REQUESTED session and this answered «لا يوجد طلب جلسة
    لهذا العميل — لم نغيّر شيئًا» about the same customer. A card whose own
    button contradicts it is worse than no card, because the operator has to
    decide which half of one screen is lying.

    So the write side asks the read side. It also no longer refuses on «no
    current subscription»: an unanswered promise outlives its period (that is
    the whole point of the fallback), and a customer whose subscription has
    since lapsed is still owed the session he asked for — the card shows it,
    so the button must be able to close it.
    """
    row = current_session(session, tenant_id=tenant_id)
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


def _oldest_open(session: Session, *, tenant_id: uuid.UUID) -> CareerSession | None:
    """The longest-unanswered session this customer has, from ANY period."""
    return session.execute(
        select(CareerSession)
        .where(CareerSession.tenant_id == tenant_id,
               CareerSession.status.in_(sorted(OPEN_STATES)))
        .order_by(CareerSession.requested_at)
    ).scalars().first()


def current_session(
    session: Session, *, tenant_id: uuid.UUID
) -> CareerSession | None:
    """This period's session, whatever state it is in — the card's answer to
    «وش صار في جلسته؟».

    With a fallback to any period, and the fallback is the correction. This
    read is keyed on ``subscription_id``, which is right for the entitlement
    («واحدة» per period) and wrong for the card: since §16 a renewal creates a
    NEW subscription row, so a customer who asked for a session, was never
    answered, and then RENEWED had their unanswered request disappear from
    their own card at the exact moment they paid us again. The row was never
    lost — :func:`open_sessions` still carried it into the operator's queue —
    but the card is what he opens when that customer writes to him, and it
    would have told him there was nothing outstanding.

    An entitlement expires with its period; an unanswered promise does not.

    AUDIT 2026-08-06 — and because this is what the operator SEES, it is now
    also what every operator action writes to: :func:`request_session` and
    :func:`_transition` both resolve here. They used to key on
    ``current_subscription.id`` on their own, so after a renewal the card and
    its own confirm button described two different worlds in one screen.
    One resolver, one answer.
    """
    subscription = current_subscription(session, tenant_id)
    row = (
        _open_or_used(session, subscription_id=subscription.id)
        if subscription is not None else None
    )
    return row if row is not None else _oldest_open(session, tenant_id=tenant_id)


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


def _tenant_code(session: Session, tenant_id: uuid.UUID) -> str:
    """§15.13: the operator's channel names a TEN code and nothing else."""
    return session.execute(
        select(Tenant.code).where(Tenant.id == tenant_id)
    ).scalars().first() or "TEN-????"


def _notify(admin_client: Any, text: str) -> None:
    """Best-effort page. The ticket in `support_events` is the durable half —
    the alert is the copy that gets read tonight, and an unreachable console
    must never be the reason a sweep stops halfway through the queue."""
    if admin_client is None:
        return
    try:
        admin_client.send_admin(text)
    except Exception:  # noqa: BLE001 — the ticket is the durable half
        logger.warning("career session alert failed", exc_info=True)


def escalate_overdue(
    session: Session, *, now: datetime, admin_client: Any = None,
) -> dict[str, int]:
    """Raise a ticket for every request that outlived «الرد خلال ٢٤ ساعة».

    A request nobody answers does not fail loudly — it just sits, and the
    customer decides on their own that the human half of what they paid 449
    riyals for is not real. So it escalates into `support_events`, where it
    joins the queue the operator already works, and stamps ``escalated_at``
    so the ticket is raised once and not once per sweep.

    Swept HOURLY since 2026-08-07, through :func:`escalate_overdue_and_commit`
    — from the conversation worker's housekeeping block, and from
    ``cv.daily_run.sweep_promises`` as a backstop. That second caller is the
    one this function had for its whole life, and it rides the delivery day
    (``engine.cli`` → ``cv.daily_run.run_daily_delivery``) on
    ``ops/systemd/career-engine-nightly.timer`` — **11:00 Riyadh** — so it was
    the whole schedule of a 24-hour promise measured once a day. A request
    that crossed the SLA at 12:05 was escalated at 11:00 the next morning:
    ~23 hours late on a promise 24 hours long, which is a promise very nearly
    measured after it has already expired. The cadence argument, and why the
    nightly is kept anyway, are in :func:`escalate_overdue_and_commit`.

    Idempotent at ANY frequency, and by the row rather than by the schedule:
    the select takes only ``REQUESTED`` rows with ``escalated_at IS NULL``,
    both branches below stamp it, and nothing ever clears it — so it is a
    one-way edge that behaves the same on the twenty-fourth pass of a day as
    on the first. There is no date arithmetic anywhere in this function and
    no per-day counter: :func:`_is_overdue` compares flat hours, and the
    returned counters describe THIS PASS, never a day. What that gate does
    NOT cover is two passes in flight at the same instant, which was
    impossible with one caller and is not with two — hence the advisory lock
    the pass opens with.

    The ledger's own age is exact and the console reads it live, so the delay
    was always in the ALERT and never in the record.

    ── WHAT THIS STILL DOES NOT MEASURE ─────────────────────────────────────
    «الرد خلال ٢٤ ساعة» is a promise about the OPERATOR'S REPLY, and the only
    thing here that stops the clock is :func:`mark_scheduled` — a button he
    taps. So this measures «we noticed nobody had tapped», an hour after the
    fact instead of a day, and nothing measures whether he then answered: the
    ticket's own age is the entire signal, and it is swept to ``released``
    after 48h by `telegram.console.release_forgotten_tickets` without anyone
    having answered anything. Closing that needs a product decision, not a
    faster sweep — see :func:`escalate_overdue_and_commit`.
    """
    counts = {"escalated": 0, "unticketed": 0}
    if not session.execute(
        select(func.pg_try_advisory_xact_lock(_SLA_SWEEP_LOCK_KEY))
    ).scalar_one():
        # Another process is mid-pass over the same rows. Standing down loses
        # nothing — the pass holding the lock is looking at exactly this work,
        # and this caller returns within SLA_SWEEP_INTERVAL_SECONDS. Logged
        # rather than counted, because the counters describe WORK and none was
        # done; a zero here means «nobody was escalated», which is true.
        logger.info("career session SLA sweep already in flight — this pass "
                    "stands down")
        return counts
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
            # AUDIT 2026-08-06. This was «a لمّاح+ customer without a channel
            # cannot have asked», i.e. an impossible row — and it is not
            # impossible, it is a documented pairing: a §12 data-deletion
            # request DELETES `customer_channels` and KEEPS `career_sessions`
            # (privacy.RETAINED_TABLES names it), so a deleted customer with an
            # unanswered request lands here by design.
            #
            # It logged and moved on without stamping, so the same row wrote
            # the same ERROR line every night, forever. The operator's feed
            # harvests «ERROR:» lines, which makes a permanent one worse than
            # useless: it is camouflage for the next real failure. Stamped and
            # announced ONCE instead — and the alert says outright that no
            # ticket exists, because `support_events` needs a channel and the
            # only outcome worse than the noise is a record claiming a ticket
            # was raised when none was.
            row.escalated_at = now
            counts["unticketed"] += 1
            logger.warning("overdue career session with no channel — no "
                           "ticket possible, escalated by alert only")
            _notify(admin_client,
                    "📅 طلب جلسة مسار تجاوز مهلة الرد ولا نقدر نفتح له تذكرة\n"
                    f"{_tenant_code(session, row.tenant_id)}\n"
                    "قناته محذوفة — غالبًا نفّذ طلب حذف بياناته")
            continue
        session.add(SupportEvent(
            id=uuid.uuid4(), tenant_id=row.tenant_id, channel_id=channel.id,
            kind=OVERDUE_TICKET_KIND, status="open",
        ))
        row.escalated_at = now
        counts["escalated"] += 1
        _notify(admin_client,
                "📅 طلب جلسة مسار تجاوز مهلة الرد\n"
                f"{_tenant_code(session, row.tenant_id)}\n"
                "وعدنا مشتركي لمّاح+ بالرد خلال أربع وعشرين ساعة")
    session.flush()
    return counts


def escalate_overdue_and_commit(
    session: Session, *, now: datetime, admin_client: Any = None,
) -> dict[str, int]:
    """The entry point every SCHEDULED caller uses — both of them, checked:
    the worker's hourly housekeeping block and `cv.daily_run.sweep_promises`
    as the nightly backstop. It said «should use» while the nightly still
    re-implemented the contract with a hand-rolled try/except and a bare
    rollback that could escape; that is fixed, so the word is now «uses».
    Sweep, commit, never
    raise.

    ── WHY AN HOUR ─────────────────────────────────────────────────────────
    Until 2026-08-07 :func:`escalate_overdue` had exactly one caller,
    ``cv.daily_run.sweep_promises``, on the 11:00 Riyadh delivery timer. On a
    72-hour guarantee a daily read is 32% late; on a **24-hour** promise it is
    96%, and the arithmetic is the whole point: a request that crossed the SLA
    at 12:05 was noticed at 11:00 the next day, by which time the promise had
    not merely been broken, it had been broken for almost as long as it had
    existed. The store sells «الرد خلال ٢٤ ساعة» and the measurement arrived
    at hour forty-seven.

    An hour is the right grain, and the finer ones were considered and are
    the same three the 72-hour sweep weighed (`promises.guarantee
    .sweep_and_commit`). Reused rather than re-derived, because the shape is
    identical — a poll, a human at the far end, one process already awake —
    with one number changed and one argument that lands HARDER here:

    * **Every minute** buys 59 minutes of paper precision. What sits at the
      far end of this alert is a person opening a WhatsApp conversation and
      writing to a customer; nothing about that changes between 12:05 and
      13:00, and an operator who is asleep is not woken 59 minutes sooner in
      any sense that reaches the customer. The cost is a full pass over every
      open request sixty times an hour, forever.
    * **A job armed at each request's own deadline** sounds exact and is the
      most fragile version. A schedule that is LOST — a process that died
      between arming and firing, a row written while the scheduler was down —
      is a promise that is never measured at all, with nothing anywhere to
      notice the silence. That is the exact failure mode this whole module was
      written against, rebuilt one layer up. A poll is self-healing by
      construction: the next pass finds whatever the last one missed.
    * **Four-hourly** would still be coarser than the operator's own reaction
      time and saves nothing real — the process that runs this is already
      awake and already sweeping on the hour.

    And the argument that lands harder: on a 24-hour promise, an hour of
    lateness is 4% and a day is 96%. The hour is not a refinement of the
    nightly here; it is the difference between measuring the promise and
    measuring its epitaph.

    ── WHERE IT RUNS, and why not a timer of its own ────────────────────────
    In ``scripts/run_worker_loop.py``'s hourly housekeeping block, beside the
    enrichment sweep, the outcome questions, the 72-hour guarantee, the weekly
    report and the forgotten-ticket release. That process is up permanently
    under a systemd watchdog, it already holds the operator's Telegram client
    and a session factory, and it is the one that is awake at 03:00.

    A dedicated unit was rejected for the reason the guarantee rejected it,
    and the reason is not «consistency»: a unit has to be INSTALLED by hand
    before the measurement improves at all, ``scripts/verify_restore.sh`` §6
    checks a fixed list of units it would not be on, and so the failure mode
    of the fix is that the fix is silently absent. An uninstalled timer
    improves nothing while looking like it does.

    ── WHY THE NIGHTLY STILL CALLS IT ───────────────────────────────────────
    Kept on purpose, as the backstop for the hour the worker is not there (a
    wedged cycle, a deploy, a host that came back without it). It cannot
    double-escalate, and that is a property of the ROW, not of the schedule:
    ``escalate_overdue`` selects only ``REQUESTED`` rows whose
    ``escalated_at`` is NULL, both of its branches stamp it before returning,
    and nothing in this module or the console ever clears it. One ticket, one
    page, one stamp — at one pass a day or at twenty-five.

    The one thing that gate does not cover is two passes in flight at the SAME
    INSTANT: both read the same unstamped row before either commits, and both
    insert a ticket and page the operator. Impossible with one caller,
    possible with two — so the pass takes ``pg_try_advisory_xact_lock`` on a
    key of its own and the loser stands down. Its own key, and NOT the
    guarantee's: see :data:`_SLA_SWEEP_LOCK_KEY` for why the two promises must
    not be able to silence each other.

    ── THE CONTRACT ─────────────────────────────────────────────────────────
    Commits its own work and NEVER raises. The worker's hourly block is a
    barrier — an escape from it costs that cycle its watchdog pet, and in the
    nightly it would cost a paying customer's delivery day. A failed pass
    rolls back whole: the ticket and the ``escalated_at`` stamp are in one
    transaction, so a crash re-escalates on the next pass rather than leaving
    a stamped request with no ticket behind it.

    ── WHAT AN HOURLY SWEEP STILL DOES NOT BUY ──────────────────────────────
    Stated here because it is the honest limit and it must not be mistaken for
    fixed. The ticket is now opened up to 23 hours sooner — and NOTHING in
    this system measures whether the operator then answered within the 24
    hours we sold. «الرد خلال ٢٤ ساعة» is a promise about HIS REPLY; what is
    measured is our noticing. Concretely:

    * the SLA clock is stopped by :func:`mark_scheduled`, which is a BUTTON.
      A customer answered on WhatsApp in ten minutes by an operator who never
      tapped it still escalates; a request the operator tapped and never wrote
      to reads as answered. The ledger measures taps, not replies.
    * once the ticket exists, its age is the only signal there is, and it is
      not a measurement of anything — nothing reports «he replied in 3h», and
      after 48h ``telegram.console.release_forgotten_tickets`` moves the
      ticket to ``released`` without a human having answered a thing.

    What WOULD measure the real promise is a stop-clock read from the
    CONVERSATION instead of from a button: an ``answered_at`` on this row,
    stamped from the first outbound message this customer received after
    ``requested_at``, and the promise reported as ``answered_at -
    requested_at <= 24h`` per request rather than as «a ticket exists».
    `delivery_messages` already carries one row per send, per tenant, with a
    ``kind`` and a timestamp, so the query is small.

    The reason it is not written here is that it rests on a PRODUCT decision
    about what counts as «رد», and every available answer is wrong in a
    different direction:

    * **Any outbound.** Cheapest and false: today's opportunity bundle went
      out at 11:00 to every ACTIVE customer, and it answers nothing he asked.
      Every request would score as answered inside the first delivery day.
    * **Only ``kind == 'operator_reply'``** — the console's own reply box,
      the one send in this system a human types (`telegram.console._run_reply`
      writes it). Honest about intent and blind in the case the tier is SOLD
      on: «تلقاني على نفس المحادثة» invites Fahad to answer from WhatsApp on
      his phone, which writes no row here at all. He would answer in four
      minutes and the report would say he never answered — a metric that
      punishes the promised behaviour is worse than no metric.
    * **Ask the customer**, or have the operator close the request himself,
      which is what :func:`mark_scheduled` already is.

    So the honest options are: make the console reply box the only supported
    way to answer a لمّاح+ request (a workflow constraint on Fahad, not a
    schema change), or ingest his own outbound from the WhatsApp Business
    account so a reply typed on his phone lands in `delivery_messages` too
    (a Meta webhook subscription, and a decision about storing his message
    bodies beside the customer's). Until one of those is chosen,
    ``answered_at`` would be a column that looks like the promise and measures
    something else — so this pass makes the NOTICING an hour late instead of a
    day late, and does not pretend to have measured the reply.
    """
    zero = {"escalated": 0, "unticketed": 0}
    try:
        counts = escalate_overdue(session, now=now, admin_client=admin_client)
        session.commit()
        return counts
    except Exception:  # noqa: BLE001 — a sweep never wedges its caller
        logger.error("career session SLA sweep failed — no overdue request "
                     "was ticketed or paged in this pass", exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — a dead session must not loop either
            logger.error("career session SLA rollback failed", exc_info=True)
        return zero


# ── the OTHER human promise on the same 449 pass: the direct line ───────────
#: «تواصل مباشر معي — أنا اللي بنيت لمّاح، وتلقاني على نفس المحادثة … اكتب لي
#: وقت ما تحتاج، بلا موعد وبلا انتظار دورك» (store page, لمّاح+).
#:
#: It lives in this module because it is the same tier, the same human, and
#: the same ledger discipline as the session above — and because the audit
#: that found it (STORE-PAGES-AR §6, DEVIATIONS §6) found it in exactly the
#: same shape: sold, and reachable by nobody. WHAT IT FOUND, in the tense that
#: now belongs to it: an ACTIVE customer's ordinary sentence classified as
#: ``OTHER`` in :mod:`career.whatsapp.inbound`, the worker answered it with a
#: template, and no operator was ever told it happened; the only thing that
#: reached a human was the word «دعم», which every 199 customer has too — so
#: what the higher tier actually bought was a star on somebody else's alert,
#: not access. The CLASSIFICATION is unchanged and does not need to change
#: (second bullet below); what changed is the worker's last branch, which now
#: calls :func:`escalate_direct_message`, so an ``OTHER`` from this tier lands
#: on the operator's tickets screen instead of nowhere.
#:
#: THREE decisions are load-bearing here, and each of them is a live incident
#: this file or its neighbour already paid for once:
#:
#: * NO KEYWORD. The 24-hour escalation above says why in full (see
#:   :func:`_open_or_used`): a keyword is a guess, a false positive is silent
#:   to the customer, and `whatsapp.inbound` has TWO live misses from reading
#:   ordinary words as commands («مساعده», and «تسويق، دعم، مبيعات» read as a
#:   support ticket). The tier does not sell a word — it sells «any message» —
#:   so a word is the wrong instrument in the first place.
#: * THE TIER TEST IS NOT IN THE CLASSIFIER. ``classify_inbound`` is pure by
#:   construction (its module imports nothing from `career` but the Arabic
#:   fold) and cannot know who is paying. Entitlement is read HERE, from the
#:   subscription row, where it is a fact rather than a spelling.
#: * NOTHING IS SAID TO THE CUSTOMER. This function sends the customer no
#:   message at all and asks the caller to send none: nothing in this system
#:   guarantees a human is awake, and «أحد من الفريق بيتواصل معك» from a
#:   robot at 2am is a promise made by the wrong party. The worker's existing
#:   `_ACTIVE_FALLBACK` already says only true things.
DIRECT_ACCESS_PLANS: frozenset[str] = SESSION_PLANS

#: `support_events.kind` (String(32)) for a لمّاح+ customer's free-form
#: message. A ticket rather than a bare alert, and deliberately in the SAME
#: table «دعم» writes to: the operator already works that queue from the
#: console's tickets screen, it already has an age and a close button, and a
#: second inbox nobody has the habit of opening is how the FIRST version of
#: this promise died.
DIRECT_MESSAGE_TICKET_KIND = "executive_direct_message"

#: Where the direct line is no longer owed. The money-reversal states (a
#: refund/cancel/chargeback is the period being unwound) plus EXPIRED — the
#: pass has ended, and «مشترك لمّاح+» is exactly what that customer is not.
#: SUSPENDED and GRACE are pointedly NOT here: a disputed account and a
#: customer inside their renewal window are both people we still owe a human.
DIRECT_ACCESS_OFF: frozenset[str] = sub_states.TERMINAL_STATES | {
    sub_states.EXPIRED
}

#: The alert. TEN code alone on its own line (§15.13) and never one character
#: of what the customer wrote — the body is PII and the operator reads it in
#: the same WhatsApp conversation the tier is sold on.
DIRECT_MESSAGE_ALERT_AR = (
    "⭐ مشترك لمّاح+ راسلك مباشرة\n"
    "{code}\n"
    "باقته تبيع: اكتب لي وقت ما تحتاج — رسالته في واتساب، وترد عليه من بطاقته"
)

#: The same customer, in a shape neither we nor this alert can read: a voice
#: note, a photo, a clip. It gets its own sentence rather than the one above
#: because the one above quietly promises a message the operator can go and
#: READ, and a customer's fifty-second voice note is not that. The tier sells
#: access, not a file format, and a voice note is the commonest thing a Saudi
#: customer sends — so it escalates like any other message, and the alert says
#: plainly what is waiting and that he has to open WhatsApp to get it.
#: ``{shape}`` is an Arabic noun the CALLER supplies (WhatsApp message types
#: are the worker's vocabulary, not this module's); the TEN code keeps its own
#: line (§15.13 + the bidi rule).
#:
#: The noun keeps a line of its own too, and NOT because it scrambles: every
#: value that reaches it is Arabic («رسالة صوتية», «صورة», «مقطع» — the whole
#: of `whatsapp.worker._SPOKEN_MEDIA`, the one caller), so «أرسل لك صورة» was
#: always safe to read. What is not safe is the guarantee: nothing on this
#: side of the call can hold a future caller to an Arabic noun, and the day
#: one passes «voice note» — or a message type verbatim — that line reverses
#: and takes the operator's «مشترك لمّاح+» with it. On its own line the worst
#: case is a Latin word sitting on a Latin line, which reads fine.
DIRECT_MESSAGE_MEDIA_ALERT_AR = (
    "⭐ مشترك لمّاح+ أرسل لك:\n"
    "{shape}\n"
    "{code}\n"
    "ما نقدر نفتحها من هنا — افتح محادثته في واتساب وشوفها، وترد عليه من بطاقته"
)


def _mid_flow(session: Session, tenant_id: uuid.UUID) -> bool:
    """Is this customer in the middle of a conversation WE started?

    An onboarding answer, a consent-gate reply or a funnel upload is not «a
    message for the operator» — it is the answer to a question we asked, and
    `whatsapp.inbound` has broken twice in precisely that direction: «مساعده»
    read as a support ticket, and a comma-separated list of career fields read
    as one. Both times a paying customer's answer was thrown away, a ticket
    was raised against someone who had asked for nothing, and the question came
    back.

    The call site is supposed to make this impossible already — the escalation
    belongs in the worker's LAST branch, after the funnel, the journey, the
    standing privacy commands, the outcome buttons and the enrichment session
    have each had their turn. This guard exists because «supposed to» is what
    the two incidents above were also relying on: a future edit that moves the
    call one branch too early would re-open the same hole, and a rule that is
    only a comment is a rule that gets moved.

    AUDIT 2026-08-07 — this used to end by admitting that an F-ENRICH answer
    was invisible here, «so for that one the branch ORDER at the call site is
    the only defence». It was not a defence at all. F-ENRICH runs with the
    journey ACTIVE, so the state test above waves it through, and the call
    site's enrichment branch declines the message and falls through whenever
    ``orchestrator.handle_enrichment`` returns False — at which point a
    customer's achievement («قللت وقت الإغلاق من عشرة أيام لأربعة») becomes a
    support ticket raised against him and the answer is gone. So the
    enrichment session is read here, from the journey's own context, where a
    branch order cannot reach it.

    CORRECTED 2026-08-07, same day, IN PLACE — because the correction has to
    travel the way the error did. The paragraph above used to name the reason
    ``handle_enrichment`` declines: «which it does, first line, when
    ``deps.achievement_renderer is None``. That is the SHIPPED default.» Both
    halves were false, and the second was copied out of this docstring into
    the project's own state before anybody read the wiring.

    * There is no such first line any more. ``handle_enrichment`` decides
      OWNERSHIP before CAPABILITY: it returns False only when nothing is open
      (no channel row, no journey, journey not ACTIVE, cursor not ``open``),
      and the renderer is consulted AFTER that — a missing one now CLOSES the
      session, answers the customer, and returns True. Which branch owns an
      enrichment answer no longer depends on what a process has wired.
    * ``None`` was never shipped. ``scripts/run_worker_loop.py`` — the only
      construction of ``orchestrator.Deps`` outside the tests — passes
      ``achievement_renderer=AnthropicAchievementRenderer(...)``, and has
      since F-ENRICH's own commit; ``None`` is a dataclass default that only
      tests exercise. So the incident this guard was written against never
      had a live population.

    The guard stays, and on the narrower ground it actually stands on: ANY
    decline by ``handle_enrichment`` — not a renderer outage — lands the
    message in the branch below, and «the call site is ordered correctly» is
    the assurance this module twice paid for trusting.

    What remains uncovered is now ONE thing and it is a tap, not a sentence:
    an outcome-button tap arrives with the journey ACTIVE and nothing open in
    this row. The call site no longer relies on order for it either — it
    refuses to escalate ANY tap, on the ground that a tap can only come from a
    card we ourselves sent (see ``whatsapp.worker``).
    """
    from career.db.models import FunnelSession, OnboardingSession

    journey = session.execute(
        select(OnboardingSession.state, OnboardingSession.context)
        .where(OnboardingSession.tenant_id == tenant_id)
    ).first()
    if journey is not None:
        state, context = journey
        if state != "ACTIVE":
            return True
        if ((context or {}).get("enrichment") or {}).get("open"):
            return True
    funnel = session.execute(
        select(FunnelSession.state).where(FunnelSession.tenant_id == tenant_id)
    ).scalars().first()
    return funnel is not None and funnel != "DONE"


def escalate_direct_message(
    session: Session, *, tenant_id: uuid.UUID, channel_id: uuid.UUID,
    now: datetime, inbound_message_id: uuid.UUID | None = None,
    unreadable: str | None = None, admin_client: Any = None,
) -> str:
    """A لمّاح+ customer said something ordinary — put it in front of a human.

    Returns one outcome name, never a boolean, for the same reason
    :func:`request_session` does: «he is not on that tier», «his pass ended»
    and «he already has a ticket open» are three different answers, and a
    caller that cannot tell them apart cannot log the truth about any of them.

        escalated | already_open | in_flow | not_entitled | lapsed
        | no_subscription

    ``unreadable`` is the Arabic noun for a message that arrived in a shape
    the operator cannot open from his console — «رسالة صوتية», «صورة»,
    «مقطع». It changes only the wording of the alert, never whether one is
    raised: a voice note is the commonest thing a Saudi customer sends, and a
    tier that answers it with «I can only read text» and nobody at the other
    end is selling a file format instead of access. The caller supplies the
    noun because WhatsApp message types belong to the WhatsApp module.

    ONE open ticket per customer, exactly like the funnel's consent stall: a
    customer who writes four messages in a row is one human waiting, not four,
    and paging per message trains the operator to stop reading the alerts.

    AUDIT 2026-08-07 — that dedupe was challenged on a real scenario and
    survives, but the argument had to change and the residue is bigger than
    the old note admitted. The challenge: a false positive on Monday burns the
    only slot, and Tuesday's real «أبي رأيك في عرض وظيفي وصلني» returns
    ``already_open`` and pages nobody. That is true, and the answer is NOT to
    page per message — it is that a false positive must not happen, which is
    why the call site now refuses to escalate a tap, an enrichment answer or a
    typed outcome label at all. What is left is genuine messages, and for
    those one ticket is right: the operator's queue is a list of people
    waiting, not a list of sentences, and he answers it by opening the
    conversation, where every message he has not read is sitting in order.

    Two pieces of residue, stated rather than glossed.

    (1) A second SUBJECT from the same customer raises nothing and moves
    nothing: the ticket keeps the age and the ``inbound_message_id`` of the
    FIRST message. The console renders the SHAPE of that first message from
    the id (``console._TICKET_OPENER_AR``), so the queue says «since when»
    and «written or recorded» — never «about what», and never that a second
    question exists at all until he opens the conversation.

    (2) CORRECTED 2026-08-07, same day. This read «nothing closes a ticket
    except the operator's own button, and nothing sweeps a forgotten one — so
    a ticket left open indefinitely silences that customer's direct line
    indefinitely». The first half stands; the second was true when written and
    is not true now, and ``telegram.console.release_forgotten_tickets`` cites
    THIS sentence as the request it fulfils — so left alone it would have two
    files describing one mechanism in contradictory tenses.

    What sweeps: a ticket still ``open`` after ``console.TICKET_FORGOTTEN_AFTER``
    (48h) is moved to ``released``. That is not a closure — it is the removal
    of the MUTE, so the dedupe above stops matching and the customer's next
    message raises a ticket of its own with its own age and its own id. It
    runs off the operator's own console traffic and, since the same audit,
    hourly from ``scripts/run_worker_loop.sweep_forgotten_tickets``, the
    process that is awake at 03:00 whether he taps or not.

    So the residue is a BOUND rather than an indefinite, and the bound is the
    honest thing to write down: one forgotten ticket can mute this customer's
    direct line for up to 48 hours — twice the «الرد خلال ٢٤ ساعة» the same
    card sells — and no sweep closes the ticket, answers the customer, or
    measures that 24 hours (DEVIATIONS D26 item 5). All of it is still bounded
    by the same act (he opens the card, answers the human, closes the ticket)
    and none of it is fixed here.

    The ticket is stamped with the message's own time rather than the
    database's clock, because the age on that screen is the whole promise:
    «بلا انتظار دورك» is a claim about how long somebody waited, and the only
    honest starting point is when they wrote.

    The alert is best-effort over a fact that is already recorded (:func:`_notify`),
    and it is honest even if the caller's transaction later rolls back: it says
    a customer wrote to you, which happened, and not «a ticket was opened».
    """
    if _mid_flow(session, tenant_id):
        return "in_flow"
    subscription = current_subscription(session, tenant_id)
    if subscription is None:
        return "no_subscription"
    if subscription.plan_code not in DIRECT_ACCESS_PLANS:
        return "not_entitled"
    if subscription.status in DIRECT_ACCESS_OFF:
        return "lapsed"
    already_open = session.execute(
        select(SupportEvent.id).where(
            SupportEvent.tenant_id == tenant_id,
            SupportEvent.kind == DIRECT_MESSAGE_TICKET_KIND,
            SupportEvent.status == "open",
        ).limit(1)
    ).scalars().first()
    if already_open is not None:
        return "already_open"
    session.add(SupportEvent(
        id=uuid.uuid4(), tenant_id=tenant_id, channel_id=channel_id,
        inbound_message_id=inbound_message_id,
        kind=DIRECT_MESSAGE_TICKET_KIND, status="open", created_at=now,
    ))
    session.flush()
    code = _tenant_code(session, tenant_id)
    _notify(admin_client, (
        DIRECT_MESSAGE_MEDIA_ALERT_AR.format(code=code, shape=unreadable)
        if unreadable else DIRECT_MESSAGE_ALERT_AR.format(code=code)
    ))
    return "escalated"


def completed_sessions_count(
    session: Session, *, tenant_id: uuid.UUID,
    subscription_id: uuid.UUID | None = None,
) -> int:
    """How many sessions this customer has actually received — the number the
    refund page's «تُخصم قيمة الخدمات البشرية اللي استلمتها فعليًا» is
    computed from. ``subscription_id`` narrows it to one paid period; None is
    the customer's whole history, which is what a lifetime question wants and
    what a refund emphatically does not (see :func:`refund_deduction_sar`)."""
    query = select(CareerSession.id).where(
        CareerSession.tenant_id == tenant_id,
        CareerSession.status == COMPLETED,
    )
    if subscription_id is not None:
        query = query.where(CareerSession.subscription_id == subscription_id)
    return len(session.execute(query).all())


def refund_deduction_sar(
    session: Session, *, tenant_id: uuid.UUID,
    subscription_id: uuid.UUID | None = None,
) -> Decimal:
    """The published deduction for ONE order, computed rather than remembered.

    The refund page promises «ونعرض عليك الأرقام قبل موافقتك», and the numbers
    have to come from somewhere: this is that somewhere.

    AUDIT 2026-08-06 — «اللي استلمتها فعليًا» is scoped to the thing being
    refunded, and this counted the customer's whole life. A refund is against
    one Salla order; a لمّاح+ customer in their fourth period who took the
    session they paid for each time would have had 600 SAR deducted from a 449
    SAR refund — a negative number, printed to the customer under a sentence
    that promises we will show him the figures. Every one of those sessions was
    real and every one of them was already paid for by the period it belonged
    to; charging them again against a different order is charging twice.

    ``subscription_id`` defaults to the live period, which is the one a refund
    request is almost always about. When the tenant holds no subscription row
    at all there is no order to refund and therefore nothing to deduct — and
    reaching for the lifetime count there would resurrect exactly the
    over-deduction this exists to remove.
    """
    if subscription_id is None:
        subscription = current_subscription(session, tenant_id)
        if subscription is None:
            return Decimal("0")
        subscription_id = subscription.id
    return SESSION_VALUE_SAR * completed_sessions_count(
        session, tenant_id=tenant_id, subscription_id=subscription_id
    )


def plan_of(session: Session, tenant_id: uuid.UUID) -> str | None:
    """The live plan code, for callers that only need the label."""
    subscription: Subscription | None = current_subscription(session, tenant_id)
    return subscription.plan_code if subscription is not None else None
