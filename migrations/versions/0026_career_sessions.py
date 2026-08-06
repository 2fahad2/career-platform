"""career_sessions — the لمّاح+ session gets a ledger instead of a memory

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-06

لمّاح+ sells «**جلسة مسار** واحدة متى طلبتها» for 449 riyals a month, and
before this table `grep 'جلسة|career_session|session_request'` returned
nothing at all. Not a partial implementation — no record that a customer had
asked, no way to see one waiting, and no record afterwards that a session had
been delivered.

That last one is not bookkeeping. The refund page promises «تُخصم قيمة
الخدمات البشرية اللي استلمتها فعليًا (جلسة المسار ١٥٠ ريالًا) — ونعرض عليك
الأرقام قبل موافقتك», so a refund for a لمّاح+ customer is computed from
whether a session actually happened. With nowhere to write that down, the
figure came from somebody's memory, and «ونعرض عليك الأرقام» is a promise you
cannot keep from memory.

The session itself stays human — nothing here books a time or sends an
invite; the arrangement happens in the same WhatsApp conversation the tier is
sold on. What the table adds is the ledger and the visibility, plus the thing
a human promise actually needs: an escalation. `escalated_at` is stamped when
a request outlives the tier's own «الرد خلال ٢٤ ساعة» and is raised as a
`support_events` ticket, so it joins the queue the operator already works
instead of sitting in a table nobody opens. Silence, not refusal, is how this
kind of promise dies.

`uq_career_sessions_live_per_subscription` is where «واحدة» actually lives. It
is partial — `WHERE status <> 'CANCELED'` — for a reason worth stating: a
cancelled arrangement did not consume the entitlement, so the customer may
ask again, while a REQUESTED, SCHEDULED or COMPLETED one means this period's
session is spoken for. Keying it on `subscription_id` makes «one per
subscription period» true with no date arithmetic anywhere, because since §16
every renewal is its own row per order. A unique index rather than a check in
code: the rule survives a caller who forgets it.

Expected lock profile: one CREATE TABLE plus one index build on the empty
table it just created — no scan, no rewrite, nothing to queue behind. The two
foreign keys take a brief SHARE ROW EXCLUSIVE on `tenants` and
`subscriptions`, validated against an empty child. `lock_timeout` is set as in
0021–0025 so the migration aborts rather than parking a lock request in front
of the live workers' readers.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
_GUC = "app.current_tenant()"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "career_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "subscription_id", UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_career_sessions_tenant_id", "career_sessions",
                    ["tenant_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_career_sessions_live_per_subscription "
        "ON career_sessions (subscription_id) WHERE status <> 'CANCELED'"
    )
    op.execute("ALTER TABLE career_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE career_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY career_sessions_tenant_isolation ON career_sessions
        USING (tenant_id = {_GUC}) WITH CHECK (tenant_id = {_GUC})
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON career_sessions TO {APP_ROLE}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("DROP POLICY IF EXISTS career_sessions_tenant_isolation "
               "ON career_sessions")
    op.drop_table("career_sessions")
    op.execute("RESET lock_timeout")
