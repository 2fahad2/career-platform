"""initial schema with row-level security foundation

Revision ID: 0001
Revises:
Create Date: 2026-07-13

Establishes the tenant registry and the first tenant-scoped table (documents),
then locks them down with Row-Level Security (§15.10). Isolation rests on:
  * The application connecting as career_app — a non-superuser role granted only
    CRUD — so RLS applies to it (the owner bypasses RLS and provisions tenants).
  * Policies keyed on the transaction-local GUC app.tenant_id (read AND write).
  * documents additionally FORCEd for catalog uniformity. NOTE (D16, audit
    2026-07-24): the deployed owner role is a SUPERUSER and bypasses RLS
    regardless of FORCE — real isolation rests on the restricted career_app
    role; tenants is ENABLE-only so the owner can seed the registry.
The career_app role itself is created by the Postgres init script
(docker/initdb/10-app-role.sh) before migrations run.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("code", name="uq_tenants_code"),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_documents_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id", "storage_key", name="uq_documents_tenant_id_storage_key"
        ),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])

    # --- Grants: the app role gets CRUD, never ownership, never DDL. ---
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO {APP_ROLE}")

    # --- Row-Level Security ---
    # tenants: registry managed by the owner/provisioning role (which bypasses
    # RLS as table owner), while the app role sees only its own row. ENABLE (not
    # FORCE) so the owner can seed/provision tenants; the app role is a
    # non-superuser, so RLS still applies to it.
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    # NULLIF(..., '') is essential: a custom GUC, once set on a connection and
    # reset at transaction end, reverts to '' (not NULL) on pooled connections.
    # Casting ''::uuid would raise; NULLIF makes an unset tenant fail closed.
    op.execute(
        """
        CREATE POLICY tenant_self_isolation ON tenants
            USING (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )

    # documents: every row confined to its tenant, on read AND write.
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY document_tenant_isolation ON documents
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS document_tenant_isolation ON documents")
    op.execute("DROP POLICY IF EXISTS tenant_self_isolation ON tenants")
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    op.drop_table("documents")
    op.drop_table("tenants")
