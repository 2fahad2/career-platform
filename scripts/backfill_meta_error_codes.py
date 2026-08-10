"""Fill `delivery_messages.meta_error_code` from the payloads that still hold it.

Migration 0030 added the column. It did not fill it, and the separation is the
whole point:

    A MIGRATION THAT ALSO BACKFILLS IS A MIGRATION THAT CANNOT BE RE-RUN AND
    CANNOT BE REVIEWED AS DATA.

It cannot be re-run because `alembic upgrade` will not visit 0030 twice — the
version table says it is done — so a backfill inside it gets exactly one
attempt, on whatever data existed at that instant, in a transaction that also
holds an ACCESS EXCLUSIVE lock on the table it is scanning. And it cannot be
reviewed as data because nobody can look at what it WOULD do before it does
it: the DDL and the UPDATE commit or roll back together, so «show me the two
rows you are about to change» is not a question the migration can answer. A
script can be run, read, argued with, and run again.

WHAT IT DOES
------------
`worker._handle_status` records Meta's refusal code from now on. Everything
before 0030 discarded it — but not irrecoverably: the whole status callback,
`errors` array included, is still on `webhook_events.payload`. This script
matches `statuses[].id` to `delivery_messages.wa_message_id` and writes the
code onto the row it belongs to.

THE CLOCK ON IT, because this is not a job that can wait: 0024 REDACTS
`webhook_events.payload` after 30 days — those bodies carry the customer's
phone and his own words, and the redaction is not optional. Every day this is
not run, the oldest recoverable refusal moves out of reach permanently. As of
the last measurement zero of 283 status payloads were redacted and the oldest
predates the oldest expiry, so today the history is complete; it will not stay
that way.

The number this exists to make joinable: two of six historical expiries were
Meta REFUSING the send, not the customer going quiet. The operator's screen
renders every one of the six as «⌛ انتهت مهلتها دون تسليم», and the 72-hour
start guarantee is priced from that count.

IDEMPOTENCY, AND WHY IT IS FILL-ONLY
------------------------------------
Only a row whose `meta_error_code` IS NULL is ever written. A row that already
carries a code is left exactly as it is, and one carrying a DIFFERENT code is
counted and reported rather than overwritten: that value was written by the
live handler, which saw the receipt that won the rank comparison, and this
script is reading history through a JSON scan that cannot tell which receipt
won. History fills gaps; it does not correct the present.

So the second run of this script writes nothing, and says so.

THE LEDGER HOLE, AND WHY IT IS COUNTED HERE
-------------------------------------------
This script already reads every stored receipt, so counting the receipts that
have NO `delivery_messages` row at all costs one more `IN (...)` and answers a
question nobody was asking: «did a message we sent leave no trace in the
ledger?» It went unasked for 24 days because the only line resembling it —
«codes with no ledger row» — counts ERROR-carrying receipts, and the missing
rows were `delivered`.

The count is SPLIT, because a hole has three causes and only one of them is a
defect (CHANGELOG §39):

  * *a lost write* — the send succeeded, the ledger INSERT died in a rolled
    back transaction. Meta billed us, the customer holds his CV, and our row
    says nothing. This is the defect.
  * *a send no code ever records* — the worker's conversational acks,
    `funnel/flow.py`, `whatsapp/samples.py`, the activation refusals. Free
    `service` receipts by the dozen. An old decision, not a defect.
  * *a refused send* — the send raised, so no row was written, which is
    correct: a row for a send that was never accepted is a bill we were never
    charged.

A detector that adds those together fires daily on the free acks and teaches
the operator to skip the report — the exact failure mode §37 paid for, where
one false line at the top buried seven true ones underneath. So the line worth
a page is the NARROW one: `pricing.billable`, or a band that is not `service`,
with no row. On 24 days of live staging data that set has exactly one member
(a `utility` template from 2026-07-17) and would have raised zero false alarms;
the twelve free `service` acks and the one refusal are counted on their own
lines, where they explain themselves instead of crying.

NO SCRIPT IN THIS TREE BECOMES A LEDGER AUTHOR
----------------------------------------------
The obvious next move — write the missing rows from the receipts — is REFUSED,
and the refusal is the finding, not an omission.

A receipt carries the wamid, the status and the price band. It names NEITHER
the message kind NOR the template. Those two are exactly the columns spend is
billed on (`cv/close.whatsapp_spend` narrows to `kind == 'template'` and prices what is
left by `template_name`, and its docstring calls that line load-bearing for
correctness) — the same two `_wa_kind_of` reads. So a repair row would carry a real wamid,
a GUESSED `kind` and an invented-or-NULL `template_name` — and nothing
downstream could tell it from a row the delivery path wrote: not the spend
arithmetic, not the console's message-status panel, not the detector above,
which would go quiet having been fed its own guess.

A ROW WE INVENT IS WORSE THAN A HOLE WE CAN SEE. The hole is visible, bounded,
and dated; the invention is permanent and indistinguishable. The fix for a lost
write belongs where the write is lost — the durability point moving ahead of
the first send, and `LEDGER_FAILED` with its true counts when it is lost
anyway — not in a repair script reading yesterday's receipts.

THE CATEGORY COLUMN IS READ AND NEVER WRITTEN
---------------------------------------------
0030 also added `delivery_messages.category`, filled from `pricing.category`
on the same receipts. This script MEASURES how much of that is recoverable
from the same payloads and writes none of it, deliberately: one script, one
write. If the measurement says the history is there, filling it is a
three-line change to this file made by someone who has read the number first —
which is exactly the review a backfill inside a migration cannot get.

WHAT IT PRINTS
--------------
Counts, Meta codes, and Meta's own category words. No phone number, no message
id, no payload fragment, no customer text — the payloads this reads are full of
all four.

ROLE AND SCOPE
--------------
Runs as `career_app` like everything else that is not one of the six declared
one-shots: it reads `webhook_events` (no RLS — an order arrives before the
tenant is known) on an unscoped sweep session, then opens ONE tenant session
per tenant to do the writing, because `delivery_messages` is behind RLS and an
unscoped UPDATE against it silently matches nothing. That silence is the
failure mode this tool must not have, so every code it could not place on a
row is reported as an unmatched count rather than left out of the arithmetic.

Its one honest limit: tenants are enumerated through
`app.tenant_ids_with_status`, which reads `subscriptions`. A tenant that has
never had a subscription row cannot be enumerated by the application role at
all, and its rows will show up in `unmatched`. Name it with `--tenant` if the
count is not zero and you know where the rows are.

Run:
    .venv/bin/python scripts/backfill_meta_error_codes.py            # dry run
    .venv/bin/python scripts/backfill_meta_error_codes.py --apply
    .venv/bin/python scripts/backfill_meta_error_codes.py --tenant <uuid> --apply
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import DeliveryMessage, WebhookEvent
from career.logging_filters import install_secret_redaction
from career.salla.subscriptions import ALL_STATES
from career.whatsapp.worker import status_billed_category, status_error_codes

#: How many ids to put in one IN (...) — the tables are small today and this
#: is about not writing a query whose size depends on how long the product has
#: been running.
_CHUNK = 500


def status_objects(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every `statuses[]` entry in one webhook body.

    The same walk `worker.process_pending_whatsapp` does — entry → changes →
    value → statuses — and total in the same way: a body that does not have
    that shape yields nothing instead of raising. This tool reads years of
    stored bodies, and the one thing it must not do is stop at the first one
    that surprises it.
    """
    for entry in payload.get("entry", []) or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []) or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            for st in value.get("statuses", []) or []:
                if isinstance(st, dict):
                    yield st


#: The band Meta puts on the free-form replies it delivers inside the open 24h
#: window and charges nothing for. Named because the whole point of the hole
#: report is that a `service` hole is not news and anything else is.
FREE_BAND = "service"


def status_is_billable(st: dict[str, Any]) -> bool | None:
    """Meta's own `pricing.billable` flag, or None when it did not say.

    Written HERE and not imported, unlike every other parser in this file, for
    a stated reason: `worker.status_billed_category` refuses to read
    `billable` on purpose — netting Meta's zero-rated sends out of
    `whatsapp_spend` would change the DEFINITION of spend, and that is a
    separate decision with its own evidence. This function is not part of that
    decision and does not touch the bill: it only decides which line of a
    REPORT a missing ledger row is counted on. Nothing it returns is ever
    written to a column.

    Total, like the two it sits beside: this is somebody else's HTTP body.
    A missing, malformed or non-boolean flag answers None, which lands the
    receipt on the quiet line unless its band already speaks for it.
    """
    pricing = st.get("pricing")
    if not isinstance(pricing, dict):
        return None
    raw = pricing.get("billable")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
        return raw.strip().lower() == "true"
    return None


@dataclass(frozen=True)
class Scan:
    """What history still knows. Nothing here is per-tenant: a stored payload
    is not scoped to anyone, which is exactly why the writing half is."""

    #: wa_message_id → the leading Meta code, the same choice the live handler
    #: makes (`worker._handle_status`). Membership here is also this file's
    #: definition of «the send was refused»: a code is Meta declining it.
    codes: dict[str, int] = field(default_factory=dict)
    #: wa_message_id → the billed category Meta reported. MEASURED ONLY —
    #: nothing in this file writes it.
    categories: dict[str, str] = field(default_factory=dict)
    #: EVERY wa_message_id that appeared in a status object, whether or not it
    #: carried a code or a band. The denominator of the ledger-hole report,
    #: and the reason that report was free to add: these ids are read already.
    receipt_ids: set[str] = field(default_factory=set)
    #: the subset Meta flagged `pricing.billable` — true on ANY receipt for
    #: that id. Monotone on purpose: being billed once is being billed.
    billable_ids: set[str] = field(default_factory=set)
    events: int = 0
    redacted: int = 0
    status_objects: int = 0

    @property
    def code_histogram(self) -> Counter[int]:
        return Counter(self.codes.values())

    @property
    def category_histogram(self) -> Counter[str]:
        return Counter(self.categories.values())


def scan_payloads(session: Session) -> Scan:
    """Read every stored WhatsApp payload and pull out what 0030 can hold.

    The parsers are IMPORTED, not re-implemented: `status_error_codes` and
    `status_billed_category` are the same two functions the live handler uses,
    so a backfill can never disagree with the handler about what a payload
    says. This repository has already paid for the other arrangement once —
    `scripts/replay_lost_events.py` shipped its own copy of the receipt ladder
    and it disagreed with the worker's on the one comparison with a customer
    behind it.

    LAST CODE WINS when one wa_message_id appears in several payloads. Meta
    redelivers webhooks freely, and a message that was refused and later
    retried can carry a code on more than one callback; the later body is the
    later fact. It matters less than it looks — the live handler will not let
    this touch a row that already has a code at all.
    """
    codes: dict[str, int] = {}
    categories: dict[str, str] = {}
    receipt_ids: set[str] = set()
    billable_ids: set[str] = set()
    events = redacted = seen = 0

    rows = session.execute(
        select(WebhookEvent.payload, WebhookEvent.payload_redacted_at)
        .where(WebhookEvent.provider == "whatsapp")
        .order_by(WebhookEvent.received_at)
    ).all()

    for payload, redacted_at in rows:
        events += 1
        if redacted_at is not None:
            # 0024 replaced the body with a PII-free skeleton. The `errors`
            # array went with it; this row is not recoverable and is counted
            # so the operator can see the fuse burning.
            redacted += 1
            continue
        for st in status_objects(payload or {}):
            seen += 1
            wamid = st.get("id")
            if not isinstance(wamid, str) or not wamid:
                continue
            receipt_ids.add(wamid)
            found = status_error_codes(st)
            if found:
                codes[wamid] = found[0]
            category = status_billed_category(st)
            if category is not None:
                categories[wamid] = category
            if status_is_billable(st):
                # OR-ed across receipts rather than last-wins: `delivered`
                # carries the pricing and `read` often carries none, so the
                # later body would otherwise un-bill a message Meta charged
                # for. The band above keeps last-wins because there Meta is
                # RE-stating the band; here it is simply silent.
                billable_ids.add(wamid)

    return Scan(codes=codes, categories=categories, receipt_ids=receipt_ids,
                billable_ids=billable_ids, events=events,
                redacted=redacted, status_objects=seen)


@dataclass
class Applied:
    """The arithmetic of one session's share of the work.

    `matched + unmatched == len(scan.codes)` is checked by the caller, because
    a backfill whose numbers do not add up has found rows it cannot see, and
    that is the report — not an exception.
    """

    matched: int = 0
    filled: int = 0
    already: int = 0
    conflicting: int = 0
    #: matched rows that already carry a billed category
    category_present: int = 0
    #: matched rows with no category whose payload could supply one — the
    #: measurement this script makes and does not act on
    category_recoverable: int = 0
    by_code: Counter[int] = field(default_factory=Counter)
    #: every scanned wa_message_id this session actually found on a row, code
    #: or no code. A SET and not a count because the sessions are per-tenant
    #: and the question is asked of all of them at once: an id present in one
    #: tenant is not a hole just because the next tenant cannot see it.
    present: set[str] = field(default_factory=set)

    def absorb(self, other: Applied) -> None:
        self.matched += other.matched
        self.filled += other.filled
        self.already += other.already
        self.conflicting += other.conflicting
        self.category_present += other.category_present
        self.category_recoverable += other.category_recoverable
        self.by_code.update(other.by_code)
        self.present |= other.present


def apply_codes(session: Session, scan: Scan, *, apply: bool = False) -> Applied:
    """Fill the gaps this session can see. Writes only when ``apply``.

    In a dry run not one attribute is assigned — the rows are read, counted
    and left untouched — so «dry run» is a property of the code path and not a
    promise about a rollback somebody remembered to issue.

    Committing is the caller's: the CLI's tenant session commits when it
    closes, and a test's fixture owns its own transaction. This flushes on
    ``apply`` so the UPDATE is issued inside the caller's transaction rather
    than at some later moment the caller did not choose.
    """
    out = Applied()
    # EVERY scanned id, not only the ones carrying a code. The rows come back
    # in the same query either way, and the difference is the whole of the
    # ledger-hole report: the 24-day-old holes were `delivered` receipts, so a
    # sweep restricted to codes could never have seen one. Sorted so the
    # chunking is deterministic and a re-run reads the same batches.
    ids = sorted(scan.receipt_ids | set(scan.codes))
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        rows = session.execute(
            select(DeliveryMessage).where(DeliveryMessage.wa_message_id.in_(chunk))
        ).scalars().all()
        for dm in rows:
            wamid = str(dm.wa_message_id)
            out.present.add(wamid)
            if wamid not in scan.codes:
                # Nothing to fill and nothing to count: every counter below is
                # about the code backfill, and widening them would silently
                # change numbers an operator has been reading for weeks.
                continue
            out.matched += 1
            code = scan.codes[wamid]
            if dm.category is not None:
                out.category_present += 1
            elif wamid in scan.categories:
                out.category_recoverable += 1
            if dm.meta_error_code is None:
                out.filled += 1
                out.by_code[code] += 1
                if apply:
                    dm.meta_error_code = code
            elif dm.meta_error_code == code:
                out.already += 1
            else:
                # The live handler wrote something else, from the receipt that
                # won. History does not get to overrule it — see the module
                # docstring — and a silent disagreement would be worse than a
                # loud one.
                out.conflicting += 1
    if apply:
        session.flush()
    return out


@dataclass
class Holes:
    """Receipts with no `delivery_messages` row, triaged into §39's three
    causes. Counts only — the ids are not kept and never printed.

    The buckets are mutually exclusive and ordered, because an operator acts
    on the first line that names him and a total is not actionable:

    ``billed`` — Meta said `billable`, or named a band that is not
    :data:`FREE_BAND`. We were charged for a message our ledger does not
    contain. THIS is the paging line, and the only one.

    ``refused`` — no money on it and Meta gave a reason code: the send was
    declined, so no row was written, which is correct. A row for a send that
    never happened is a bill we were never charged.

    ``unledgered`` — the remainder: free `service` receipts and receipts with
    no pricing at all. Conversational acks, `funnel/flow.py`,
    `whatsapp/samples.py`, the activation refusals — sends no code in this
    tree has ever recorded. A decision, not a defect, and the reason this
    report is split at all: twelve of these fire every day, and a detector
    that pages on them is a detector the operator learns to skip.
    """

    billed: int = 0
    refused: int = 0
    unledgered: int = 0
    by_band: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return self.billed + self.refused + self.unledgered


#: The label a hole with no `pricing` object at all is counted under. Not
#: `service`: «Meta said free» and «Meta said nothing» are different facts and
#: only one of them is a decision we made.
NO_BAND = "(no pricing)"


def ledger_holes(scan: Scan, applied: Applied) -> Holes:
    """Which scanned receipts have no ledger row, and why that is or is not news.

    Pure: it reads the scan and the presence set the sessions built, and
    touches neither. This is deliberately not a query — the presence set is
    the union across every tenant session, and a hole is only a hole when NO
    tenant could see the row.
    """
    holes = Holes()
    for wamid in sorted(scan.receipt_ids - applied.present):
        band = scan.categories.get(wamid)
        holes.by_band[band or NO_BAND] += 1
        if wamid in scan.billable_ids or (band is not None and band != FREE_BAND):
            holes.billed += 1
        elif wamid in scan.codes:
            holes.refused += 1
        else:
            holes.unledgered += 1
    return holes


def _tenant_ids(session: Session) -> list[str]:
    from career.db.session import tenant_ids_with_status

    return tenant_ids_with_status(session, sorted(ALL_STATES))


def _line(label: str, value: object) -> None:
    print(f"  {label:.<34}: {value}")


def _report(scan: Scan, applied: Applied, *, apply: bool) -> None:
    unmatched = len(scan.codes) - applied.matched
    print("APPLIED" if apply else "DRY RUN — nothing was written")
    _line("whatsapp webhook events read", scan.events)
    _line("…already redacted (0024)", scan.redacted)
    _line("status objects in them", scan.status_objects)
    _line("distinct ids carrying a code", len(scan.codes))
    _line("ledger rows matched", applied.matched)
    _line("filled" if apply else "would be filled", applied.filled)
    _line("…already correct", applied.already)
    _line("…carrying a different code", applied.conflicting)
    _line("codes with no ledger row", unmatched)
    for code, count in sorted(scan.code_histogram.items()):
        _line(f"    code {code}", count)
    print("  billed category — measured here, written by nothing here")
    _line("    ids whose payload names one", len(scan.categories))
    _line("    matched rows already stamped", applied.category_present)
    _line("    matched rows recoverable", applied.category_recoverable)
    for name, count in sorted(scan.category_histogram.items()):
        _line(f"    band {name}", count)
    if unmatched:
        print("  NOTE: an unmatched code is a refusal with no ledger row, or a "
              "tenant this role could not enumerate — see --tenant.")

    holes = ledger_holes(scan, applied)
    print("  ledger holes — receipts Meta sent us with no delivery_messages row")
    _line("    status ids in the receipts", len(scan.receipt_ids))
    _line("    …found on a ledger row", len(applied.present))
    _line("    …with no row at all", holes.total)
    _line("    BILLED, no row — PAGE THIS", holes.billed)
    _line("    refused send, no row — correct", holes.refused)
    _line("    free ack — no code records it", holes.unledgered)
    for band, count in sorted(holes.by_band.items()):
        _line(f"      hole band {band}", count)
    if holes.billed:
        # The only line in this report that is an incident. Phrased as what to
        # do rather than as what was counted, because a count is not an
        # instruction and this one has a customer behind it — and it says in
        # the same breath what NOT to do, since «fill in the missing row» is
        # the first thing the reader will reach for.
        print("  ACT: a message Meta charged us for has no ledger row. That "
              "customer's day is not trustworthy.")
        print("       The row is NOT to be reconstructed here: the receipt "
              "carries no kind and no template_name,")
        print("       so a repair row is a guess nothing downstream could tell "
              "from a fact.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="write the codes. Without it nothing is assigned at all.",
    )
    parser.add_argument(
        "--tenant", action="append", default=[], metavar="UUID",
        help="restrict to this tenant (repeatable). Default: every tenant "
             "with a subscription row.",
    )
    args = parser.parse_args(argv)

    install_secret_redaction()

    from career.db.session import sweep_session, tenant_session

    with sweep_session() as session:
        scan = scan_payloads(session)
        tenants = args.tenant or _tenant_ids(session)

    applied = Applied()
    for tenant_id in tenants:
        with tenant_session(str(tenant_id)) as scoped:
            applied.absorb(apply_codes(scoped, scan, apply=args.apply))

    _report(scan, applied, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
