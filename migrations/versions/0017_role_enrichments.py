"""role_enrichments — thin-role enrichment ledger (F-ENRICH, CHANGELOG §13)

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-20

The once-ever guarantee for the post-activation enrichment nudge: one row per
(tenant, role fact). Presence of a row ⟹ never ask that role again, across
every nightly run and the sweep fallback. FORCE-RLS like every tenant table.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
_TENANT_GUC = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "role_enrichments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("profile_facts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),   # ASKED|ENRICHED|SKIPPED
        sa.Column("trigger", sa.String(length=32), nullable=False),  # lazy_generation|post_activation_sweep
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "fact_id",
                            name="uq_role_enrichments_tenant_fact"),
    )
    op.execute("ALTER TABLE role_enrichments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE role_enrichments FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY role_enrichments_tenant_isolation ON role_enrichments "
        f"USING (tenant_id = {_TENANT_GUC}) WITH CHECK (tenant_id = {_TENANT_GUC})"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON role_enrichments TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.drop_table("role_enrichments")
