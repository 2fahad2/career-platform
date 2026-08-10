"""Recover webhook events the Salla sweep destroyed (INCIDENT 2026-08-03).

``process_pending_webhooks`` selected every row in ``webhook_events`` with
``processing_status = 'received'``, with no predicate on ``provider``. WhatsApp
rows therefore entered the Salla routing, matched neither the provision nor the
lifecycle event names, and fell into the final ``else`` that marks a row
``ignored``. Nothing in this codebase re-reads ``ignored`` — the WhatsApp
worker selects ``received`` only — so those events were not delayed, they were
destroyed. The query is fixed in career.salla.provisioning; this script is for
the rows the bug already ate.

WHY A REPLAY IS NOT SIMPLY "SET IT BACK TO received"
----------------------------------------------------
Re-queuing hands an old event to the live WhatsApp worker as if it had just
arrived, and the worker's job includes talking to customers. A careless replay
therefore has three distinct ways to do harm, and each one is answered here in
code rather than in the operator's memory:

*Duplicate outbound.* Only ``messages`` events can produce an outbound — the
``statuses`` path (``worker._handle_status``) writes to ``delivery_messages``
and sends nothing at all. So ``statuses`` is the default and only kind this
tool will replay; anything that can speak to a customer needs ``--allow-outbound``
typed by a human who has thought about it. On top of that gate, every message
id in the payload is checked against ``inbound_messages`` first: the worker is
idempotent per ``wa_message_id`` and returns before sending anything for a
message it has already recorded, so an event whose messages are all recorded is
skipped as pointless rather than replayed as risky. That guard has a hole it
does not cover on its own, and it is the reason a ``failed`` row is refused
outright for outbound-capable kinds: the worker writes ``failed`` after a
rollback, so its outbound already happened while the ``inbound_messages`` row
that proves it did not survive. See :func:`_verdict_for_messages`.

*A reply to a conversation that has moved on.* An inbound from three weeks ago
replayed today is answered today, in the present tense, out of nowhere. Age is
irrelevant for a delivery receipt and decisive for a message, so
``--max-age-days`` is enforced on outbound-capable kinds only.

*Silently rolling a delivery backwards.* Every status entry is compared against
what ``delivery_messages`` already holds and only a strictly forward move is
replayed, which is why the tool reads the target rows instead of trusting the
timestamps. «Forward» is not this file's opinion: the ladder is imported from
``career.whatsapp.worker``, the one module that writes the column, because a
second copy of it here shipped disagreeing with the first (see :func:`_rank`).
The worker itself also refuses a backwards receipt, so a mis-ranked replay
costs an operator a false report rather than a corrupted row — but a false
report on a recovery tool is what the ledger then makes permanent.

A RECEIPT WITH NO LEDGER ROW IS COUNTED, NOT REPAIRED
-----------------------------------------------------
Some receipts have no ``delivery_messages`` row to move at all: Meta accepted
and delivered a message that our ledger never recorded. This tool has always
skipped them — ``_handle_status`` iterates the rows matching the wamid, so
replaying such an event does precisely nothing — and the skip is now a NAMED,
COUNTED line in the report (:data:`NO_LEDGER_ROW`), because this is the only
place in the tree that already knows the number and it was being spent on a
truncated tally key.

Counted, and deliberately not repaired. Teaching this script to CREATE the
missing row would destroy the one argument that makes it safe to run: it only
ever moves an existing row FORWARD, so its worst case is a no-op the worker
refuses. And the row it would create could not be honest — a receipt carries
the wamid, the status and the price band, and names NEITHER the message kind
NOR the template name, which are exactly the two columns
``cv/close.whatsapp_spend`` bills on and ``_wa_kind_of`` reads. The row would
carry a real wamid, a guessed ``kind`` and an invented ``template_name``, and
nothing downstream — the spend arithmetic, the console's message-status panel,
the detector in ``scripts/backfill_meta_error_codes.py`` that finds these holes
— could tell it from a row the delivery path wrote. A ROW WE INVENT IS WORSE
THAN A HOLE WE CAN SEE (CHANGELOG §39). The hole belongs to the write path;
this script reports it and keeps its hands off.

IDEMPOTENCY
-----------
Two independent guards, because the first one is only true while the criteria
stay at their defaults. (1) A replayed row leaves the ``ignored``/``failed``
selection the moment it becomes ``received``, so a second run with the same
criteria selects nothing. (2) Every replayed event id is appended to the audit
ledger, and an id already in the ledger is refused — which holds even if
someone passes ``--status received`` or points at a different criteria set.

Dry-run is the default. ``--apply`` is the only thing that writes.

Run:
    .venv/bin/python scripts/replay_lost_events.py                  # dry run
    .venv/bin/python scripts/replay_lost_events.py --apply
    .venv/bin/python scripts/replay_lost_events.py \\
        --event-type messages --allow-outbound --max-age-days 2 --apply

Output is redacted by construction: event ids, counts and outcomes only. No
phone, no message body, no payload — the payloads this script reads are full of
both.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from career.config import get_settings
from career.db.models import DeliveryMessage, InboundMessage, WebhookEvent
from career.logging_filters import install_secret_redaction
from career.whatsapp.worker import receipt_rank

#: Where the replay ledger lives. Under data/ so it is gitignored: it is an
#: operational record, not source, and it names live event ids.
DEFAULT_AUDIT_PATH = Path("data/ops/replay_lost_events.jsonl")

#: The kind whose handler provably cannot message a customer.
SAFE_EVENT_TYPES = frozenset({"statuses"})

#: The status a replay may NEVER treat as «nothing happened yet». See
#: :func:`_verdict_for_messages` — the worker had already spoken to the
#: customer before it rolled back.
ROLLED_BACK_STATUS = "failed"

#: The verdict for a receipt whose message was never written to the ledger.
#: A CONSTANT because the report counts it by identity: matching on a prefix
#: of a sentence is how a rename turns a named incident back into an anonymous
#: skip line. See the module docstring — this is a hole in the ledger, not a
#: replayable event, and this tool reports it rather than filling it.
NO_LEDGER_ROW = "no delivery_messages row to apply it to"


@dataclass(frozen=True)
class Outcome:
    """One event's verdict. ``event_id`` is a UUID and safe to print; nothing
    else on this record derives from the payload."""

    event_id: uuid.UUID
    event_type: str
    from_status: str
    action: str          # replay | skip
    reason: str
    entries: int = 0     # how many messages/statuses the payload carried


def _rank(status: str | None) -> int:
    """The receipt ladder, borrowed from the only module that writes it.

    This file used to carry its own, and it disagreed with the worker's on the
    one comparison with a customer behind it: it put ``failed`` at the TOP, so
    a ``delivered`` or ``read`` receipt destroyed while the row still said
    ``failed`` was ranked BACKWARDS and skipped as «already at or past this
    receipt» — the tool refused to recover precisely the receipts worth
    recovering, the row stayed ``failed``, ``cv/close.whatsapp_spend`` went on
    excluding a message the customer had read, and the console showed the
    operator a failure for a message that had arrived. The mirror case was as
    bad in the other direction: a stale ``sent`` over a ``failed`` ranked
    FORWARD here, so the tool re-queued an event the worker then correctly
    no-ops — and the audit ledger refuses that event a second time forever, so
    the report the operator acted on was simply false.

    The argument for the worker's order lives with the order, on
    ``worker.RECEIPT_ORDER``. It wins for the plain reason that it is the one
    the write path obeys: a verdict computed on a ladder the writer does not
    use is a prediction about a different program.
    """
    return receipt_rank(status)


def _entries(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Pull the messages[] or statuses[] leaves out of Meta's nesting. Never
    returns the payload itself, so a caller cannot accidentally print one."""
    out: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for item in value.get(key, []) or []:
                if isinstance(item, dict):
                    out.append(item)
    return out


def _outcome(
    event: WebhookEvent, entries: int, *, action: str, reason: str
) -> Outcome:
    """Build a verdict carrying the four fields every one of them repeats.

    This used to be a dict splatted into the constructor, which reads well and
    types as ``**dict[str, object]`` — so mypy could no longer tell UUID from
    str and the gate went red on twenty-four identical errors. A factory keeps
    the de-duplication and keeps the field types.
    """
    return Outcome(
        event_id=event.id, event_type=event.event_type,
        from_status=event.processing_status, entries=entries,
        action=action, reason=reason,
    )


def _verdict_for_statuses(
    session: Session, event: WebhookEvent, items: list[dict[str, Any]]
) -> Outcome:
    """A receipt is worth replaying only if it still tells us something new AND
    would not undo something newer."""
    if not items:
        return _outcome(event, len(items), action="skip",
                        reason="payload carries no delivery receipts")

    forward = 0
    targets = 0
    for item in items:
        wamid = item.get("id")
        incoming = str(item.get("status", ""))
        if not wamid:
            continue
        rows = list(session.execute(
            select(DeliveryMessage.status).where(
                DeliveryMessage.wa_message_id == str(wamid)
            )
        ).scalars().all())
        if not rows:
            continue          # receipt for a message we never recorded
        targets += len(rows)
        if any(_rank(incoming) > _rank(current) for current in rows):
            forward += 1

    if targets == 0:
        # Not «nothing to do»: Meta priced a send our ledger never recorded.
        # Skipping is still right — `_handle_status` iterates matching rows
        # and would touch none — but the number is reported by name rather
        # than folded into a truncated tally key. See the module docstring for
        # why the row is not created here.
        return _outcome(event, len(items), action="skip", reason=NO_LEDGER_ROW)
    if forward == 0:
        return _outcome(event, len(items), action="skip",
                        reason="delivery status already at or past this receipt")
    return _outcome(event, len(items), action="replay",
                        reason=f"{forward} receipt(s) would move a delivery forward")


def _verdict_for_messages(
    session: Session, event: WebhookEvent, items: list[dict[str, Any]],
    *, max_age_days: int, now: datetime,
) -> Outcome:
    """An inbound is worth replaying only if it was never recorded, and only if
    answering it now would still make sense to the human who sent it.

    AND only if the worker has not already answered it once. The two statuses
    this tool selects by default are NOT alike, and treating them alike is how
    it could send a customer the same thing twice:

    ``ignored`` is a row the Salla sweep marked terminal without any WhatsApp
    code ever looking at it. Nothing was sent, nothing was written, and the
    idempotency argument below holds in full.

    ``failed`` is the opposite. It is written by ``worker`` in its own except
    block, AFTER ``owner_session.rollback()`` — which means the worker DID run
    the event: it may have activated an order, replied to an unknown number,
    sent a support ack, landed a held bundle. Those are HTTP calls to Meta and
    they are not in the transaction; the rollback took back only our side, the
    ``inbound_messages`` row included. So the per-``wa_message_id`` guard that
    makes a replay safe is exactly the row the rollback destroyed:
    ``unrecorded`` counts every one of those messages as «never handled», and
    replaying hands the worker a message it will process from the top and send
    for a second time. The customer reads it twice; for an activation or a
    delivery that is not a cosmetic duplicate.

    A ``failed`` row is therefore refused for any outbound-capable kind, with
    no flag to override it, because nothing in the payload can tell us whether
    the send happened before the exception or after it — and «probably not»
    is not a standard for talking to a paying customer. Receipts are
    unaffected: ``_handle_status`` sends nothing, so ``statuses`` events are
    still recovered from ``failed`` exactly as before, which is the recovery
    the 2026-08-03 incident actually needs.
    """
    if not items:
        return _outcome(event, len(items), action="skip",
                        reason="payload carries no inbound messages")

    if event.processing_status == ROLLED_BACK_STATUS:
        return _outcome(
            event, len(items), action="skip",
            reason="the worker already processed this and rolled back — its "
                   "outbound is on the wire and cannot be un-sent; replaying "
                   "would message the customer twice",
        )

    unrecorded = 0
    for item in items:
        wamid = item.get("id")
        if not wamid:
            continue
        seen = session.execute(
            select(InboundMessage.id).where(
                InboundMessage.wa_message_id == str(wamid)
            )
        ).first()
        if seen is None:
            unrecorded += 1

    if unrecorded == 0:
        return _outcome(event, len(items), action="skip",
                        reason="every message already recorded by another path")

    age_days = (now - event.received_at).days
    if age_days > max_age_days:
        return _outcome(event, len(items), action="skip",
                        reason=f"{age_days}d old — replying now would answer a "
                              f"conversation that has moved on "
                              f"(--max-age-days {max_age_days})")
    return _outcome(event, len(items), action="replay",
                        reason=f"{unrecorded} message(s) never recorded")


def _verdict(
    session: Session, event: WebhookEvent, *, max_age_days: int, now: datetime,
) -> Outcome:
    payload: dict[str, Any] = event.payload or {}
    if event.event_type == "statuses":
        return _verdict_for_statuses(session, event, _entries(payload, "statuses"))
    if event.event_type == "messages":
        return _verdict_for_messages(
            session, event, _entries(payload, "messages"),
            max_age_days=max_age_days, now=now,
        )
    # 'other' — account updates, template state changes. The worker walks the
    # same two leaves, so an event carrying neither is a no-op by construction
    # and there is nothing to recover.
    both = _entries(payload, "messages") + _entries(payload, "statuses")
    if not both:
        return Outcome(event_id=event.id, event_type=event.event_type,
                       from_status=event.processing_status, action="skip",
                       reason="no messages or statuses to process", entries=0)
    return _verdict_for_messages(
        session, event, _entries(payload, "messages"),
        max_age_days=max_age_days, now=now,
    )


def _read_ledger(path: Path) -> set[str]:
    """Event ids this tool has already re-queued. A malformed line is skipped
    rather than fatal: a half-written ledger must not block a recovery."""
    if not path.exists():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("action") == "replay" and not row.get("dry_run", True):
            done.add(str(row.get("event_id")))
    return done


def _append_ledger(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-queue webhook events a foreign worker marked terminal.",
    )
    parser.add_argument("--provider", default="whatsapp",
                        help="webhook_events.provider to recover (default: whatsapp)")
    parser.add_argument("--status", default="ignored,failed",
                        help="terminal statuses to select (default: ignored,failed)")
    parser.add_argument("--event-type", default="statuses",
                        help="event types to consider; anything outside "
                             f"{sorted(SAFE_EVENT_TYPES)} needs --allow-outbound")
    parser.add_argument("--since", default=None,
                        help="only events received on/after this ISO timestamp")
    parser.add_argument("--until", default=None,
                        help="only events received before this ISO timestamp")
    parser.add_argument("--max-age-days", type=int, default=2,
                        help="refuse to replay an outbound-capable event older "
                             "than this (default: 2)")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--allow-outbound", action="store_true",
                        help="permit event types whose handler can message a "
                             "customer (messages/other)")
    parser.add_argument("--apply", action="store_true",
                        help="actually re-queue; without it nothing is written")
    parser.add_argument("--audit-file", default=str(DEFAULT_AUDIT_PATH))
    args = parser.parse_args()

    install_secret_redaction()

    statuses = _csv(args.status)
    event_types = _csv(args.event_type)
    unsafe = [t for t in event_types if t not in SAFE_EVENT_TYPES]
    if unsafe and not args.allow_outbound:
        print(f"REFUSING: {', '.join(unsafe)} can produce a customer-facing "
              "message. Re-run with --allow-outbound if that is what you mean.")
        return 2

    settings = get_settings()
    now = datetime.now(UTC)
    audit_path = Path(args.audit_file)
    already = _read_ledger(audit_path)

    print(f"replay_lost_events — {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"  database    : {settings.db_name}")
    print(f"  provider    : {args.provider}")
    print(f"  statuses    : {', '.join(statuses)}")
    print(f"  event types : {', '.join(event_types)}")
    if ROLLED_BACK_STATUS in statuses and unsafe:
        # Say the rule out loud BEFORE the verdicts, so an operator who came
        # here to recover a customer's lost message is told why none of the
        # `failed` ones will move — rather than reading ten skip lines and
        # concluding the tool is broken.
        print(f"  refusing    : {ROLLED_BACK_STATUS} + {', '.join(unsafe)} — "
              "the worker already sent, then rolled back; a replay would "
              "message the customer twice")
    if args.since or args.until:
        print(f"  window      : {args.since or '-'} .. {args.until or '-'}")
    print(f"  ledger      : {audit_path} ({len(already)} already replayed)")

    engine = create_engine(settings.owner_database_url, future=True)
    outcomes: list[Outcome] = []
    with Session(engine) as session:
        stmt = (
            select(WebhookEvent)
            .where(
                WebhookEvent.provider == args.provider,
                WebhookEvent.processing_status.in_(statuses),
                WebhookEvent.event_type.in_(event_types),
            )
            .order_by(WebhookEvent.received_at)
            .limit(args.limit)
        )
        if args.since:
            stmt = stmt.where(
                WebhookEvent.received_at >= datetime.fromisoformat(args.since))
        if args.until:
            stmt = stmt.where(
                WebhookEvent.received_at < datetime.fromisoformat(args.until))
        events = list(session.execute(stmt).scalars().all())

        for event in events:
            if str(event.id) in already:
                outcomes.append(Outcome(
                    event_id=event.id, event_type=event.event_type,
                    from_status=event.processing_status, action="skip",
                    reason="already re-queued by an earlier run (ledger)",
                ))
                continue
            outcome = _verdict(
                session, event, max_age_days=args.max_age_days, now=now)
            outcomes.append(outcome)
            if outcome.action == "replay" and args.apply:
                # 'received' is the ONE status the WhatsApp worker selects, so
                # this is the whole of the re-queue. processed_at and
                # attempt_count are left exactly as they are: they are the
                # honest history of what was done to this row, and a recovery
                # tool that tidies away the evidence of the incident is worse
                # than one that does not.
                event.processing_status = "received"
        if args.apply:
            session.commit()

    tally = Counter(f"{o.action}:{o.reason.split(' —')[0][:40]}" for o in outcomes)
    print(f"\nselected {len(outcomes)} event(s)")
    for outcome in outcomes:
        verb = "REPLAY" if outcome.action == "replay" else "skip  "
        if outcome.action == "replay" and not args.apply:
            verb = "would replay"
        print(f"  {verb:>13}  {outcome.event_id}  {outcome.event_type:<9} "
              f"({outcome.from_status}, {outcome.entries} entr"
              f"{'y' if outcome.entries == 1 else 'ies'})  {outcome.reason}")

    replayed = sum(1 for o in outcomes if o.action == "replay")
    holes = [o for o in outcomes if o.reason == NO_LEDGER_ROW]
    print(f"\n  to replay : {replayed}")
    print(f"  skipped   : {len(outcomes) - replayed}")
    print(f"  no ledger row : {len(holes)}")
    for key, count in sorted(tally.items()):
        print(f"    {count:>3}  {key}")
    if holes:
        # A named line and not a tally row, because this one is not about the
        # replay at all: it says a message Meta accepted has nothing in
        # `delivery_messages`. Nothing here can fix that — the receipt does not
        # carry the kind or the template name — so it points at the tool that
        # counts them all, billable ones first.
        print("  NOTE: those events carry receipts for messages with NO ledger "
              "row. A replay cannot fix that and this tool will not invent the "
              "row (see the module docstring). Count them, billable first, with "
              "scripts/backfill_meta_error_codes.py")

    _append_ledger(audit_path, [
        {
            "at": now.isoformat(),
            "dry_run": not args.apply,
            "event_id": str(o.event_id),
            "event_type": o.event_type,
            "from_status": o.from_status,
            "action": o.action,
            "reason": o.reason,
            "entries": o.entries,
        }
        for o in outcomes
    ])
    if not args.apply and replayed:
        print("\nDRY RUN — nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
