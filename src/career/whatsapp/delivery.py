"""Adaptive delivery execution (whatsapp §08).

Given a bundle (generic parts list for now; C7 fills real CV/job refs), either
send it directly (open window) or send the morning template and hold the bundle
until the customer opens the window, then descend. Every outbound message is
logged in delivery_messages for the delivery receipts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import String, select, update
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, Delivery, DeliveryMessage
from career.whatsapp.adaptive import DeliveryAction, plan_delivery
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.templates import REGISTRY, TemplateSpec, billed_category
from career.whatsapp.window import WindowState, window_state

logger = logging.getLogger("career.whatsapp")

DELIVERY_PENDING = "PENDING_WINDOW"
DELIVERY_OPENED = "OPENED"
DELIVERY_COMPLETED = "COMPLETED"
DELIVERY_NO_SEND = "NO_SEND"
DELIVERY_PARTIAL = "PARTIAL"      # some job groups failed — honest, never hidden

#: How many times one held bundle may be re-attempted before we stop trying
#: and close it honestly. A customer who taps and gets nothing deserves the
#: next message to try again — but not forever, and not on every keystroke.
MAX_DISPATCH_ATTEMPTS = 3

#: The honest outcomes of an operator «إعادة إرسال». A refusal is NOT a
#: failed attempt: only :data:`RESEND_SENT` ever touches the attempt budget.
RESEND_SENT = "sent"
RESEND_NO_BUNDLE = "no_bundle"
RESEND_OPTED_OUT = "opted_out"
RESEND_WINDOW_CLOSED = "window_closed"

#: The widest provider id the two columns that hold one can store, read off
#: the model instead of typed here. Both `delivery_messages.wa_message_id` and
#: `deliveries.template_message_id` are ``varchar(128)`` today; a migration
#: that widens either one widens this guard with it, rather than leaving a
#: number written in August 2026 rejecting ids the database would now accept.
_WA_ID_COLUMN = DeliveryMessage.__table__.c.wa_message_id.type
_WA_MESSAGE_ID_MAX: int = (
    _WA_ID_COLUMN.length if isinstance(_WA_ID_COLUMN, String) and _WA_ID_COLUMN.length
    else 128
)


def usable_message_id(value: object, *, what: str) -> str | None:
    """Meta's message id, or ``None`` when the database would refuse it.

    AUDIT 2026-08-06, second review. The last wave taught
    `salla/lifecycle._record_send` to check this value — and stopped there,
    at ONE of `record_out`'s twenty-two call sites. Every other site is a
    delivery-shaped path where the row a refused INSERT destroys is a DELIVERY
    LEDGER row: constant 3 evidence that a customer was sent his CVs, the
    source `close.whatsapp_spend` bills from, and the row a Meta receipt is
    later written onto. The reviewer reproduced it at one of them —
    ``DataError (StringDataRightTruncation)`` at commit, and the ledger row
    that mattered persisted zero times, taking the `deliveries` row with it
    because a rollback is whole.

    So the check lives at the funnel every send passes through, and the
    twenty-two callers are covered by construction rather than by twenty-two
    authors each remembering. **Rejected alternative:** repeating the check at
    each call site. That is the same shape as the defect — a rule that holds
    wherever somebody remembered it — and the twenty-third caller written next
    month would not have it.

    **Rejected alternative:** truncating to fit. A truncated id is not a
    damaged id, it is a DIFFERENT id: `worker._handle_status` matches receipts
    by exact equality on this column, and two ids sharing a 128-character
    prefix would hand one send's receipt to another send's row. Silence beats
    a wrong answer about what reached a customer.

    ``value`` is typed `object` because this is the one place in the module
    that must assume nothing: it is the parsed body of an HTTP response from
    somebody else's service. Both failure modes are real and neither is
    exotic — an id longer than the column (a provider format change) and an id
    that is not a string at all (a client returning the response envelope
    instead of digging the id out of it, one refactor away at all times).

    THE PRICE, because it is not nothing (the last wave's docstring called it
    «harmless»): a row with a NULL id can never be matched by an incoming
    receipt, so its status stays ``sent`` for good. `close.whatsapp_spend`
    bills every ``kind="template"`` row whose status is not ``failed``, so a
    template that Meta later reports as FAILED is billed anyway. That
    over-reports spend — the safe direction, and far better than the
    alternative it replaces, where the row did not exist at all and a template
    Meta charged us for was counted at zero. It is also why the log line below
    is ERROR: the operator's harvester forwards ``ERROR:`` lines only, and a
    provider whose ids stopped fitting our columns is a thing he must hear
    about the first time, not after a month of quietly unmatchable sends.
    """
    mid = value if isinstance(value, str) else None
    if mid is not None and len(mid) > _WA_MESSAGE_ID_MAX:
        mid = None
    if mid is None:
        logger.error(
            "provider message id unusable for %s — the row is written with a "
            "NULL id, so the send is still counted (and, if it is a template, "
            "still billed) but its delivery receipt can never be matched",
            what,
        )
    return mid


#: How long a template send stays eligible for Meta's own price band before we
#: stop waiting for one and write ours. Meta's status callbacks arrive in
#: seconds and its redeliveries in minutes; forty-eight hours is not an
#: estimate of that latency but a margin wide enough to cover a webhook outage,
#: a deferred `webhook_events` row working through its retries, and a night the
#: worker spent down. The cost of waiting too long is one more day of a NULL
#: that already prices correctly (`close._wa_kind_of` step 2); the cost of not
#: waiting long enough is writing a belief over a measurement that was on its
#: way. The two are not symmetric, so this is generous on purpose.
RECEIPT_SETTLES_AFTER = timedelta(hours=48)


def freeze_unmeasured_categories(
    session: Session, *, now: datetime,
    settles_after: timedelta = RECEIPT_SETTLES_AFTER,
) -> int:
    """Write OUR billing category onto template sends Meta never priced for us.
    Returns how many rows were stamped.

    ── MEASUREMENT AND BELIEF, SAID PLAINLY ────────────────────────────────
    `delivery_messages.category` holds one of two different kinds of fact and
    the column cannot tell them apart, so the rule has to:

    * **Measurement.** ``statuses[].pricing.category`` — what Meta CHARGED for
      this exact message. Written by `worker._handle_status` the moment the
      receipt lands. It is the authority and nothing here may overwrite it.
    * **Belief.** :func:`templates.billed_category` — today's best reading of
      what Meta charges for that TEMPLATE (the dated snapshot, or a fresher
      `template_category_update` push), MARKETING whenever nothing has ever
      measured it, because an unverified category must never make a bill look
      smaller than it is. That is what this function writes.

    A belief is worth writing at all for one reason, and it is not accuracy —
    `close._wa_kind_of` already falls back to exactly this value for a NULL
    row, so the number does not move on the day. It is STABILITY. That
    fallback re-asks the registry every time the question is asked, and Meta
    rewrites the registry (five templates on 2026-07-17, five again on
    2026-08-02), so `rollup_costs` over an old day answered differently
    depending on the day it was asked. Freezing the belief onto the row ends
    that: after this runs, a re-run of an old day reproduces the number that
    day was billed at, whatever Meta decides afterwards.

    ── WHY IT IS NOT ONE LINE IN `record_out` ──────────────────────────────
    Stamping the belief at SEND time is the obvious shape, it is what
    migration 0030's own note asked for, and it is wrong as the receipt half
    is built. `worker._handle_status` fills this column only while it is NULL
    and logs an ERROR rather than overwriting a value that disagrees — a
    fill-once rule that is exactly right against a second RECEIPT and blind to
    the difference between a receipt and a guess. A belief written at send
    time is always first, so it would win every disagreement, permanently, and
    the ledger would record what we expected to be charged in the one place
    built to record what we WERE charged. The whole of 0030 would be undone by
    a line that looks like a completion of it.

    Waiting removes the conflict instead of arguing with it: the WHERE clause
    below touches only rows the measurement has already declined to fill, long
    after any receipt could still arrive, so precedence is a property of the
    query rather than a rule two modules have to keep agreeing about.

    THE PRICE, because it is not nothing: the belief is read at freeze time
    and not at send time, so a template Meta re-categorised in between is
    frozen at the NEW band for an OLD send. That window is bounded by
    :data:`RECEIPT_SETTLES_AFTER` where the old behaviour was unbounded — the
    same registry read applied to all of history — and it closes for good the
    moment the row is stamped. The send-time version becomes strictly better
    the day `_handle_status` learns to overwrite a belief (a value it can only
    distinguish with something on the row that says which kind it is); that is
    a change in `whatsapp/worker.py` and a column, and it belongs to their
    owners.

    NOT STAMPED, deliberately:

    * anything but ``kind="template"`` — free-form service messages inside the
      open 24h window are not billed and `close.whatsapp_spend` never counts
      them; a band on them would be a number about nothing.
    * a ``template_name`` outside :data:`templates.REGISTRY`. `_wa_kind_of`
      sorts those into ``wa_unknown``, which is priced at the marketing rate
      and is NOT a third price band — it is the bucket for «nobody ever
      measured this». Writing MARKETING onto them would move them into
      ``wa_marketing`` at exactly the same price while destroying the one
      signal that says a template nobody recognises is being sent.
    * a row that already carries a category, whoever wrote it.

    Runs as the owner across every tenant (the nightly's session), like the
    lifecycle sweep's own listing: this is one estate-wide accounting statement
    per template, not per-customer work, and it writes nothing a tenant can
    see differently.
    """
    cutoff = now - settles_after
    frozen = 0
    # One UPDATE per template name — the band is per NAME, so this is the whole
    # of the work in nine statements that load no rows, and the sorted() keeps
    # the emitted SQL stable the way `salla.renewal` sorts before `.in_()`.
    for name in sorted(REGISTRY):
        result = session.execute(
            update(DeliveryMessage)
            .where(
                DeliveryMessage.kind == "template",
                DeliveryMessage.template_name == name,
                DeliveryMessage.category.is_(None),
                DeliveryMessage.created_at < cutoff,
            )
            .values(category=str(billed_category(name)))
            .execution_options(synchronize_session=False),
        )
        frozen += int(result.rowcount or 0)  # type: ignore[attr-defined]
    if frozen:
        logger.info(
            "froze the billed category of %d template sends Meta never "
            "priced for us — the bill for those days stops moving",
            frozen,
        )
    return frozen


@dataclass(frozen=True)
class ResendResult:
    """What the resend did — never just «a Delivery or None».

    The caller has to tell the operator the truth, and «there is nothing to
    resend» and «we refuse to try because the window is shut» are different
    truths with different next steps. ``delivery`` is the bundle we looked at
    (present even on a refusal, so the caller can report on it).
    """

    outcome: str
    delivery: Delivery | None = None

    @property
    def status(self) -> str | None:
        return None if self.delivery is None else str(self.delivery.status)

    @property
    def delivered_groups(self) -> list[str]:
        if self.delivery is None:
            return []
        results = self.delivery.bundle.get("results") or {}
        return [str(g) for g in (results.get("delivered") or [])]


def record_out(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    # None only where there is genuinely no channel to point at: the two
    # lifecycle templates that go to the phone on the Salla order fire before
    # the buyer has ever replied, so no CustomerChannel exists yet (0028). Every
    # send from THIS module has a channel and always passes one.
    channel_id: uuid.UUID | None,
    kind: str,
    wa_message_id: str,
    template_name: str | None = None,
    delivery_id: uuid.UUID | None = None,
    now: datetime | None = None,
    status: str = "sent",
) -> DeliveryMessage:
    """The one door onto `delivery_messages`, and the one place the provider's
    id is checked before it enters the session.

    ``wa_message_id`` is annotated ``str`` because every caller in the tree has
    one to hand — but the annotation is a statement about CALLERS, not a
    promise about the value: what they hand over came out of somebody else's
    HTTP response and was never inspected on the way. :func:`usable_message_id`
    therefore assumes nothing about it, and the column is written NULL when the
    database would refuse it. Read that function before changing this: the NULL
    has a price, and it is written down there.

    Of the row's five text columns this is the only one that is not ours.
    ``kind`` and ``status`` are module literals, ``template_name`` comes from
    our own approved `TemplateSpec` — none of them can be widened by a provider
    changing its mind. One value from outside, one check.

    NO SAVEPOINT HERE, deliberately, and this is a separate decision from the
    one above rather than the other half of it:

    * `salla/lifecycle._record_send` wraps its row in ``begin_nested()``
      because its transaction carries OTHER work — a whole night of
      subscription marks for customers already processed — and one refused
      accounting row must not discard them.
    * Here the transaction carries the delivery this row is the evidence FOR.
      A failure that validation cannot prevent (a channel deleted underneath
      us, a tenant that no longer exists) means we cannot record what we sent,
      and constant 3 says a delivery we cannot evidence is not a delivery we
      may claim. Swallowing it would commit a `deliveries` row saying
      COMPLETED with no messages under it, and bill for sends nothing shows.
      Failing the transaction refuses to claim it — loudly, where the caller
      can see it.
    * And it would not be free. A savepoint is only meaningful with a flush
      inside it, which turns one batched INSERT per commit into a
      SAVEPOINT/INSERT/RELEASE round trip PER LEDGER ROW on the hot delivery
      path — a grouped bundle writes one row per job card, one per document
      and one per outcome prompt.

    The failure this module actually suffered was not «a row failed», it was
    «a row failed AVOIDABLY, from a value nobody checked». That is what the
    check above removes; wrapping every row would have hidden the rest.

    ``category`` IS NOT WRITTEN HERE, and that is a decision rather than an
    omission — :func:`freeze_unmeasured_categories` is the other half and the
    argument for where it lives is written out there. In one sentence: this
    function knows only what we BELIEVE the send will be billed at, Meta's
    receipt carries what it WAS billed at, and the handler that records the
    receipt (`worker._handle_status`) fills the column only while it is still
    NULL — so a belief stamped on the row at this moment would be the value
    that arrives first and would shut the measurement out for good.

    ── WHAT IS DELIBERATELY NOT LEDGERED, AND WHY ──────────────────────────
    CHANGELOG 39: «ما لا يُقيَّد أصلًا يُكتب أنه لا يُقيَّد». This paragraph is
    the DENOMINATOR — the list a gap-detector has to subtract before it can
    call a missing row a hole. Verified against the tree on 2026-08-10: fifty
    calls reach a WhatsApp client, twenty-three of them record a row here, and
    the twenty-seven that do not expand to THIRTY-EIGHT distinct messages a
    customer can receive. Counted where the MESSAGE is chosen rather than
    where the client is called — `worker._reply` is one call to the client and
    twelve different messages, and a detector comparing receipts to rows sees
    the twelve.

    * `worker._reply`, TWELVE call sites — the conversational acks (stop and
      resume confirmations, the support ack, the way-back note for an
      opted-out customer, the media and document acks, the thanks). Free-form
      service replies inside an open 24h window: Meta bills nothing for them,
      so a row would carry no money and no receipt anyone reads.
    * `worker`'s `_UNRECOGNIZED` reply to an unknown number — ONE. There is no
      tenant yet, and `delivery_messages.tenant_id` is NOT NULL, so this row
      is not merely unwritten but impossible.
    * the whole of `funnel/flow.py`, FIFTEEN sends. `record_out` has never
      appeared in that file: the free analysis funnel is a conversation, not a
      delivery, and none of it is billed.
    * `whatsapp/samples.py`, THREE — sent to a prospect before any order
      exists. Same NOT NULL wall as the unrecognised reply.
    * the activation refusals in `whatsapp/activation_flow`, FOUR (`_INVALID`,
      `_CONFLICT`, `_EXPIRED`, `_USED`). The successful activation right below
      them IS ledgered; a refusal has no tenant to attribute.
    * `onboarding/orchestrator`'s `_DELETE_DONE`, ONE — its channel row was
      just deleted, so the FK it would need is gone by construction (that one
      already says so at its call site).
    * `salla/lifecycle._send_renew_link`, ONE, and `salla/provisioning`'s
      renewal confirmation, ONE. Both are free-form texts that ride an already
      open window beside a template that IS ledgered.

    THE THIRD CATEGORY, which is not a hole either: a send that RAISED writes
    no row, because every caller here records only after the client returns.
    That is correct and must stay — a ledger row for a refused send is a bill
    Meta never charged us, and `close.whatsapp_spend` would price it.

    So the only real hole is the FIRST kind: a row lost AFTER a successful
    send. That is what :func:`durability_point` exists for.
    """
    dm = DeliveryMessage(
        id=uuid.uuid4(), tenant_id=tenant_id, channel_id=channel_id,
        delivery_id=delivery_id,
        wa_message_id=usable_message_id(wa_message_id, what=template_name or kind),
        kind=kind, template_name=template_name, status=status,
        status_updated_at=now,
    )
    session.add(dm)
    return dm


class LedgerLost(RuntimeError):
    """A durability point could not commit what the customer already received.

    Raised INSTEAD of returning, because the object the caller holds is a lie
    after the rollback: `deliveries` reverts to the status it carried before
    the send (NO_SEND on the direct path), so a caller that read it would
    conclude nothing was sent and close the day SKIPPED_OPTED_OUT — the exact
    manufactured number CHANGELOG 39 is about. It carries the groups that
    LANDED so the day can still close `LEDGER_FAILED` with real counts.
    """

    def __init__(self, *, delivered: list[str], failed: list[str],
                 landed: bool = True) -> None:
        super().__init__("delivery ledger lost after a successful send")
        self.delivered = delivered
        self.failed = failed
        #: :func:`anything_landed`, read BEFORE the rollback could destroy it.
        #: Defaulted rather than required, and defaulted to the conservative
        #: side: a raiser that does not answer is taken to mean the customer
        #: received something, so the day says LEDGER_FAILED. The opposite
        #: default would let a real delivery be closed as a send failure, and
        #: under-claiming a delivery the customer has read is the error this
        #: whole class exists to stop. Pass :func:`anything_landed` explicitly
        #: where the delivery object is to hand.
        self.landed = landed


def anything_landed(delivery: Delivery) -> bool:
    """Did ANY message of this delivery actually reach the customer?

    The one question the honest day state hinges on after a crash, and it is
    not «is the delivered list non-empty» — that list only exists for grouped
    C7 bundles. A generic parts bundle sends text and document with no groups
    at all, and a held bundle sends a real morning template while delivering
    no job; both landed. A grouped bundle that failed EVERY job is the
    opposite case: terminal, and nothing reached anybody.

    So: a delivered group is proof, and otherwise the status is — COMPLETED
    and PENDING both mean a message left the building, PARTIAL with nothing
    delivered means none did, and NO_SEND never sent.
    """
    results = delivery.bundle.get("results") or {}
    if results.get("delivered"):
        return True
    return str(delivery.status) in (DELIVERY_COMPLETED, DELIVERY_PENDING)


def durability_point(session: Session, *, what: str) -> bool:
    """Commit what the customer has ALREADY received, before the first thing
    that can raise. ``True`` when the ledger is safe; ``False`` when it was
    lost anyway (the session is rolled back and usable either way).

    ── THE DIRECTION CONSTANT 3 DOES NOT NAME ──────────────────────────────
    «لا كتابة في السجلّ إلا بعد اكتمال التسليم، والكتابة ذرية» names one
    direction — never write EARLY — and is silent about the other: never LOSE
    the write afterwards. On 2026-08-10 a live nightly sent four messages to a
    real customer (two texts, a document, an interactive card, all four
    acknowledged by Meta), then the `delivery_messages` INSERT hit a column
    the host database did not have; the per-tenant `except` rolled back, and
    all four ledger rows and the `deliveries` row went with it. A rollback is
    whole — but a send is not undoable, so «roll back the tenant» silently
    means «keep the half the customer received, discard the half that records
    it».

    Every send happens OUTSIDE any transaction we control. So the transaction
    boundary has to be placed where it matches that fact: after the last send,
    before the first work that can raise.

    ── TWO ALTERNATIVES, BOTH MEASURED AND BOTH REJECTED ───────────────────
    * **A savepoint around the ledger rows.** It does nothing here, and this
      was proved on `career_test` rather than argued: ``RELEASE SAVEPOINT`` is
      not a commit — it merges the nested work into the enclosing transaction,
      which the outer ``rollback()`` then discards exactly as before. A
      savepoint protects the OUTER work from an inner failure (which is why
      `salla/lifecycle._record_send` uses one, and why `close.close_tenant_day`
      wraps the suppression loop); it cannot protect inner work from an outer
      rollback. Nothing short of a commit makes a row survive one.
    * **A second, autonomous session for the ledger rows.** Rejected on the
      schema: `delivery_messages.delivery_id` is an FK to `deliveries(id)`,
      and on the send path that parent row lives in the CALLER's still-open
      transaction. A second connection would block on it until the caller
      ends, then fail its INSERT the moment the caller rolls back — a
      guaranteed lock wait followed by the same lost row.

    ── THE PRICE, SAID OUT LOUD ────────────────────────────────────────────
    After this commits, the caller's per-tenant ``except`` CAN NO LONGER UNDO
    the tenant. That is not a side effect to be minimised; it is the point.
    The half it would undo is the half the customer already received, and an
    orchestrator that can erase it is an orchestrator that reports zero for a
    CV a human has read. What the ``except`` keeps is the ability to roll back
    everything written AFTER this line, which is the only part nobody has seen.

    On failure the shape is `telegram/console._run_reply`'s, deliberately: the
    message IS with the customer and only our record failed, so the log says
    exactly which half succeeded rather than «failed», and it is ERROR because
    the operator's harvester forwards ERROR lines only. ``what`` is a phrase,
    never an id — §15.13 keeps PII and raw identifiers out of the logs.
    """
    try:
        session.commit()
        return True
    except Exception:  # noqa: BLE001 — the send already happened
        logger.error(
            "%s landed but was not recorded — the customer has the messages "
            "and the ledger does not; the day closes LEDGER_FAILED with the "
            "counts that actually reached them",
            what, exc_info=True,
        )
        session.rollback()
        return False


def _send_bundle_parts(
    session: Session, channel: CustomerChannel, delivery: Delivery,
    *, whatsapp_client: WhatsAppClient, now: datetime,
) -> None:
    for part in delivery.bundle.get("parts", []):
        if part.get("kind") == "text":
            mid = whatsapp_client.send_text(channel.phone_e164, part["body"])
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="text", wa_message_id=mid, delivery_id=delivery.id, now=now)
        elif part.get("kind") == "document":
            mid = whatsapp_client.send_document(
                channel.phone_e164, part["ref"],
                filename=part.get("filename", "cv.pdf"), caption=part.get("caption", ""),
            )
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="document", wa_message_id=mid, delivery_id=delivery.id, now=now)


def _send_grouped_bundle(
    session: Session, channel: CustomerChannel, delivery: Delivery,
    *, whatsapp_client: WhatsAppClient, now: datetime,
) -> tuple[list[str], list[str]]:
    """C7 job bundles (§08): header, then per job its card followed by ITS
    document. A failing card skips its OWN document only; a sent card whose
    document fails marks the JOB failed (a missing CV is never success);
    one job's failure never aborts its siblings. Returns
    (delivered_groups, failed_groups)."""
    delivered: list[str] = []
    failed: list[str] = []

    header = delivery.bundle.get("header")
    if header:
        try:
            mid = whatsapp_client.send_text(channel.phone_e164, header)
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="text", wa_message_id=mid, delivery_id=delivery.id,
                       now=now)
        except Exception as exc:  # noqa: BLE001 — header failure ≠ job failures
            logger.error("bundle header send failed: %s", exc, exc_info=True)

    for entry in delivery.bundle.get("jobs", []):
        group = str(entry.get("group", ""))
        card = entry.get("card") or {}
        document = entry.get("document") or {}
        try:
            mid = whatsapp_client.send_text(channel.phone_e164, card["body"])
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="text", wa_message_id=mid, delivery_id=delivery.id,
                       now=now)
        except Exception as exc:  # noqa: BLE001 — card failed → document not attempted
            logger.error("job card send failed: %s", exc, exc_info=True)
            failed.append(group)
            continue
        try:
            mid = whatsapp_client.send_document(
                channel.phone_e164, document["ref"],
                filename=document.get("filename", "cv.pdf"),
                caption=document.get("caption", ""),
            )
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="document", wa_message_id=mid, delivery_id=delivery.id,
                       now=now)
        except Exception as exc:  # noqa: BLE001 — card without its CV = job FAILED
            logger.error("job document send failed: %s", exc, exc_info=True)
            failed.append(group)
            continue
        outcome = entry.get("outcome") or {}
        if outcome.get("buttons"):
            # §14 measurement fuel only — the delivery contract (§08) is
            # card+document, so a failed buttons message never fails the job.
            try:
                mid = whatsapp_client.send_interactive(
                    channel.phone_e164, outcome.get("body", ""),
                    [tuple(b) for b in outcome["buttons"]],
                )
                record_out(session, tenant_id=channel.tenant_id,
                           channel_id=channel.id, kind="interactive",
                           wa_message_id=mid, delivery_id=delivery.id, now=now)
            except Exception:  # noqa: BLE001
                logger.warning("outcome buttons send failed", exc_info=True)
        delivered.append(group)
    return delivered, failed


def _dispatch_bundle(
    session: Session, channel: CustomerChannel, delivery: Delivery,
    *, whatsapp_client: WhatsAppClient, now: datetime, retry_ok: bool = False,
) -> None:
    """Send the held bundle and set the HONEST final status.

    A tap that delivers NOTHING no longer consumes the bundle. The old code
    stamped COMPLETED/PARTIAL unconditionally after the send loop, so when
    every document failed the delivery left PENDING_WINDOW anyway — and both
    the descend query and the stale sweep filter on PENDING_WINDOW, which
    made the failure terminal and unreachable. The customer's one chance was
    spent on an attempt that sent them nothing (live: 27 July, zero delivered
    and no trace). Now a total failure stays claimable, up to
    MAX_DISPATCH_ATTEMPTS, so the next inbound — or the operator's resend —
    tries again.

    ``retry_ok`` is True only for a HELD bundle (the descend and resend
    paths). On the open-window direct path there was no template and no tap
    to protect, and the day must still close with an honest state (§15.12),
    so a total failure stays terminal exactly as before.
    """
    attempts = int(delivery.bundle.get("attempts", 0)) + 1
    if delivery.bundle.get("grouped"):
        delivered, failed = _send_grouped_bundle(
            session, channel, delivery, whatsapp_client=whatsapp_client, now=now
        )
        delivery.bundle = {
            **delivery.bundle,
            "attempts": attempts,
            "results": {"delivered": delivered, "failed": failed},
        }
        if delivered:
            delivery.status = (
                DELIVERY_COMPLETED if not failed else DELIVERY_PARTIAL
            )
        elif retry_ok and attempts < MAX_DISPATCH_ATTEMPTS:
            logger.error(
                "held bundle delivered nothing on attempt %d — staying "
                "claimable for a retry", attempts,
            )
            delivery.status = DELIVERY_PENDING
            return          # completed_at stays NULL: nothing completed
        else:
            logger.error(
                "held bundle delivered nothing after %d attempts — closing "
                "it honestly", attempts,
            )
            delivery.status = DELIVERY_PARTIAL
    else:
        _send_bundle_parts(
            session, channel, delivery, whatsapp_client=whatsapp_client, now=now
        )
        delivery.bundle = {**delivery.bundle, "attempts": attempts}
        delivery.status = DELIVERY_COMPLETED
    delivery.completed_at = now


def close_day_from_terminal_delivery(
    session: Session, delivery: Delivery, *, now: datetime
) -> None:
    """§15.12 + §15.3: a delivery that reached a TERMINAL status closes its
    tenant's day and writes the suppression for what actually landed.

    AUDIT (constant 12): this used to live only at the two callers that
    happened to remember it (the nightly orchestrator and the inbound descend
    in whatsapp/worker). The operator's resend was a third caller and it
    remembered nothing — so a resend that LANDED left ``tenant_day_states``
    with no row for that run_date at all (permanently: the row is no longer
    PENDING_WINDOW, so neither the descend query nor the stale sweep can ever
    reach it again), wrote no suppression (the same posting passed the gate
    again the next night and the identical cached PDF was re-sent), and
    skipped the day's cost rollup. It now lives INSIDE the dispatch module,
    next to the status it depends on, so no future caller can forget it.
    """
    if delivery.status not in (DELIVERY_COMPLETED, DELIVERY_PARTIAL):
        return
    # local imports: career.cv.daily_run imports this module at module scope
    from career.cv import close as close_mod
    from career.cv.daily_run import close_from_delivery

    closed = close_from_delivery(session, delivery=delivery, now=now)
    if closed is None:  # pragma: no cover — guarded by the status check above
        return
    try:
        close_mod.rollup_costs(
            session, tenant_id=delivery.tenant_id, day=closed.run_date
        )
    except Exception:  # noqa: BLE001 — accounting never blocks the close
        logger.error("resend cost rollup failed", exc_info=True)


def resend_pending_delivery(
    session: Session, *, tenant_id: uuid.UUID,
    whatsapp_client: WhatsAppClient, now: datetime,
) -> ResendResult:
    """The operator's «إعادة إرسال»: re-attempt today's held bundle.

    Until now a delivery that failed after the tap was terminal — there was
    no retry path anywhere, for the customer OR the operator.

    Two audit fixes are load-bearing here:

    * A held bundle is held precisely because the 24h window was CLOSED. A
      free-form re-dispatch into a closed window is rejected by Meta for
      EVERY job, yet the old code counted the attempt anyway — so three taps
      on the most common held-bundle state (waiting for the morning tap) made
      the bundle terminal and unclaimable, and a customer who tapped the
      template later that day received nothing. The window is now checked
      BEFORE the attempt counter moves, and a refusal costs nothing.
    * A resend that lands is a delivery like any other: it closes the day and
      writes suppression (:func:`close_day_from_terminal_delivery`).
    """
    delivery = session.execute(
        select(Delivery)
        .where(Delivery.tenant_id == tenant_id,
               Delivery.status == DELIVERY_PENDING)
        .order_by(Delivery.created_at)
    ).scalars().first()
    if delivery is None:
        return ResendResult(RESEND_NO_BUNDLE)
    channel = session.get(CustomerChannel, delivery.channel_id)
    if channel is None:
        return ResendResult(RESEND_NO_BUNDLE)
    if channel.opt_out_at is not None:
        return ResendResult(RESEND_OPTED_OUT, delivery)
    window = window_state(
        last_inbound_at=channel.last_inbound_at,
        opt_out_at=channel.opt_out_at, now=now,
    )
    if window is not WindowState.OPEN:
        # No send is possible, so no attempt is spent and nothing is stamped:
        # the bundle stays exactly as claimable as it was.
        return ResendResult(RESEND_WINDOW_CLOSED, delivery)
    delivery.opened_at = delivery.opened_at or now
    _dispatch_bundle(session, channel, delivery,
                     whatsapp_client=whatsapp_client, now=now, retry_ok=True)
    close_day_from_terminal_delivery(session, delivery, now=now)
    return ResendResult(RESEND_SENT, delivery)


def deliver_adaptive(
    session: Session, channel: CustomerChannel, bundle: dict[str, Any],
    *, run_date: date, whatsapp_client: WhatsAppClient, daily_template: TemplateSpec,
    now: datetime,
) -> Delivery:
    window = window_state(
        last_inbound_at=channel.last_inbound_at, opt_out_at=channel.opt_out_at, now=now
    )
    action = plan_delivery(window)
    delivery = Delivery(
        id=uuid.uuid4(), tenant_id=channel.tenant_id, channel_id=channel.id,
        run_date=run_date, status=DELIVERY_NO_SEND,
        window_state_at_start=window.value, bundle=bundle,
    )
    session.add(delivery)
    # THE FK PRECONDITION, and the reason this is a commit and not the flush
    # it was until 2026-08-10. `delivery_messages.delivery_id` points HERE, so
    # a ledger row can only outlive a rollback if its parent already has. A
    # flush makes the row visible to this transaction and to nothing else; the
    # durability point below would then be committing the parent and the
    # children together, which is fine — until the parent's INSERT is the
    # statement that fails, at which point the send has happened and there is
    # no row to attach it to. Committing before the first send costs one round
    # trip and removes that ordering entirely.
    #
    # It is safe to commit early precisely because the row is honest at this
    # moment: NO_SEND, nothing claimed. If this commit RAISES, nothing has
    # been sent yet and the caller's ordinary failure path closes the day
    # correctly — that is the one moment where losing the row is the right
    # outcome, so it is deliberately not a durability point.
    session.commit()

    if action is DeliveryAction.SKIP_OPTED_OUT:
        # Nothing leaves the building, so there is nothing to make durable —
        # the caller's own commit carries this row.
        delivery.status = DELIVERY_NO_SEND
        return delivery
    if action is DeliveryAction.SEND_DIRECT:
        _dispatch_bundle(session, channel, delivery,
                         whatsapp_client=whatsapp_client, now=now)
    else:  # SEND_TEMPLATE_THEN_WAIT
        mid = whatsapp_client.send_template(
            channel.phone_e164, daily_template.name, daily_template.language,
            buttons=daily_template.buttons,
        )
        # The SECOND unguarded provider write on this path, and the reason
        # «validate once, at the funnel» was not the whole fix:
        # `deliveries.template_message_id` is `varchar(128)` too, and it is
        # stamped here, outside `record_out`. An id the ledger row survives
        # would still have failed the UPDATE on THIS row and rolled back the
        # held bundle whole — the customer receives a morning template, taps
        # it, and there is no delivery left to descend. Nothing in the tree
        # reads this column (it is a debugging convenience; the ledger row
        # below is the real record), so writing NULL here costs nothing at all.
        delivery.template_message_id = usable_message_id(
            mid, what=daily_template.name
        )
        record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                   kind="template", wa_message_id=mid,
                   template_name=daily_template.name, delivery_id=delivery.id, now=now)
        delivery.status = DELIVERY_PENDING

    # THE DURABILITY POINT. Read while the objects still hold the truth: after
    # a failed commit the rollback reverts them, so the answer to «what landed»
    # has to be taken before the question can be destroyed.
    results = delivery.bundle.get("results") or {}
    delivered = [str(g) for g in (results.get("delivered") or [])]
    unlanded = [str(g) for g in (results.get("failed") or [])]
    landed = anything_landed(delivery)
    if not durability_point(session, what="today's delivery"):
        raise LedgerLost(delivered=delivered, failed=unlanded, landed=landed)
    return delivery


def descend_pending_delivery(
    session: Session, channel: CustomerChannel,
    *, whatsapp_client: WhatsAppClient, now: datetime,
) -> Delivery | None:
    """When the customer opens the window, send the held bundle."""
    delivery = session.execute(
        select(Delivery)
        .where(Delivery.channel_id == channel.id, Delivery.status == DELIVERY_PENDING)
        .order_by(Delivery.created_at)
    ).scalars().first()
    if delivery is None:
        return None
    delivery.opened_at = now
    _dispatch_bundle(session, channel, delivery,
                     whatsapp_client=whatsapp_client, now=now, retry_ok=True)
    return delivery
