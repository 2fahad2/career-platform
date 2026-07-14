"""transactional outbox + message idempotency

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-14

- outbox_events: events written in the SAME transaction as the business state
  change (transactional outbox). ENABLE-only RLS: the app writes confined by
  WITH CHECK to its tenant, while the relay runs as the owner role and scans
  unpublished events across all tenants to publish them.
- processed_messages: idempotency ledger. A (tenant_id, idempotency_key) that
  is already present makes a redelivered message a no-op. ENABLE + FORCE, since
  it is always written by the worker under a verified tenant context.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
_TENANT_GUC = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
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
    # Partial index for the relay's "unpublished" scan.
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

    # outbox_events: ENABLE-only so the owner-role relay can scan all tenants.
    op.execute("ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY outbox_tenant_isolation ON outbox_events
            USING (tenant_id = {_TENANT_GUC})
            WITH CHECK (tenant_id = {_TENANT_GUC})
        """
    )

    # processed_messages: ENABLE + FORCE — always written under a verified tenant.
    op.execute("ALTER TABLE processed_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE processed_messages FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY processed_messages_tenant_isolation ON processed_messages
            USING (tenant_id = {_TENANT_GUC})
            WITH CHECK (tenant_id = {_TENANT_GUC})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS processed_messages_tenant_isolation ON processed_messages")
    op.execute("DROP POLICY IF EXISTS outbox_tenant_isolation ON outbox_events")
    op.drop_table("processed_messages")
    op.execute("DROP INDEX IF EXISTS ix_outbox_events_unpublished")
    op.drop_table("outbox_events")
