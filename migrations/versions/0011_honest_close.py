"""usage_events + cost_allocations + tenant_day_states (whitepaper §14/§15.12)

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-17

The honest-close layer: raw per-call usage (append-only), the per-tenant/day
cost rollup, and the ONE honest daily state per tenant (constant §15.12 —
never a silent success). All FORCE RLS; usage_events and tenant_day_states
also matter to the owner-side runner, which bypasses RLS by role.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_usage_events_tenant_id_occurred_at",
        "usage_events", ["tenant_id", "occurred_at"],
    )
    _rls("usage_events")
    op.execute(f"GRANT SELECT, INSERT ON usage_events TO {APP_ROLE}")

    op.create_table(
        "cost_allocations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "tenant_id", "day", "category",
            name="uq_cost_allocations_tenant_day_category",
        ),
    )
    _rls("cost_allocations")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON cost_allocations TO {APP_ROLE}")

    op.create_table(
        "tenant_day_states",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("counts", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_date", name="uq_tenant_day_states_tenant_run_date"
        ),
    )
    _rls("tenant_day_states")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON tenant_day_states TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("tenant_day_states")
    op.drop_table("cost_allocations")
    op.drop_table("usage_events")
