"""Single webhook-intake authority (LEGACY §13: one module owns each cross-cutting
truth). Both Salla and WhatsApp dedupe-persist their raw events through here, so
the idempotency semantics can never diverge between providers.

Dedupe is by ``event_fingerprint`` (unique). Returns the new row id on a fresh
event, or None if it was already seen. Commits so the intake is durable before
the endpoint returns 200.

Since 0024 this module owns two more things about the same rows, for the same
reason — there is one place they can be true.

**Who the body is about.** The payload is the provider's, verbatim, and it
carries the buyer's name, mobile and email (Salla) or the customer's phone,
profile name and typed text (Meta). Until 0024 nothing connected any of that
to a tenant, so a customer's «حذف بياناتي» deleted their profile, their
messages and their channels and left the raw bodies that contained the same
sentences sitting beside them. The link is resolved HERE, at intake, and not
at processing: a row is personal from the millisecond it lands, and the rows
that are never processed — poisoned, deferred, quarantined — are precisely the
ones that live longest. Resolution that fails is not an error; it writes NULL
and the nightly tries again.

**How long the body lives.** Thirty days, then the payload is replaced by a
PII-free skeleton. The row itself is never deleted, because it is also the
idempotency record: `event_fingerprint` is the whole of what makes a replayed
Salla webhook a no-op, and an order whose fingerprint we had thrown away would
provision the same customer a second time.

One exception, added 2026-08-07 and bounded by its own clock: an
`app.store.authorize` body still holds the merchant credential Salla delivers
exactly once, and when the credential writer refuses it the operator is
promised, in the refusal itself, that it is still there to consume. So that one
body is kept while the credential in it can still be used — never longer, never
past the longest life Salla has ever issued whatever the payload claims, and
never for a body that carries a customer. See `_AUTHORIZE_EVENT`.

Every row here was signature-verified before it was written, and that is now a
property of this module rather than of its callers: `persist_deduped_event`
takes the verdict as an argument it cannot default and refuses anything else.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent
from career.db.session import tenant_for_phone, tenant_for_salla_order

logger = logging.getLogger("career.webhooks")

#: How long a raw provider body is kept before it is redacted.
#:
#: The body is scaffolding, not the record. Everything the product actually
#: runs on has already been parsed out of it into rows that carry their own
#: tenant and their own retention — inbound_messages, subscriptions,
#: subscription_events. What the raw body is still good for after processing is
#: forensics, and thirty days is drawn from the longest of the three things
#: that actually reach back for one:
#:
#: * the retry budget (0022) exhausts in hours, and `replay_lost_events.py`
#:   refuses to replay anything outbound older than two days by default;
#: * the operator's loop is the weekly report, so «what happened last week»
#:   has to be answerable, and «what happened the week before» usually too;
#: * a customer noticing that a delivery never arrived does so within their
#:   delivery cycle, which is the month they paid for.
#:
#: Ninety days — the §12 figure — was rejected: that promise is about the
#: customer's own data for as long as they are a customer, and stretching it
#: over the provider's raw body would keep the most sensitive copy the longest
#: for the least reason. Seven was rejected because a multi-day outage has
#: already happened once and `replay_lost_events` was how it was recovered.
#:
#: This bounds the LIVE window. The nightly backup is a separate control
#: (constant 14) and redaction cannot reach backwards into one: what changes
#: there is that a body is carried in backups taken during a thirty-day window
#: instead of in every backup ever taken.
RAW_PAYLOAD_RETENTION_DAYS = 30

#: The nightly pass is bounded so that a backlog cannot turn into one enormous
#: transaction holding row locks across the intake path. Whatever it does not
#: reach tonight it reaches tomorrow; the window is thirty days wide and the
#: table takes about three hundred rows a month.
_PRUNE_BATCH = 5_000

#: The one event whose body is not scaffolding.
#:
#: Salla's «Easy Mode» never shows the merchant access token to anyone: it is
#: delivered exactly once, inside this signed webhook (CHANGELOG §29). When the
#: credential writer REFUSES that delivery — it is for another store, or the
#: store on disk was never recorded — `career.salla.tokens.ForeignStoreCredential`
#: leaves the row `received` and tells the operator, in as many words, that the
#: credential «is still in the webhook_events row … so the operator can consume
#: it deliberately the moment they have decided»
#: (`scripts/consume_stored_authorize.py` is how they consume it).
#:
#: Until 2026-08-07 the sweep below redacted that body on the ordinary
#: thirty-day clock, whatever the row's `processing_status`. The row survived
#: and the credential did not: the refusal destroyed the thing it promised to
#: keep, and the consume script silently stopped working.
#:
#: THE RULE, AND WHY IT IS THIS ONE. An unconsumed authorize body keeps its
#: payload while the credential in it can still be used, and not one day
#: longer. It is bounded by the credential's OWN life AND by
#: `_AUTHORIZE_MAX_LIFE_DAYS`, so it cannot become an indefinite secret store —
#: which matters, because §29's whole argument for keeping the credential in a
#: 0600 file rather than a table was that a row widens the leak surface from
#: «root on this host» to «anybody who can read a row». The second bound is
#: what makes that argument hold: without it «the credential's own life» is
#: whatever `data.expires` says, and an indefinite secret store is exactly what
#: a payload claiming the year 2100 would have produced.
#:
#: Rejected: exempting every unprocessed authorize row forever, which is that
#: same widened surface with no end date. Rejected: «redact everything except
#: the credential», which keeps the secret and throws away the harmless
#: context — precisely backwards. Rejected: shortening the window for these
#: rows, which does not keep the promise at all.
#:
#: NOTHING HERE WEAKENS §25. The retention window exists because these bodies
#: carry the buyer's name, mobile and email; an `app.store.authorize` body
#: carries a credential and the app's own metadata (app id, name, scope) and no
#: customer field of any kind. The exemption is keyed on the event type, so it
#: can never reach a body that has a customer in it — and `redact_for_tenant`,
#: the deletion path, is untouched: it erases on request, immediately, and an
#: authorize row could only be reached by it if it were ever linked to a
#: tenant, which it is not (it is about a store, not a buyer).
_AUTHORIZE_EVENT = "app.store.authorize"

#: The longest an authorize body may be considered consumable, counted from
#: the moment it LANDED and not from anything the payload says.
#:
#: Fourteen days is the longest access-token life Salla documents
#: (`expires_in: 1209599` on the Authorization page; other Salla material says
#: one week, and `scripts/refresh_salla_token.py` takes the shorter figure
#: where it has to guess). Past it the credential cannot be live under any
#: lifetime Salla has ever published, so the body goes.
#:
#: IT IS BOTH THE FALLBACK AND THE CEILING, and the second half is the
#: 2026-08-07 fix. The fallback was always this: a renamed or absent
#: `data.expires` — the same shape that makes `store_credentials` record no
#: expiry at all — has to be bounded by something, and «unknown» must not mean
#: «forever». But the branch that COULD read `data.expires` was bounded by
#: nothing: the deferral asked «is now before the expiry in the body?», and the
#: body is Salla's. A payload carrying `expires: 4102444800` — the year 2100 —
#: was therefore consumable for seventy-four years and never redacted, which is
#: a §12/§25 retention promise whose deadline is a number somebody else sends
#: us. Signed today, so not attacker-reachable; unbounded all the same, and the
#: docstring claimed the credential's own expiry «is what bounds this».
#:
#: A ceiling can only ever SHORTEN the life of a raw body, so §12/§25 cannot be
#: weakened by it — `RAW_PAYLOAD_RETENTION_DAYS` is untouched, `_redact` is
#: untouched, and `redact_for_tenant` (the on-request deletion path) never
#: consulted any of this in the first place.
#:
#: WHAT IT CHANGES IN PRACTICE, said plainly rather than discovered later:
#: fourteen days is INSIDE the thirty-day retention window, so a real Salla
#: authorize body — whose access token is dead by day fourteen at the latest —
#: can no longer defer anything at all. The sweep first looks at the row on day
#: thirty and by then the ceiling has passed. The deferral is not dead code: it
#: is the thing that keeps the §29 promise if `RAW_PAYLOAD_RETENTION_DAYS` is
#: ever shortened below this figure, and it is what makes «kept while it can be
#: used, and not one day longer» true at BOTH ends instead of one. The honest
#: reading of the pre-fix behaviour is that the only bodies the exemption ever
#: actually saved were the ones whose payload claimed a life Salla does not
#: issue.
_AUTHORIZE_MAX_LIFE_DAYS = 14


def _wa_identifiers(payload: dict[str, Any]) -> list[str]:
    """Every phone number a Meta batch names, in the order Meta wrote them.

    `messages[].from`, `statuses[].recipient_id` and `contacts[].wa_id` are the
    three places a number appears; the worker reads the first two to route and
    the third is what carries the profile name. Meta delivers all of them
    WITHOUT the leading «+» while `customer_channels.phone_e164` may hold
    either spelling (career.whatsapp.phones carries that live bug), so the
    caller tries every variant rather than trusting one.
    """
    found: list[str] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                found.append(message.get("from"))
            for status in value.get("statuses") or []:
                found.append(status.get("recipient_id"))
            for contact in value.get("contacts") or []:
                found.append(contact.get("wa_id"))
    return [f for f in found if isinstance(f, str) and f]


def _whatsapp_subjects(session: Session, payload: dict[str, Any]) -> set[str]:
    from career.whatsapp.phones import phone_variants

    subjects: set[str] = set()
    for raw in _wa_identifiers(payload):
        for variant in phone_variants(raw):
            tenant_id = tenant_for_phone(session, variant)
            if tenant_id is not None:
                subjects.add(tenant_id)
                break
    return subjects


def resolve_subject_tenant(
    session: Session,
    *,
    provider: str,
    payload: dict[str, Any],
    order_id: str | None = None,
) -> str | None:
    """Which tenant this body is about, or None when that has no single answer.

    A Salla order id resolves through the subscription that was created from
    it; a WhatsApp phone resolves through the channel it belongs to. Both go
    through the `app.*` sweep functions rather than a query of our own, because
    those functions are the sanctioned way to learn a tenant id without already
    having one (0021) and they return an id, never a row.

    **Exactly one, or nothing.** A single Meta POST can batch messages from two
    different customers — the worker already loops over `entry[].changes[]` for
    that reason — and stamping such a row with one of them would be wrong in
    both directions at once: that customer's deletion request would erase the
    other customer's forensic record, and their data export would be pointed at
    a body containing somebody else's message. Ambiguity stays NULL and is
    handled by the clock instead of by the link.

    Never raises. Resolution runs inside a SAVEPOINT so that a failure — a
    malformed payload, or a database still at a migration where the `app.*`
    functions do not exist yet — rolls back only itself and leaves the
    surrounding transaction able to do the one thing that actually matters
    here, which is to record the event before the endpoint returns 200.
    """
    try:
        with session.begin_nested():
            if provider == "salla":
                return tenant_for_salla_order(session, order_id) if order_id else None
            if provider == "whatsapp":
                subjects = _whatsapp_subjects(session, payload)
                return subjects.pop() if len(subjects) == 1 else None
            return None
    except Exception:  # noqa: BLE001 — the 200 is worth more than the link
        # No payload, no phone, no order id in the line: this logger is not
        # exempt from §15.13 just because it is reporting a failure.
        logger.warning(
            "webhook intake: could not resolve the subject tenant (provider=%s)",
            provider,
            exc_info=True,
        )
        return None


class UnverifiedWebhook(Exception):
    """A body was offered to the intake without a verified signature.

    Never raised by the two intakes that exist — both answer their provider
    before they reach here. It exists so that the THIRD one, written by
    somebody who has not read this module, cannot get a row into
    `webhook_events` by forgetting a step. See
    :func:`persist_deduped_event`.
    """


def persist_deduped_event(
    session: Session,
    *,
    provider: str,
    event_type: str,
    fingerprint: str,
    payload: dict[str, Any],
    signature_valid: bool,
    order_id: str | None = None,
) -> str | None:
    """Record one provider body, once. Returns the new row id, or None if the
    fingerprint was already seen.

    **``signature_valid`` is required, and it is required to be true.**

    Until 2026-08-07 this function wrote the literal ``signature_valid=True``
    into every row it inserted. The column is named as a verdict and was in
    fact a constant: what made it mean anything was that BOTH callers happened
    to verify first and return early — `salla.webhook.receive_webhook` and
    `whatsapp.webhook.receive_whatsapp_webhook` still do, and they still
    should, because a forged request must be refused before it can drive an
    insert at all. That is call-site discipline, and call-site discipline is
    inherited by nobody: a third intake — a Salla app-event endpoint, a
    payment provider, whatever ships next — would have got ``True`` for free
    simply by calling the one function that exists for this.
    It stopped being a documentation problem when the column acquired a
    reader that acts on it. `salla.provisioning._verified` consults exactly
    this field before it will let a payload deliver a LIVE MERCHANT
    CREDENTIAL to the credential store (§29), and
    `credential_is_still_consumable` consults it again before it will keep
    that body past the retention window. A column that is true by
    construction is fine; a column that is true by assumption while two
    security decisions read it as a fact is not.

    So the verdict is now an argument with no default. Omitting it is a
    ``TypeError`` at the call, not a silent ``True`` in the database, and
    anything other than a literal ``True`` is refused with
    :class:`UnverifiedWebhook` rather than stored — which is what makes «every
    row in this table was signature-verified» an invariant the two readers
    above may rely on, instead of a habit.

    REJECTED: verifying INSIDE this function (taking ``raw_body``, the header
    and the secret, and running the provider's HMAC here). It is the stronger
    construction — a caller could then not lie, only omit — and it was still
    the wrong trade. Both endpoints must answer «invalid signature» BEFORE
    they parse a body or compute a fingerprint, so the check has to happen at
    the call site regardless; moving a second copy in here would verify every
    body twice, make this module the owner of every provider's signing
    scheme, and force each of them to hand the raw bytes and the shared secret
    across one more boundary for no property the early return does not already
    have. What was actually missing was that the ANSWER never travelled with
    the body. Now it does.
    """
    if signature_valid is not True:
        # No payload, no fingerprint, no header in the line (§15.13): this is
        # about a caller's contract, and the body it is refusing is by
        # definition one we could not authenticate.
        raise UnverifiedWebhook(
            f"refusing to record an unverified {provider} webhook "
            f"({event_type}) — verify the signature before persisting"
        )
    subject_tenant_id = resolve_subject_tenant(
        session, provider=provider, payload=payload, order_id=order_id
    )
    stmt = (
        pg_insert(WebhookEvent)
        .values(
            provider=provider,
            event_type=event_type,
            event_fingerprint=fingerprint,
            signature_valid=signature_valid,
            salla_order_id=order_id,
            subject_tenant_id=subject_tenant_id,
            payload=payload,
            processing_status="received",
        )
        .on_conflict_do_nothing(constraint="uq_webhook_events_event_fingerprint")
        .returning(WebhookEvent.id)
    )
    row = session.execute(stmt).first()
    session.commit()
    return str(row[0]) if row is not None else None


# ── redaction ────────────────────────────────────────────────────────────────


def redacted_payload(payload: Any, *, now: datetime) -> dict[str, Any]:
    """What replaces the body: the shape it had, and nothing it contained.

    The top-level keys are kept because they are the difference between «a
    Meta status batch» and «a message batch», or between a Salla order event
    and an install hook, and that is the question a forensic reader asks
    first. Neither provider puts anything personal at the top level — Meta has
    `object`/`entry`, Salla has `event`/`merchant`/`data` — and nothing below
    it survives.
    """
    shape = sorted(payload) if isinstance(payload, dict) else []
    return {"_redacted": True, "redacted_at": now.isoformat(), "shape": shape}


def _as_utc(value: datetime) -> datetime:
    """Compare like with like. `received_at` comes back aware from Postgres,
    and a caller may hand us a naive `now`; a naive value here is UTC by the
    same convention the rest of this system writes with."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def credential_is_still_consumable(
    event: WebhookEvent, *, now: datetime
) -> bool:
    """Does this row still hold a credential the operator could act on?

    Every clause is a way for the answer to be no, and each one is doing work:

    * the provider and event type — `webhook_events` is one shared table and a
      WhatsApp row wearing this event name must not buy itself an exemption
      from the retention clock;
    * the signature — an unsigned body is an attempted credential injection
      (§29's first condition), never something we keep for longer;
    * `received` — any other status means the decision was already taken. A
      processed authorize was consumed; a failed one was refused terminally.
      Only the deferred row is waiting on a human;
    * an access token actually in the body — a payload with none is nothing to
      preserve, and it is already `_redacted` if a previous sweep ran;
    * and the credential's expiry, bounded by :data:`_AUTHORIZE_MAX_LIFE_DAYS`.
      Past it there is nothing left to consume, so the promise is over and the
      ordinary rule applies. What the payload claims is an input to that
      answer, never the whole of it — see the constant.

    ABOUT THE `received` CLAUSE, because the next reader will ask (raised in
    review, 2026-08-07). `scripts/consume_stored_authorize.py --event-id`
    exists precisely to rescue a row that is `ignored` or `failed`, and this
    clause drops both, so the sweep redacts on day thirty a body that the
    operator could in principle still have named. That is deliberate and it
    costs nothing, for a reason that is now true of EVERY status rather than
    two of them: the ceiling above is fourteen days and the retention window is
    thirty, so by the time any authorize body is redacted its access token has
    been dead for a fortnight. No status buys a row extra time any more, and
    none needs to.

    The reviewer's own version of that reasoning — «a Salla token dies before
    the sweep can reach it» — is right about the outcome and incomplete about
    the reason, so the whole of it is written here. The body also carries a
    REFRESH token, and `scripts/refresh_salla_token.py` documents that Salla
    publishes no lifetime for one: it is single-use, invalidated by the next
    exchange, and otherwise undated. So a thirty-day-old body may well still
    hold something usable. We redact it anyway, ON PURPOSE — a secret whose
    retention is bounded by a value nobody documents is not bounded, and §29's
    entire argument for keeping the credential in a 0600 file rather than a
    table was that a row widens the leak surface from «root on this host» to
    «anybody who can read a row». The operator's recovery is the one §29
    already gives him and the one the sweep's own ERROR line names: reinstall
    the app, which re-fires `app.store.authorize`.
    """
    if event.provider != "salla" or event.event_type != _AUTHORIZE_EVENT:
        return False
    if event.processing_status != "received" or not event.signature_valid:
        return False
    payload = event.payload if isinstance(event.payload, dict) else {}
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("access_token"):
        return False
    return _as_utc(now) < _authorize_usable_until(event, data)


def _authorize_usable_until(event: WebhookEvent, data: dict[str, Any]) -> datetime:
    """When the credential in this body stops being usable — the EARLIER of
    what the payload claims and what Salla could possibly have issued.

    `data.expires` is an absolute unix epoch (live-verified on the 2026-07-15
    delivery) and the parser is the credential module's own, so this can never
    drift from what the writer would record if the operator did consume the
    row. But it is a number a third party sends us, and until 2026-08-07 it
    was the whole answer: a body claiming a year-2100 expiry deferred its own
    redaction for seventy-four years.

    The ceiling is measured from `received_at`, which is OURS — the moment the
    row landed, written by the database. So the answer is now bounded at both
    ends by facts we own, and the payload can only ever make it SOONER.
    Rejected: capping at `RAW_PAYLOAD_RETENTION_DAYS`, which would bound the
    body by the very window the deferral exists to step outside of and would
    make the whole clause a no-op by construction rather than by arithmetic;
    rejected: the refresh path's seven-day conservative guess, which is the
    right number when you are deciding to RENEW a credential early and the
    wrong one for deciding to DESTROY the last copy of it — a ceiling must be
    the longest life Salla has ever published, not the shortest.
    """
    ceiling = _as_utc(event.received_at) + timedelta(
        days=_AUTHORIZE_MAX_LIFE_DAYS
    )
    expiry: datetime | None = None
    try:
        from career.salla.tokens import parse_expiry

        expiry = parse_expiry(data.get("expires"))
    except Exception:  # noqa: BLE001 — an unreadable expiry is «unknown», not a crash
        logger.debug("could not read the expiry of a stored authorize payload",
                     exc_info=True)
    if expiry is not None:
        return min(_as_utc(expiry), ceiling)
    return ceiling


def _redact(event: WebhookEvent, *, now: datetime) -> None:
    event.payload = redacted_payload(event.payload, now=now)
    event.payload_redacted_at = now


def redact_for_tenant(session: Session, *, tenant_id: Any, now: datetime) -> int:
    """Erase the raw bodies belonging to one tenant, now — the deletion path.

    Only the rows that resolved to exactly one tenant, which is the honest
    reach of this operation. A body that was ambiguous or never resolvable is
    not erased on request; it expires on the retention clock instead, and
    `career.onboarding.privacy` says so to the customer rather than reporting a
    completeness it does not have.

    The row survives, redacted. It is the idempotency record, and `career_app`
    has no DELETE on this table in any case.
    """
    events = session.execute(
        select(WebhookEvent).where(
            WebhookEvent.subject_tenant_id == tenant_id,
            WebhookEvent.payload_redacted_at.is_(None),
        )
    ).scalars().all()
    for event in events:
        _redact(event, now=now)
    session.flush()
    return len(events)


def prune_webhook_payloads(
    session: Session,
    *,
    now: datetime,
    retention_days: int = RAW_PAYLOAD_RETENTION_DAYS,
) -> dict[str, int]:
    """The nightly pass: link what has become linkable, expire what is old.

    Two halves, in this order on purpose. The late link exists because the
    most valuable event of all — the order that pays for a new subscription —
    arrives BEFORE the tenant it creates, so at intake there was nothing to
    resolve against and by tonight there is. Linking before redacting means a
    body that is about to expire is still credited to its customer, which is
    what makes the count in their deletion report honest.

    Runs on the owner session inside the nightly, spans tenants by nature, and
    returns counters rather than sending anything: a per-customer notice about
    housekeeping is noise, and the only number an operator needs to see is the
    one that says something is stuck.
    """
    counts = {
        "linked": 0, "redacted": 0, "unlinked": 0, "expired_unprocessed": 0,
        #: Bodies left intact past the window because they still hold a
        #: credential nobody has decided about (see `_AUTHORIZE_EVENT`).
        "deferred_credentials": 0,
        #: …and the ones whose credential ran out while it waited. That is a
        #: decision the operator never took, and the only place it is visible.
        "expired_credentials": 0,
    }

    unlinked = session.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.subject_tenant_id.is_(None),
            WebhookEvent.payload_redacted_at.is_(None),
        )
        .order_by(WebhookEvent.received_at)
        .limit(_PRUNE_BATCH)
    ).scalars().all()
    for event in unlinked:
        resolved = resolve_subject_tenant(
            session,
            provider=event.provider,
            payload=event.payload if isinstance(event.payload, dict) else {},
            order_id=event.salla_order_id,
        )
        if resolved is None:
            # An install hook, a webhook for an order we refused, a batch
            # carrying two customers, a number that never became a channel.
            # There is no tenant to attribute it to and inventing one would be
            # worse than the clock, which reaches it anyway.
            counts["unlinked"] += 1
            continue
        event.subject_tenant_id = uuid.UUID(resolved)
        counts["linked"] += 1

    cutoff = now - timedelta(days=retention_days)
    expiring = session.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.payload_redacted_at.is_(None),
            WebhookEvent.received_at < cutoff,
        )
        .order_by(WebhookEvent.received_at)
        .limit(_PRUNE_BATCH)
    ).scalars().all()
    for event in expiring:
        if credential_is_still_consumable(event, now=now):
            # The one body that is not scaffolding, and the one promise a
            # refusal made about it. It is deferred, not exempt: the clause
            # that keeps it is the credential's own expiry, so this row comes
            # back to this loop and is redacted the day it stops mattering.
            counts["deferred_credentials"] += 1
            continue
        if (
            event.provider == "salla"
            and event.event_type == _AUTHORIZE_EVENT
            and event.processing_status == "received"
        ):
            # It WAS the deferred credential and its expiry has passed: nobody
            # ever consumed it, and now nobody can. Counted apart from the
            # generic stuck-row number because the action is different — this
            # one is «a store's install is still not connected».
            counts["expired_credentials"] += 1
        if event.processing_status == "received":
            # Still queued after the whole retention window. The retry budget
            # gave up on it weeks ago, so the body is expiring on a row nobody
            # ever finished — the payload goes regardless (it is not more
            # exempt for being stuck) but the operator hears the count, because
            # a silent one of these is a customer whose webhook never landed.
            counts["expired_unprocessed"] += 1
        _redact(event, now=now)
        counts["redacted"] += 1

    session.flush()
    if counts["expired_unprocessed"]:
        logger.warning(
            "webhook prune: %d events were still unprocessed when their raw "
            "body expired after %d days",
            counts["expired_unprocessed"],
            retention_days,
        )
    if counts["expired_credentials"]:
        logger.error(
            "webhook prune: %d salla authorize credentials were never consumed "
            "and have now expired — the store that sent them is not connected "
            "and only a reinstall can offer another one",
            counts["expired_credentials"],
        )
    if counts["deferred_credentials"]:
        logger.info(
            "webhook prune: %d authorize bodies kept past the retention window "
            "— they still hold a usable credential awaiting an operator "
            "decision",
            counts["deferred_credentials"],
        )
    return counts
