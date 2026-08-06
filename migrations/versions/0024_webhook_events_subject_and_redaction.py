"""webhook_events gets a subject and an expiry date for its raw body

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-06

The oldest unclosed compliance item. `webhook_events` stores the provider's
body exactly as it arrived: from Salla the buyer's name, mobile and email;
from Meta the customer's phone, their profile name and the text they typed. It
had no tenant column at all, so it was in none of the machinery that exists to
honour §12 — not in `_PERSONAL_DELETION_ORDER`, not in `RETAINED_TABLES`, not
in the export bundle, and no pruning anywhere. A customer who sent «حذف
بياناتي» had their profile, facts, messages and channels deleted while the raw
bodies that carried the same sentences sat untouched beside them, and the
nightly backup carried those bodies for up to six months. Measured on staging
on 6 أغسطس: 13 rows carrying a message body, 62 carrying a profile name, 272
carrying a wa_id.

Two columns close it.

`subject_tenant_id` is the tenant the body is ABOUT. Not `tenant_id`: this
table is deliberately outside RLS — intake happens before the tenant is known
and the worker legitimately scans across tenants for its queue — and the RLS
meta-test keys on a column literally named `tenant_id`, so that name would
demand a policy that would break the intake it is trying to protect. The
precedent is `break_glass_log.target_tenant_id` (0021), which is outside RLS
for the same kind of reason. It is nullable because it is often genuinely
unknown: an order webhook arrives BEFORE the tenant it will create exists, an
`app.store.authorize` install hook is about the store and not about any
customer, and a single Meta POST can batch two customers' messages, in which
case attributing it to one of them would both under-delete and over-disclose.
Those rows keep NULL and are protected by the clock instead of by the link.

`payload_redacted_at` is when the raw body was replaced by a PII-free
skeleton. Redaction, not deletion, because the row is also the idempotency
record: `event_fingerprint` is what makes a replayed webhook a no-op, and a
Salla retry of an order whose fingerprint we had thrown away would provision
the same customer a second time. Keeping the fingerprint and dropping the body
keeps the guarantee and drops the data — and note that `career_app` holds
INSERT/SELECT/UPDATE on this table and no DELETE, so redaction is the only
erasure the application role could perform here anyway.

No foreign key on `subject_tenant_id`. Adding one means validating it, which
means the scan this migration promises not to do, and `NOT VALID` would be a
constraint that lies until somebody remembers to validate it. The column is a
link for erasure, not a referential guarantee: the worst a stale id can do is
make one redaction pass miss a row that the retention window then redacts on
its own schedule.

No index either, and that is a decision rather than an omission. The table is
276 rows and 592 kB — the whole of it is read faster than an index lookup
plans — and `CREATE INDEX` takes a SHARE lock and scans, which is exactly the
cost this migration is written to avoid. The prune's predicate
(`payload_redacted_at IS NULL AND received_at < …`) is the right shape for a
partial index the day the table crosses a million rows, and that day it should
be built CONCURRENTLY, outside a migration, not smuggled in here now.

Expected lock profile: ONE `ALTER TABLE` adding two nullable columns with no
default. ACCESS EXCLUSIVE held for the catalog update only — no table rewrite,
no scan, no default to backfill — so the duration is microseconds and the only
risk is waiting for the lock rather than holding it. `lock_timeout` is set for
the same reason as 0021, 0022 and 0023: the intake writes to this table on
every webhook, so the migration aborts rather than queues behind a reader and
blocks the queue behind itself.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "ALTER TABLE webhook_events "
        "ADD COLUMN subject_tenant_id uuid, "
        "ADD COLUMN payload_redacted_at timestamptz"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "ALTER TABLE webhook_events "
        "DROP COLUMN subject_tenant_id, "
        "DROP COLUMN payload_redacted_at"
    )
    op.execute("RESET lock_timeout")
