"""delivery_messages.channel_id becomes optional — the template spend that
could not be written down

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-06

An audit measured the hole rather than guessing at it: five approved-template
send sites record nothing in `delivery_messages`, and `close.whatsapp_spend`
derives the entire WhatsApp bill from that table. Per subscription per 30-day
period the unbilled share is 14% for a renewing customer, 18% in their first
month, 25% for one lapsing — and **100% for an order that is paid and never
activated**, because the only template that order ever receives is one of the
two this migration unblocks. Since delivery moved from dawn to 11:00 most
bundles land free-form inside the open 24h window and cost nothing, so the
lifecycle templates are now the MAJORITY of billed template traffic, and none
of it was counted.

Three of the five sites needed no schema change at all: `lifecycle._live_channel`
already loaded the whole `CustomerChannel` and discarded everything but the
phone, and `_send_template` already received a message id from the client and
threw it away. Both are now kept and one INSERT is written.

The other two could not be fixed in code, and the reason is structural:
`channel_id` is NOT NULL, and both sends happen BEFORE any CustomerChannel
exists.

* the zero-touch welcome (`provisioning._announce_provision`) — the buyer's
  REPLY to that template is what creates the channel, so demanding a channel to
  record it is demanding the effect before the cause;
* the day-5 claim reminder (`lifecycle.sweep_subscription_lifecycle`), which
  chases the same order on the same order phone for the same reason.

So the column becomes nullable. Nothing else in the system reads it in a way
this can break: `whatsapp_spend` groups by `template_name` and filters on
`kind`, `status` and `tenant_id` — it never mentions `channel_id`; the Meta
status callback in `whatsapp/worker` looks a row up by `wa_message_id`; the §12
export renders `kind`, `template_name`, `status`, `created_at`; the deletion
sweep keys on `tenant_id`. The foreign key is untouched, so a row that DOES
have a channel still carries it and still cascades with it.

Expected lock profile: one ALTER TABLE … DROP NOT NULL. That is a catalog
update — `pg_attribute.attnotnull` flipped to false — with **no rewrite, no
scan and no index build**; dropping a constraint can never invalidate existing
rows, so Postgres has nothing to verify. It takes ACCESS EXCLUSIVE on
`delivery_messages` for the duration of that catalog write (sub-millisecond).
The risk is queueing, not duration: an ACCESS EXCLUSIVE request parked behind a
long-running SELECT blocks every reader that arrives after it, and this table
is read by the operator's business screen. `lock_timeout = 3s` as in 0021–0027,
so the migration aborts and can be retried instead of stalling the worker loop.

Downgrade is honest rather than convenient. `SET NOT NULL` re-validates by
scanning the table (there is no NOT VALID form for a not-null on a plain
column), and it FAILS if any of the rows this migration exists to permit are
present. That is the correct behaviour: the alternative — deleting billing
records so a schema change can proceed — would destroy the very spend this
migration was written to make visible. If the downgrade is genuinely wanted,
the operator must decide what to do with those rows first, and the error names
the column.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("ALTER TABLE delivery_messages ALTER COLUMN channel_id DROP NOT NULL")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    # Scans the table and raises 23502 if a pre-channel template row exists —
    # see the module docstring; that refusal is the point.
    op.execute("ALTER TABLE delivery_messages ALTER COLUMN channel_id SET NOT NULL")
    op.execute("RESET lock_timeout")
