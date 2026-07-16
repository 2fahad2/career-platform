"""funnel_sessions — the CV-analysis product journey (whitepaper §04, C8)

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-17

One row per funnel tenant: a tiny state machine (consents → upload → path →
done), a context cursor, and the stored report JSON. Deliberately separate
from onboarding_sessions — the funnel is not the onboarding and must never
pollute its FSM vocabulary.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"


def upgrade() -> None:
    op.create_table(
        "funnel_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False, unique=True,
        ),
        sa.Column(
            "subscription_id", UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "channel_id", UUID(as_uuid=True),
            sa.ForeignKey("customer_channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("context", JSONB(), nullable=False, server_default="{}"),
        sa.Column("report", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute("ALTER TABLE funnel_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE funnel_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY funnel_sessions_tenant_isolation ON funnel_sessions
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON funnel_sessions TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("funnel_sessions")
