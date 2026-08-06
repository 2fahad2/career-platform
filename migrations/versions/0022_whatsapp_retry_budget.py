"""webhook_events: a retry budget for the WhatsApp worker, and a dead letter

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-06

INCIDENT 2026-07-19: one `whatsapp/messages` event sits in staging with
`processing_status = 'failed'` and has sat there ever since. The WhatsApp
worker marked an event `failed` on ANY exception — a Claude timeout, a Meta
5xx, a dropped database connection — and `failed` is terminal: nothing in this
codebase selects it. The customer's message was not «rejected», it was
DROPPED, and the only way back was an operator running
`scripts/replay_lost_events.py` by hand, which for a `messages` event it
refuses to do anyway (it cannot know whether the worker had already answered
the customer before it rolled back).

The Salla side of the same table solved this in `salla/provisioning.py`:
`_defer_webhook` leaves a retryable failure `received` so the next pass picks
it up, and `_quarantine` is reserved for an event that is genuinely poisoned.
Salla can defer with nothing but a counter because it defers WHOLESALE — the
one condition it retries on (Salla unreachable) applies to every waiting row
at once, so it stops the batch and sleeps the process for a minute. A WhatsApp
event fails ALONE, so leaving it `received` with no clock would put it back in
front of the worker three seconds later, forever: a retry storm, and every
turn of it possibly another Claude call. Hence the two columns this migration
adds — a time before which the row is not eligible, and a written reason.

The columns:

`next_attempt_at` — when the row becomes selectable again. NULL means «now»,
which is what every existing row and every fresh intake row is, so there is no
backfill: the intake in `webhooks/intake.py` keeps inserting exactly the
columns it inserts today and every one of those events is immediately due.

`failure_kind` — the worker's verdict in one PII-free slug (`transient`,
`poison`, `exhausted`, `already_spoke`), so a row that stopped is a row that
says why. `already_spoke` is the one worth naming here: it means the attempt
had already put a message in front of the customer before it failed, so
retrying it would send that message a second time, and we would rather stop
and tell the operator than repeat ourselves at a paying customer.

`failure_detail` — the exception's class name, nothing else. Never the
message: exception strings in this codebase routinely carry a phone number or
a fragment of what the customer wrote, and this row is read by the operator
(constant 13 — TEN codes only, no PII in logs or the admin channel).

Deliberately NOT added: an index. The predicate gains
`(next_attempt_at IS NULL OR next_attempt_at <= :now)` on a table holding 275
rows on staging and growing by a few hundred a week, already scanned without
an index by both workers' existing `provider + processing_status` filter. An
index here would be a cost with no reader. The threshold at which that changes
is roughly a hundred thousand rows — at which point the right index is a
partial one, `(provider, received_at) WHERE processing_status = 'received'`,
and it must be built CONCURRENTLY outside a migration transaction.

Expected lock profile: catalog-only. Three ADD COLUMN, all nullable with no
default, which since PostgreSQL 11 (and always, for a NULL default) writes a
pg_attribute row and touches no heap page — no rewrite, no scan, no row
locked. The whole statement is microseconds. As in 0021 the risk is not
duration but queueing: an ACCESS EXCLUSIVE request parked behind a long
SELECT blocks every later reader, so `lock_timeout` is 3s and the migration
aborts and is retried rather than stalling the worker loop. All three columns
are added in ONE `ALTER TABLE`, so the lock is taken once and not three times.

`webhook_events` has no RLS (an order arrives before the tenant is known) and
its grant to `career_app` is table-level, so the new columns need no GRANT.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "ALTER TABLE webhook_events"
        " ADD COLUMN next_attempt_at timestamptz,"
        " ADD COLUMN failure_kind varchar(32),"
        " ADD COLUMN failure_detail varchar(64)"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "ALTER TABLE webhook_events"
        " DROP COLUMN failure_detail,"
        " DROP COLUMN failure_kind,"
        " DROP COLUMN next_attempt_at"
    )
    op.execute("RESET lock_timeout")
