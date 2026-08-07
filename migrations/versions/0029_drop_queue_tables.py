"""drop outbox_events and processed_messages — the queue subsystem is gone

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-06

The transactional outbox and the queue it fed were deleted this week (see
DEVIATIONS D19: the worker loop polls `webhook_events` directly and dedupes on
`inbound_messages.wa_message_id`). Both tables are now referenced by nothing:
no writer, no reader, no sweep. What was left behind is worse than dead weight
— they carry RLS policies, column grants and a FORCE flag that the meta-tests
keep asserting, so every future reader of the schema has to work out that the
most heavily-defended pair of tables in the database is also the pair nobody
uses.

Precondition, checked before this file was written rather than asserted after:
`SELECT count(*)` on both in career_staging returned **0**. Nothing is being
destroyed; if a future environment disagrees, the operator finds out from the
count and not from the drop.

`app.tenants_with_pending_work()` is rebuilt in the same migration, and that is
the part that would have bitten. 0021 defines it as a three-branch UNION whose
first branch is `SELECT tenant_id FROM outbox_events WHERE published_at IS
NULL`. A LANGUAGE-sql function with a string body carries no catalog dependency
on the tables it names, so dropping `outbox_events` does NOT fail and does NOT
warn — the function simply starts raising «relation "outbox_events" does not
exist» the next time anything calls it, which is the retention sweep's «is this
tenant still live?» question. It is redefined FIRST, so there is no instant at
which the function is broken, and `CREATE OR REPLACE` keeps the owner
(career_sweep), the EXECUTE grant to career_app and the REVOKE from PUBLIC that
0021 attached to it. The two surviving branches — an unfinished onboarding
journey, an incomplete delivery — are the whole of «pending work» now.

Expected lock profile: one CREATE OR REPLACE FUNCTION (a pg_proc row, no table
touched) and two DROP TABLEs. Each DROP takes ACCESS EXCLUSIVE on its own
table, which is uncontended by construction — nothing has opened either of them
since the queue was deleted. The lock that matters is on `tenants`: dropping a
table removes its foreign key, and removing an FK takes a brief ACCESS
EXCLUSIVE on the REFERENCED table, so this migration momentarily locks the one
table every process reads. No rewrite and no scan: the work is catalog-only and
the files are unlinked at commit. `lock_timeout = 3s` as in 0021–0028, so a
request that would queue behind a long-running reader of `tenants` aborts and
is retried instead of blocking every reader that arrives after it.

Downgrade restores both tables exactly as 0002 built them, plus the FORCE that
0018 added and the policy expression + sweep capability that 0021 rewrote —
i.e. the catalog state as it actually stood the moment before this migration,
not as 0002 left it. It cannot restore rows, and there were none.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
SWEEP_ROLE = "career_sweep"
#: The fail-closed expression every tenant policy has carried since 0021.
_GUC = "app.current_tenant()"

#: The function without its outbox branch. Same signature, same volatility,
#: same SECURITY DEFINER + search_path — replacing any of those would be a
#: different function wearing the same name.
_PENDING_WORK_WITHOUT_OUTBOX = """
CREATE OR REPLACE FUNCTION app.tenants_with_pending_work() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
    SELECT tenant_id FROM onboarding_sessions WHERE completed_at IS NULL
    UNION
    SELECT tenant_id FROM deliveries WHERE completed_at IS NULL
$$
"""

#: 0021's definition, restored verbatim by downgrade().
_PENDING_WORK_WITH_OUTBOX = """
CREATE OR REPLACE FUNCTION app.tenants_with_pending_work() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
    SELECT tenant_id FROM outbox_events WHERE published_at IS NULL
    UNION
    SELECT tenant_id FROM onboarding_sessions WHERE completed_at IS NULL
    UNION
    SELECT tenant_id FROM deliveries WHERE completed_at IS NULL
$$
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    # FIRST — see the module docstring. The function must never name a table
    # that is not there, not even for the length of one migration.
    op.execute(_PENDING_WORK_WITHOUT_OUTBOX)

    # Policies, grants and indexes belong to the table and go with it; naming
    # them here would only be a list to keep in step with the catalog.
    op.drop_table("processed_messages")
    op.drop_table("outbox_events")

    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_outbox_events_tenant_id_tenants", ondelete="CASCADE",
        ),
    )
    op.execute(
        "CREATE INDEX ix_outbox_events_unpublished ON outbox_events (created_at) "
        "WHERE published_at IS NULL"
    )

    op.create_table(
        "processed_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_processed_messages"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_processed_messages_tenant_id_tenants", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_processed_messages_tenant_id_idempotency_key",
        ),
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON processed_messages TO {APP_ROLE}")

    # 0002 left outbox_events ENABLE-only for the owner-role relay; 0018 added
    # FORCE so the RLS meta-test's catalog invariant held uniformly.
    op.execute("ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE processed_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE processed_messages FORCE ROW LEVEL SECURITY")

    # The 0021 expression, not the 0002 one: a downgrade that restored the old
    # silently-empty GUC on two tables and left it fail-closed on twenty-eight
    # would be a hole nobody would think to look for.
    op.execute(
        f"""
        CREATE POLICY outbox_tenant_isolation ON outbox_events
            USING (tenant_id = {_GUC}) WITH CHECK (tenant_id = {_GUC})
        """
    )
    op.execute(
        f"""
        CREATE POLICY processed_messages_tenant_isolation ON processed_messages
            USING (tenant_id = {_GUC}) WITH CHECK (tenant_id = {_GUC})
        """
    )

    # 0021's sweep capability over outbox_events: two columns, SELECT only.
    op.execute(
        f"GRANT SELECT (tenant_id, published_at) ON outbox_events TO {SWEEP_ROLE}"
    )
    op.execute(
        f"CREATE POLICY outbox_events_sweep_read ON outbox_events "
        f"FOR SELECT TO {SWEEP_ROLE} USING (true)"
    )

    # Last, and only now that the table it names exists again.
    op.execute(_PENDING_WORK_WITH_OUTBOX)

    op.execute("RESET lock_timeout")
