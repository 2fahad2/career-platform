"""outcome_events — the customer's «قدّمت/تجاهل» feedback (whitepaper §08/§14)

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-17

Append-only measurement fuel: every delivered job can receive one or more
outcome events from the customer's buttons. FORCE RLS like every
tenant-scoped table; the app role gets INSERT/SELECT only (append-only —
feedback is never rewritten).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outcome_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "delivery_id", UUID(as_uuid=True),
            sa.ForeignKey("deliveries.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("job_ref", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_outcome_events_tenant_id_occurred_at",
        "outcome_events", ["tenant_id", "occurred_at"],
    )
    op.execute("ALTER TABLE outcome_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outcome_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY outcome_events_tenant_isolation ON outcome_events
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT ON outcome_events TO career_app")


def downgrade() -> None:
    op.drop_table("outcome_events")
