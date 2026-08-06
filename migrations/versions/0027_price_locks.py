"""price_locks — the founder price lock, and the outage its absence was

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-06

The founding-seat block promises «سعره اليوم مقفول له: لو ارتفعت الأسعار يوم
من الأيام، ما ترتفع عليه — ما دام تجديده مستمر». `grep price_lock` returned
zero, and the gap was worse than a missing feature.

§09 binds a paid order with a triple match — product, amount, currency —
against ONE global expected price per product, held in the environment. Raise
the store price and the founder who renews at their locked old amount fails
that match. `provision_order` returns AMOUNT_MISMATCH and marks the webhook
`failed`, which is TERMINAL: no retry path reads a failed event, no sweep
revisits it. The founder pays, gets no subscription, no activation link and no
service, and the only trace is an operator alert saying our own pricing looks
misconfigured. The day prices rise, every founder renewal breaks that way at
once. The promise inverted into the most expensive failure the payment path
has.

This table is the fix's memory: what a customer bought a pass at, captured the
first time they buy it and ONLY after the triple match has already agreed the
amount is today's real price. That ordering is the whole basis for trusting it
later — a lock is never captured from an amount no guard has verified.

The relaxation it enables is deliberately narrow, and the shape of the table
is what keeps it narrow. The lock belongs to a specific tenant and a specific
plan, resolved from the order phone through the same identity the renewal path
uses — never from anything a buyer can type into an order. It carries ONE
exact amount, so an order matching neither the current price nor that tenant's
locked price still fails closed. `plan_code` references `plan_entitlements`,
so a lock cannot exist for a product that is not a pass — «التقييم ما يحسب
كرسي».

`lapsed_at` is the published continuity rule made durable: «"تجديد مستمر"
يعني: تجدد قبل نهاية اشتراكك أو خلال ٧ أيام بعده», and PRODUCTS-SHEET finishes
it — «انقطعت أكثر؟ الكرسي ينفتح لغيرك، وترجع بسعر يومها». A lapsed lock is
stamped, never deleted, because «why did his price change?» has to stay
answerable a year later. Note this window is deliberately TIGHTER than the
lifecycle's own: a subscription graces for 48 hours, expires, and is still
sent a recovery nudge seven days after that. They are welcome back either way;
what they lose after seven days is the locked price, which is the only thing
the sentence promised.

No unique constraint on (tenant_id, plan_code) even though the code keeps one
live lock per pair: a lapsed lock and its successor must be able to coexist,
which is precisely the history the column exists to keep. Uniqueness that
would have to be partial on `lapsed_at IS NULL` was considered and left out —
the write path is a single provisioning worker that reads before it writes,
and an index whose only job is to stop a race that has no second writer is
cost without a reader.

Expected lock profile: one CREATE TABLE plus one index on the empty table it
creates. Three foreign keys, each validated against an empty child, taking a
brief SHARE ROW EXCLUSIVE on `tenants`, `subscriptions` and
`plan_entitlements` — no scan of any parent, no rewrite. `lock_timeout` as in
0021–0026.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
_GUC = "app.current_tenant()"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        "price_locks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "plan_code", sa.String(length=32),
            sa.ForeignKey("plan_entitlements.plan_code"), nullable=False,
        ),
        sa.Column("amount_sar", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_subscription_id", UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("lapsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_price_locks_tenant_id", "price_locks", ["tenant_id"])
    op.execute("ALTER TABLE price_locks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE price_locks FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY price_locks_tenant_isolation ON price_locks
        USING (tenant_id = {_GUC}) WITH CHECK (tenant_id = {_GUC})
        """
    )
    # SELECT and INSERT only, plus the UPDATE that stamps `lapsed_at`. No
    # DELETE: a price a customer was promised is not something the
    # application may make disappear.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON price_locks TO {APP_ROLE}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("DROP POLICY IF EXISTS price_locks_tenant_isolation "
               "ON price_locks")
    op.drop_table("price_locks")
    op.execute("RESET lock_timeout")
