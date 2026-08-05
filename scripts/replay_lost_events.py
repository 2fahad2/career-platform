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
skipped as pointless rather than replayed as risky.

*A reply to a conversation that has moved on.* An inbound from three weeks ago
replayed today is answered today, in the present tense, out of nowhere. Age is
irrelevant for a delivery receipt and decisive for a message, so
``--max-age-days`` is enforced on outbound-capable kinds only.

*Silently rolling a delivery backwards.* ``_handle_status`` assigns the receipt's
status unconditionally, so replaying a stale ``sent`` over a message that has
since been ``read`` would erase the newer truth. Every status entry is compared
against what ``delivery_messages`` already holds and only a strictly forward
move is replayed. This is why the tool reads the target rows instead of
trusting the timestamps.

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

#: Where the replay ledger lives. Under data/ so it is gitignored: it is an
#: operational record, not source, and it names live event ids.
DEFAULT_AUDIT_PATH = Path("data/ops/replay_lost_events.jsonl")

#: The kind whose handler provably cannot message a customer.
SAFE_EVENT_TYPES = frozenset({"statuses"})

#: Meta's receipt progression. ``_handle_status`` overwrites the stored status
#: with whatever the receipt says, so a replay may only ever move a delivery
#: FORWARD along this ladder. ``failed`` sits at the top deliberately — once a
#: send is known failed, an older optimistic receipt must not overwrite it.
_STATUS_RANK = {
    "": -1, "queued": 0, "accepted": 1, "sent": 2,
    "delivered": 3, "read": 4, "failed": 5,
}


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
    return _STATUS_RANK.get((status or "").lower(), -1)


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
        return _outcome(event, len(items), action="skip",
                        reason="no delivery_messages row to apply it to")
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
    answering it now would still make sense to the human who sent it."""
    if not items:
        return _outcome(event, len(items), action="skip",
                        reason="payload carries no inbound messages")

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
    print(f"\n  to replay : {replayed}")
    print(f"  skipped   : {len(outcomes) - replayed}")
    for key, count in sorted(tally.items()):
        print(f"    {count:>3}  {key}")

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
