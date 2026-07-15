"""engine — shared job pool, discovery runs, per-tenant decisions/suppressions

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-15

Whitepaper §06: discovery is shared (one pool, no RLS — public job-ad data,
written by the owner-role engine, app role SELECT only), evaluation is per
tenant (decisions + suppressions FORCE RLS). Suppression keys reuse the
delivered-key URL identity (§8.1) so dedupe and suppression can never diverge.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
_TENANT_GUC = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _tenant_rls(table: str, policy: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {policy} ON {table} "
        f"USING (tenant_id = {_TENANT_GUC}) WITH CHECK (tenant_id = {_TENANT_GUC})"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False)


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column:
    kwargs: dict[str, object] = {"nullable": nullable}
    if default_now:
        kwargs["server_default"] = sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def _jsonb(name: str, *, nullable: bool = False) -> sa.Column:
    kwargs: dict[str, object] = {"nullable": nullable}
    if not nullable:
        kwargs["server_default"] = sa.text("'{}'::jsonb")
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), **kwargs)


def upgrade() -> None:
    # ── job_postings (the shared pool — system table, app SELECT only) ───────
    op.create_table(
        "job_postings",
        _uuid_pk(),
        sa.Column("url_identity", sa.String(length=80), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("source_native_id", sa.String(length=128), nullable=True),
        sa.Column("job_entity_id", sa.String(length=80), nullable=True),
        sa.Column("cross_source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("repost_group_id", sa.String(length=80), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("company", sa.String(length=256), nullable=False),
        sa.Column("location", sa.String(length=128), nullable=True),
        sa.Column("description_snippet", sa.Text(), nullable=True),
        sa.Column("salary_raw", sa.String(length=256), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        _jsonb("route"),
        _ts("posted_at"),
        _ts("first_seen_at", nullable=False, default_now=True),
        _ts("last_seen_at", nullable=False, default_now=True),
        sa.Column("jd_status", sa.String(length=24), nullable=False,
                  server_default=sa.text("'NOT_REQUESTED'")),
        sa.Column("jd_snippet", sa.Text(), nullable=True),
        _ts("enriched_at"),
        sa.PrimaryKeyConstraint("id", name="pk_job_postings"),
        sa.UniqueConstraint("url_identity", name="uq_job_postings_url_identity"),
    )
    op.create_index("ix_job_postings_cross_source_fingerprint", "job_postings",
                    ["cross_source_fingerprint"])
    op.create_index("ix_job_postings_repost_group_id", "job_postings",
                    ["repost_group_id"])
    op.execute(f"GRANT SELECT ON job_postings TO {APP_ROLE}")

    # ── discovery_runs (system table, app SELECT only) ───────────────────────
    op.create_table(
        "discovery_runs",
        _uuid_pk(),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False,
                  server_default=sa.text("'running'")),
        sa.Column("digest_only", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        _jsonb("counts"),
        _ts("started_at", nullable=False, default_now=True),
        _ts("finished_at"),
        sa.PrimaryKeyConstraint("id", name="pk_discovery_runs"),
    )
    op.execute(f"GRANT SELECT ON discovery_runs TO {APP_ROLE}")

    # ── tenant_job_decisions (FORCE RLS) ─────────────────────────────────────
    op.create_table(
        "tenant_job_decisions",
        _uuid_pk(),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_posting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("near_miss", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        _jsonb("reasons"),
        sa.Column("gate_policy_version", sa.String(length=16), nullable=False),
        _jsonb("rank", nullable=True),
        sa.Column("ranking_policy_version", sa.String(length=16), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_job_decisions"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_tenant_job_decisions_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["discovery_runs.id"],
                                name="fk_tenant_job_decisions_run_id_discovery_runs",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_posting_id"], ["job_postings.id"],
                                name="fk_tenant_job_decisions_job_posting_id_job_postings",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "run_id", "job_posting_id",
                            name="uq_tenant_job_decisions_tenant_run_job"),
    )
    op.create_index("ix_tenant_job_decisions_tenant_id_run_id",
                    "tenant_job_decisions", ["tenant_id", "run_id"])
    _tenant_rls("tenant_job_decisions", "tenant_job_decisions_tenant_isolation")

    # ── tenant_job_suppressions (FORCE RLS, D3 port of §8.1) ─────────────────
    op.create_table(
        "tenant_job_suppressions",
        _uuid_pk(),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("suppression_key", sa.String(length=512), nullable=False),
        sa.Column("repost_group_id", sa.String(length=80), nullable=True),
        _ts("delivered_at", nullable=False),
        _ts("expires_at", nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_job_suppressions"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_tenant_job_suppressions_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "suppression_key",
                            name="uq_tenant_job_suppressions_tenant_key"),
    )
    op.create_index("ix_tenant_job_suppressions_repost_group_id",
                    "tenant_job_suppressions", ["repost_group_id"])
    _tenant_rls("tenant_job_suppressions", "tenant_job_suppressions_tenant_isolation")


def downgrade() -> None:
    for table, policy in (
        ("tenant_job_suppressions", "tenant_job_suppressions_tenant_isolation"),
        ("tenant_job_decisions", "tenant_job_decisions_tenant_isolation"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.drop_table("tenant_job_suppressions")
    op.drop_table("tenant_job_decisions")
    op.drop_table("discovery_runs")
    op.drop_table("job_postings")
