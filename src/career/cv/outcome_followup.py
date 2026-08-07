"""The outcome question — the only measure the product is actually judged by
(§20 F-OUTCOME).

We know who tapped «قدّمت» and we have never once learned what happened next.
«We sent forty-four CVs» is an operational number; «he got three interviews»
is the number that says whether any of this works. Without it every change to
a prompt, a gate weight or a ranking rule is a gamble settled only by a
complaint.

So fourteen days after an application, one question with three buttons, and
the answer is recorded as the NEXT stage on the same job — an application
becomes a path rather than a single event.

Every constraint here is deliberate:

* **Once per application, forever.** Someone who answered is never asked
  again, and someone who ignored it is asked exactly once and then left
  alone. A question that repeats is a nuisance, and a nuisance costs more
  than the datum is worth.
* **Inside the free 24h window only.** No paid template, no message at a bad
  hour: if the window is shut we wait, and after thirty days we close the ask
  silently, because the answer's value decays and a stale question is worse
  than none.
* **Never to someone who stopped messages.** (An earlier draft of this note
  also promised «never on a day they are already receiving a delivery»; no
  such check existed, so the promise is removed rather than left as fiction.
  In practice the two rarely collide: delivery runs at eleven, this sweep
  fires hourly on an open window.)

Built BEFORE launch on purpose: this data cannot be recovered afterwards. A
customer who applied today and was not asked will not remember in a month.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.arabic import normalize_ar
from career.db.models import CustomerChannel, OutcomeEvent
from career.whatsapp.delivery import record_out
from career.whatsapp.window import WindowState, window_state

logger = logging.getLogger("career.cv")

#: Long enough that a real process has had time to move (most employers reply
#: or go silent within two weeks), short enough that the customer still
#: remembers which job we mean.
ASK_AFTER_DAYS = 14

#: After this we stop waiting for an open window and close the ask silently.
GIVE_UP_AFTER_DAYS = 30

#: How long an ASKED question stays open before we accept it went unanswered.
#: Without this a single ignored question blocked that customer forever.
ANSWER_WINDOW_DAYS = 7

#: The application stage we follow up on, and the stages that end the thread.
#: Deliberately short: outcome_events.outcome is VARCHAR(16), and widening a
#: column on an append-only ledger to fit a prettier word is the wrong trade.
APPLIED = "applied"
ASKED = "asked"
#: Terminal answers — any of these means this job's path is complete.
INTERVIEW = "interview"
NO_REPLY = "no_reply"
REJECTED = "rejected"
GAVE_UP = "unanswered"

_ANSWERS: dict[str, str] = {
    "oc_interview": INTERVIEW,
    "oc_noreply": NO_REPLY,
    "oc_rejected": REJECTED,
}

#: One question, three taps, no typing. The job title rides on its own line —
#: it is often English, and a mixed line scrambles in the customer's client.
QUESTION_AR = (
    "سؤال سريع 🙏 وآخر مرة عن هالفرصة:\n"
    "{title}\n"
    "قدّمت عليها قبل أسبوعين — وش صار؟"
)

BUTTONS: tuple[tuple[str, str], ...] = (
    ("oc_interview", "جاني مقابلة 🎉"),
    ("oc_noreply", "ما ردّوا"),
    ("oc_rejected", "اعتذروا"),
)

THANKS_INTERVIEW_AR = (
    "توفيق يا رب 🤍\n"
    "وهذي أهم معلومة تعطينا إياها — نعرف منها أي فرص تستاهل وقتك فعلًا"
)
#: A tap with nothing pending — a second tap, or an old card scrolled back to.
ALREADY_ANSWERED_AR = "مسجّلة عندنا 👍 شكرًا لك"

THANKS_OTHER_AR = (
    "شكرًا لك 🙏\n"
    "حتى «ما ردّوا» تفيدنا — نتعلم منها ونحسّن اختياراتنا لك"
)


#: The same three answers as the customer's own typing, keyed on the folded
#: label. DERIVED from :data:`BUTTONS` and :data:`_ANSWERS` rather than
#: written out a second time: the labels we show and the labels we accept are
#: then one object, and a relabelled button cannot leave a stale spelling
#: behind. `normalize_ar` is what makes «ما ردّوا» and «ما ردوا» one key, and
#: what drops the 🎉 off «جاني مقابلة» — the customer types the word, not the
#: emoji.
_TYPED_ANSWERS: dict[str, str] = {
    normalize_ar(label): outcome
    for button_id, label in BUTTONS
    if (outcome := _ANSWERS.get(button_id)) is not None
}


def parse_answer(
    reply: str | None, *, question_open: bool = False,
    tapped: bool | None = None,
) -> str | None:
    """The outcome this reply means, or None.

    A machine id — «oc_noreply» — is read when ``tapped`` says the payload
    carried a button id, and then regardless of ``question_open``: a tap on an
    old card is still our own card coming back, and whether a question is
    still open decides only what we SAY back (the caller's
    :data:`ALREADY_ANSWERED_AR` branch), never whether we understood it.

    AUDIT 2026-08-07 — that used to be «read ALWAYS», justified by «a machine
    id can only have come from a card we ourselves sent, so it is by
    construction an answer to this question». The construction does not exist.
    `whatsapp.worker` computes ``effective = _button_id_of(msg) or text_body``
    and hands us THAT, so for a plain text message ``reply`` is literally what
    the customer typed — and a customer who types ``oc_interview`` while a
    question is open had it recorded through :func:`record_answer` as his
    answer, which is a §20 datum that can never be re-collected, written from
    a typed string. Unlikely, and a false invariant is worth removing at the
    price of one argument.

    The fact lives with the caller and cannot be recovered here: by the time
    the two are collapsed into one string, «id from the payload» and «the same
    characters typed by a human» are indistinguishable. So the caller states
    it. ``tapped=True`` is the payload's button id, ``tapped=False`` is
    anything a person typed, and ``None`` — the default — means the caller has
    not been taught to say. ``None`` still reads the id, exactly as before,
    and logs that it did, because there is one call site left in that state
    (`whatsapp.worker`, whose fix is a single keyword) and a transitional
    default that is silent is a transitional default that becomes permanent.
    When that call site passes ``tapped=``, this arm and this paragraph go.

    A TYPED label — the customer writes «ما ردّوا» instead of tapping — is read
    only when ``question_open`` is true. Until this parameter existed the
    typing was read nowhere: `whatsapp.worker` derived its own label map from
    :data:`BUTTONS` inside the message loop, and before THAT the sentence fell
    through every branch and opened a لمّاح+ direct-message ticket against the
    customer — our own question turned into a complaint, and the one §20 datum
    that can never be re-collected thrown away.

    Two rules keep this from becoming the over-eager reading this file's
    neighbours have already paid for twice («مساعده» read as a support
    command, «تسويق، دعم، مبيعات» read as one):

    * **The whole message, or nothing.** Matching is equality on the folded
      string, never a substring: «ما ردّوا عليّ من الشركة اللي رشحتوها لي» is a
      sentence for a human, not an answer to a survey, and it stays one.
    * **Never a question that was not asked.** ``question_open`` is the
      caller's answer to :func:`pending_job_ref`, and it is PRESERVED rather
      than replaced — nothing inside a string can tell us that «اعتذروا» is
      about the job we asked about rather than about a wedding invitation, and
      answering «مسجّلة عندنا 👍» to someone who wrote it about something else
      both loses his message and lies to him. It is not defended by the
      caller's branch order any more, though: the default is the safe reading,
      so a future call site has to state the fact to get the wider one.

    Deliberately NOT taken here: the session. This stays a pure string
    function — it is called on every inbound message, and a parser that opens
    a query is a parser that cannot be used in the cheap position.
    """
    value = (reply or "").strip()
    if tapped is not False:
        outcome = _ANSWERS.get(value)
        if outcome is not None:
            if tapped is None:
                # No id, no phone, no body — just the fact that a call site
                # is still relying on the transitional arm.
                logger.warning(
                    "outcome: machine id read as an answer without the caller "
                    "stating it came from a tap")
            return outcome
    if not question_open:
        return None
    return _TYPED_ANSWERS.get(normalize_ar(value))


def _stages(session: Session, tenant_id: uuid.UUID, job_ref: str) -> set[str]:
    return set(session.execute(
        select(OutcomeEvent.outcome).where(
            OutcomeEvent.tenant_id == tenant_id,
            OutcomeEvent.job_ref == job_ref,
        )
    ).scalars().all())


def pending_job_ref(
    session: Session, *, tenant_id: uuid.UUID
) -> str | None:
    """The job whose outcome question is still open for this customer.

    The buttons carry the ANSWER, not the job — three fixed ids keep the
    payload inside Meta's limits and keep the customer's tap unambiguous. So
    the job is resolved here: the most recently asked application that has no
    answer yet. Asking about one job at a time is what makes that safe, and
    the sweep enforces it.
    """
    asked = session.execute(
        select(OutcomeEvent.job_ref, OutcomeEvent.occurred_at).where(
            OutcomeEvent.tenant_id == tenant_id,
            OutcomeEvent.outcome == ASKED,
        ).order_by(OutcomeEvent.occurred_at.desc())
    ).all()
    for job_ref, _ in asked:
        stages = _stages(session, tenant_id, job_ref)
        if not (stages & {INTERVIEW, NO_REPLY, REJECTED, GAVE_UP}):
            return str(job_ref)
    return None


def record_answer(
    session: Session, *, tenant_id: uuid.UUID, job_ref: str, outcome: str,
    now: datetime,
) -> str:
    """Store the answer and return the Arabic thank-you to send back."""
    from career.cv.deliver import record_outcome

    record_outcome(
        session, tenant_id=tenant_id, job_ref=job_ref, outcome=outcome,
        reason="customer_followup", now=now,
    )
    return THANKS_INTERVIEW_AR if outcome == INTERVIEW else THANKS_OTHER_AR


def stale_asked(
    session: Session, *, now: datetime
) -> list[tuple[uuid.UUID, str]]:
    """Questions asked long ago and never answered.

    These MUST be closable, and originally they were not: the give-up path
    only ran over `due_applications`, which excludes anything already ASKED —
    so an unanswered question stayed pending forever, `pending_job_ref` never
    returned None, and every later application for that customer was skipped
    every hour for the rest of their subscription. Twenty such customers took
    the whole per-pass budget and the feature stopped collecting anything at
    all (proved by execution, not argument).
    """
    cutoff = now - timedelta(days=ANSWER_WINDOW_DAYS)
    rows = session.execute(
        select(OutcomeEvent.tenant_id, OutcomeEvent.job_ref)
        .where(OutcomeEvent.outcome == ASKED,
               OutcomeEvent.occurred_at <= cutoff)
    ).all()
    out: list[tuple[uuid.UUID, str]] = []
    for tenant_id, job_ref in rows:
        if not (_stages(session, tenant_id, job_ref)
                & {INTERVIEW, NO_REPLY, REJECTED, GAVE_UP}):
            out.append((tenant_id, str(job_ref)))
    return out


def due_applications(
    session: Session, *, now: datetime
) -> list[tuple[uuid.UUID, str, datetime]]:
    """(tenant, job_ref, applied_at) for applications ripe to ask about.

    Ripe means: applied at least ASK_AFTER_DAYS ago, never asked, and no
    answer already on record. Ordered oldest first so a backlog drains in the
    order the customer lived it.
    """
    cutoff = now - timedelta(days=ASK_AFTER_DAYS)
    rows = session.execute(
        select(OutcomeEvent.tenant_id, OutcomeEvent.job_ref,
               OutcomeEvent.occurred_at)
        .where(OutcomeEvent.outcome == APPLIED,
               OutcomeEvent.occurred_at <= cutoff)
        .order_by(OutcomeEvent.occurred_at)
    ).all()
    out: list[tuple[uuid.UUID, str, datetime]] = []
    for tenant_id, job_ref, applied_at in rows:
        stages = _stages(session, tenant_id, job_ref)
        if stages & {ASKED, INTERVIEW, NO_REPLY, REJECTED, GAVE_UP}:
            continue          # already asked, already answered, or closed
        out.append((tenant_id, job_ref, applied_at))
    return out


def _title_for(session: Session, job_ref: str) -> str:
    """The customer's own words for this job, or the reference as a fallback."""
    from career.db.models import JobPosting

    row = session.execute(
        select(JobPosting.title, JobPosting.company).where(
            JobPosting.url == job_ref
        )
    ).first()
    if row is None:
        return job_ref
    title, company = row
    return f"{title} — {company}" if company else str(title)


def sweep_outcome_questions(
    session: Session,
    *,
    whatsapp_client: Any,
    now: datetime,
    limit: int = 20,
) -> dict[str, int]:
    """Ask, or wait, or give up — one honest pass. Safe to run hourly."""
    counts = {"asked": 0, "waiting_window": 0, "gave_up": 0, "skipped": 0,
              "closed_unanswered": 0}

    # FIRST: retire questions nobody answered. Doing this before anything else
    # is what keeps one ignored question from blocking a customer forever.
    from career.cv.deliver import record_outcome

    for tenant_id, job_ref in stale_asked(session, now=now):
        record_outcome(
            session, tenant_id=tenant_id, job_ref=job_ref, outcome=GAVE_UP,
            reason="no_answer", now=now,
        )
        counts["closed_unanswered"] += 1
    if counts["closed_unanswered"]:
        session.flush()

    for tenant_id, job_ref, applied_at in due_applications(session, now=now)[:limit]:
        channel = session.execute(
            select(CustomerChannel).where(
                CustomerChannel.tenant_id == tenant_id
            ).order_by(CustomerChannel.created_at)
        ).scalars().first()
        if channel is None or channel.opt_out_at is not None:
            counts["skipped"] += 1
            continue

        # ONE open question per customer at a time. The buttons carry the
        # answer and not the job, so a second question would make the next tap
        # ambiguous — and two surveys at once is a nuisance besides.
        if pending_job_ref(session, tenant_id=tenant_id) is not None:
            counts["skipped"] += 1
            continue

        if now - applied_at > timedelta(days=GIVE_UP_AFTER_DAYS):
            # The answer's value has decayed past the cost of asking. Close it
            # honestly so the row is not re-examined every hour forever.
            from career.cv.deliver import record_outcome

            record_outcome(
                session, tenant_id=tenant_id, job_ref=job_ref,
                outcome=GAVE_UP, reason="window_never_opened", now=now,
            )
            counts["gave_up"] += 1
            continue

        state = window_state(
            last_inbound_at=channel.last_inbound_at,
            opt_out_at=channel.opt_out_at, now=now,
        )
        if state is not WindowState.OPEN:
            # A paid template for a survey is not worth it, and a question at
            # a bad hour is worse than no question. Wait for them to write.
            counts["waiting_window"] += 1
            continue

        try:
            mid = whatsapp_client.send_interactive(
                channel.phone_e164,
                QUESTION_AR.format(title=_title_for(session, job_ref)),
                list(BUTTONS),
            )
        except Exception:  # noqa: BLE001 — a survey never breaks the loop
            logger.warning("outcome question send failed", exc_info=True)
            counts["skipped"] += 1
            continue

        record_out(session, tenant_id=tenant_id, channel_id=channel.id,
                   kind="interactive", wa_message_id=mid, now=now)
        record_outcome(
            session, tenant_id=tenant_id, job_ref=job_ref, outcome=ASKED,
            reason="followup_sent", now=now,
        )
        # Commit per question, not per pass. The message is already gone; if
        # the loop dies before the caller commits, the ASKED row vanishes and
        # the customer is asked the very same thing again (proved: SENT TWICE).
        # A survey that repeats is the one thing this feature must never be.
        session.commit()
        counts["asked"] += 1

    return counts
