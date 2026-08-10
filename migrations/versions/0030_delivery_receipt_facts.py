"""delivery_messages records what Meta's receipt already told us: the refusal
code, and the category the send was billed at

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-10

Two facts arrive on every WhatsApp status callback, on the same
`wa_message_id`, and both were read and thrown away. They are unjoinable
today, and both are money.

── 1. meta_error_code ───────────────────────────────────────────────────────
`worker._handle_status` now READS Meta's `errors` array and logs the code
(131049 per-user marketing cap, 131050 recipient switched «Offers and
announcements» off, 131047 re-engagement required), which reaches the
operator's error screen the same night. That was the right first move and it
is not enough, for a reason with a date on it:

* the log line is EPHEMERAL — it lives in the journal and is rotated;
* `webhook_events.payload`, where the same array also lives, is REDACTED
  after 30 days (0024). The redaction is not optional: those payloads carry
  the customer's phone and his own words.

So the durable answer to «how many of our expiries were Meta REFUSING rather
than the customer going quiet» has a 30-day fuse on it. That number is not
trivia: two of the six historical expiries were refusals, and the 72-hour
start guarantee — a financial promise — is priced from the expiry count. This
column is the only place the answer survives the redaction.

NULLABLE, NO DEFAULT, and both halves of that are deliberate. The column is
meaningful only for a status that carried an `errors` array; a default of 0
would read as a Meta code, and NOT NULL would force one. NULL here means «this
row was never told a reason» — the state of every send that simply worked, and
of every row written before this migration.

The partial index is `WHERE meta_error_code IS NOT NULL`. It buys nothing
measurable today (hundreds of rows) and is kept anyway, on two grounds that
are not speed-today: the query it serves is the one the guarantee is priced
from and will be run against a table that grows with every message ever sent;
and because it is PARTIAL it indexes only the rare refusal, so the ordinary
send — which is almost all of them — adds no index entry when it is written.
An unqualified index on a column that is NULL 99% of the time would have been
the wrong shape and is what «add an index» usually means.

── 2. category ──────────────────────────────────────────────────────────────
`cv/close.whatsapp_spend` prices a historical row by asking
`templates.billed_category` — TODAY's measurement of that template's category
at Meta. Meta re-categorised five of the eight live templates on 2026-07-17
and again on 2026-08-02, so every row sent before those dates is now priced at
a category it was not billed at. `close._wa_kind_of` documents that limit
honestly and names this column as the fix.

The consequence is worse than «some rows are mispriced». `rollup_costs` is
idempotent in its WRITE (SET, not increment) but not in its VALUE: re-running
it over a day from before a re-categorisation writes a different number than
it wrote that day, with nothing in the row to say which reading it is. A
ledger whose past changes when a third party changes its mind is not a ledger.

The category of a send is a fact ABOUT THAT SEND — exactly like `status`,
which nobody would dream of re-deriving from the template registry — so it
belongs on the row.

WHERE THE VALUE COMES FROM, because this is the part that decides whether the
column is worth its migration: **Meta's own delivery receipt.** The status
callback carries `statuses[].pricing`, and `pricing.category` is what Meta
BILLED, not what we believed we would be billed. `worker._handle_status`
already has that payload open, already matches it to this row by
`wa_message_id`, and already writes to it — so the same handler that stops
discarding the error code stops discarding the price band, and the whole
change is one migration and one handler.

MEASURED / NOT MEASURED, stated plainly so nobody reads more into this than
was checked: that Meta's status object carries a `pricing` object is provider
documentation, and whether THESE payloads carry `pricing.category` was NOT
verified against live data in this change (staging was unreadable from where
it was written). The handler therefore records it WHEN PRESENT and does
nothing at all when it is absent, and `close._wa_kind_of` falls back to the
existing `billed_category` lookup on NULL. A column that stays NULL is exactly
today's behaviour and never a worse one — this migration cannot regress the
bill, it can only stop it drifting.

The complementary half belongs to another owner and is one line: `record_out`
in `whatsapp/delivery.py` is the single door onto this table and already
receives `template_name`, so stamping `billed_category(template_name)` at send
time would fill the column for sends whose receipt never carries a price band.
That value is a BELIEF (today's measurement, MARKETING when unmeasured) where
the receipt's is a MEASUREMENT, which is why the receipt must overwrite it and
not the other way round — and why the receipt half ships first.

32 characters: Meta's category vocabulary is wider than our two words —
`authentication_international` is 28 — and a value the column cannot hold
would raise DataError on the status webhook and lose the whole callback, which
is a far worse failure than an unrecognised category. The handler clamps to
the column's own declared length as a second guard.

── the lock profile ─────────────────────────────────────────────────────────
ONE lock acquisition, not three. Alembic runs the whole migration inside one
transaction, so the ACCESS EXCLUSIVE that the first ADD COLUMN takes on
`delivery_messages` is HELD until commit: the second ADD COLUMN queues behind
nothing, and the CREATE INDEX — which would need only SHARE — is already
covered by something stronger.

Neither ADD COLUMN rewrites or scans the table: both are nullable with no
default, so Postgres writes a `pg_attribute` row and stops. The CREATE INDEX
does scan the heap (a partial index still has to look at every row to find the
qualifying ones), but it builds an EMPTY index — the column it indexes was
created three statements earlier in this same transaction, so every row's
value is NULL — and the table is hundreds of rows.

The risk here is queueing, not duration, and it is the same one 0028 named: an
ACCESS EXCLUSIVE request parked behind a long-running SELECT blocks every
reader that arrives after it, and this table is read by the operator's
business screen and written by the worker on every status callback.
`lock_timeout = 3s` as in 0021–0029, so this aborts and is retried rather than
stalling the estate.

── downgrade ───────────────────────────────────────────────────────────────
Drops the index and both columns. It is NOT free of consequence and saying so
is the point: DROP COLUMN destroys every recorded code and category. Within 30
days they can be recovered from `webhook_events.payload` by
`scripts/backfill_meta_error_codes.py`; after the redaction window they cannot
be recovered at all, from anywhere. The index is dropped explicitly even
though DROP COLUMN would take it along, so the two statements can be read in
the order they happen.

NOT A BACKFILL. This migration adds structure and writes not one row. The two
rows that history can fill are filled by `scripts/backfill_meta_error_codes.py`
— a separate, re-runnable, dry-run-by-default script — because a migration
that also backfills is a migration that cannot be re-run and cannot be
reviewed as data.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Partial on purpose — see the module docstring. Only a refused send is
#: indexed, so the ordinary send pays nothing to write.
_REFUSAL_INDEX = "ix_delivery_messages_meta_error_code"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    op.add_column(
        "delivery_messages",
        sa.Column("meta_error_code", sa.Integer(), nullable=True),
    )
    op.add_column(
        "delivery_messages",
        sa.Column("category", sa.String(length=32), nullable=True),
    )
    op.execute(
        f"CREATE INDEX {_REFUSAL_INDEX} ON delivery_messages (meta_error_code) "
        "WHERE meta_error_code IS NOT NULL"
    )

    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    # Postgres would drop this with the column; it is named here so the order
    # of events is readable rather than implied.
    op.execute(f"DROP INDEX IF EXISTS {_REFUSAL_INDEX}")
    op.drop_column("delivery_messages", "category")
    op.drop_column("delivery_messages", "meta_error_code")

    op.execute("RESET lock_timeout")
