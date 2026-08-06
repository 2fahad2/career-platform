"""delivery_guarantees — the 72-hour start guarantee gets a clock and a record

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-06

The refund policy page has sold this since launch: «ما وصلتك أول فرصة خلال ٧٢
ساعة من تفعيل اشتراكك؟ استرداد كامل أو تمديد الاشتراك — أنت تختار», and the
guarantee line under every subscription repeats it. Nothing in the codebase
measured it. `grep 72` found an enrichment TTL; there was no clock, no state
on any record, and no signal to the operator when it broke — so the only
alarm the guarantee had was a customer complaining, and a customer who does
not complain simply keeps a broken promise. That is the failure this table
exists to make impossible: it fails in OUR favour and leaves no trace.

One row per tenant, enforced by a unique constraint, because «ضمان البداية»
is a guarantee about the beginning and a customer begins once. Keying it on
the subscription instead would restart the clock at every renewal (§16 gives
each order its own row with its own `current_period_start`) and manufacture a
breach for someone who has been served for months.

`activated_at` is a COPY of `onboarding_sessions.completed_at` — the §05
anchor stamped by `policy.activate` — rather than a reference to it, so the
deadline that a customer is owed against cannot move afterwards. `deadline_at`
is stored rather than computed for the same reason: the number 72 lives in one
place in the code and in one place per row, and a row already judged is judged
against the clock it was judged with.

`facts` is the PII-free packet the operator decides with: which honest day
states the window contained, whether the customer had paused or opted out, how
many weekend days it spanned (§08 delivers Sunday–Thursday while the promise
is written in flat hours — that tension is ours to own, so it is disclosed to
him rather than silently excused by the code). `remedy` records what he
applied. Nothing here refunds or extends on its own: the page says «أنت
تختار», the choice is the customer's, and the money is his to move.

Expected lock profile: one CREATE TABLE, which takes ACCESS EXCLUSIVE on a
relation that does not exist yet — nothing to queue behind, nothing to scan,
no rewrite. The two foreign keys take a brief SHARE ROW EXCLUSIVE on `tenants`
and `subscriptions`; both are validated against an empty child table, so there
is no scan of either parent. `lock_timeout` is set for the same reason as
0021–0024: the API and the workers write these tables continuously, and a
migration should abort and be retried rather than park a lock request in front
of every later reader.

RLS is the default posture for anything carrying `tenant_id` (§15.10) and the
meta-test in `tests/test_rls_meta.py` fails CI if it is forgotten: ENABLE,
FORCE, and one policy calling `app.current_tenant()` — the fail-closed
function 0021 installed, which RAISES when no tenant is bound instead of
quietly returning nothing.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
_GUC = "app.current_tenant()"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "delivery_guarantees",
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
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("first_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remedy", sa.String(length=16), nullable=True),
        sa.Column("remedy_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("facts", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", name="uq_delivery_guarantees_tenant_id"),
    )
    op.execute("ALTER TABLE delivery_guarantees ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE delivery_guarantees FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY delivery_guarantees_tenant_isolation ON delivery_guarantees
        USING (tenant_id = {_GUC}) WITH CHECK (tenant_id = {_GUC})
        """
    )
    # No DELETE, like every other ledger the app role touches: a guarantee row
    # is the evidence of what was promised and what was done about it.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON delivery_guarantees TO {APP_ROLE}"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("DROP POLICY IF EXISTS delivery_guarantees_tenant_isolation "
               "ON delivery_guarantees")
    op.drop_table("delivery_guarantees")
    op.execute("RESET lock_timeout")
