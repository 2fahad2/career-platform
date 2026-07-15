"""onboarding — sessions, consents, profile, facts, policies, privacy, uploads

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-15

Whitepaper §05 (customer journey) + §12 (privacy/consents). All tables are
tenant-scoped (ENABLE + FORCE RLS). consent_events is append-only for the app
role (INSERT/SELECT only — history is never edited, same posture as
audit_events). No national-id / date-of-birth columns exist by design.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
_TENANT_GUC = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _tenant_rls(table: str, policy: str, *, append_only: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {policy} ON {table} "
        f"USING (tenant_id = {_TENANT_GUC}) WITH CHECK (tenant_id = {_TENANT_GUC})"
    )
    if append_only:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    else:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False)


def _tenant_fk() -> sa.Column:
    return sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)


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
    # ── onboarding_sessions (the 10-state journey, one per tenant) ───────────
    op.create_table(
        "onboarding_sessions",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        _ts("state_entered_at", nullable=False, default_now=True),
        _ts("last_interaction_at"),
        _ts("last_reminder_at"),
        _ts("completed_at"),
        _jsonb("context"),
        _ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_onboarding_sessions"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_onboarding_sessions_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"],
                                name="fk_onboarding_sessions_subscription_id_subscriptions",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["customer_channels.id"],
                                name="fk_onboarding_sessions_channel_id_customer_channels",
                                ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", name="uq_onboarding_sessions_tenant_id"),
    )
    _tenant_rls("onboarding_sessions", "onboarding_sessions_tenant_isolation")

    # ── consent_events (append-only: INSERT/SELECT for the app role) ─────────
    op.create_table(
        "consent_events",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=True),
        sa.Column("source_inbound_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("occurred_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_consent_events"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_consent_events_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_inbound_message_id"], ["inbound_messages.id"],
                                name="fk_consent_events_source_inbound_message_id_inbound_messages",
                                ondelete="SET NULL"),
    )
    op.create_index("ix_consent_events_tenant_id_purpose", "consent_events",
                    ["tenant_id", "purpose"])
    _tenant_rls("consent_events", "consent_events_tenant_isolation", append_only=True)

    # ── customer_profiles (basic-data answers, one per tenant) ───────────────
    op.create_table(
        "customer_profiles",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("cv_full_name", sa.String(length=128), nullable=True),
        sa.Column("city", sa.String(length=64), nullable=True),
        sa.Column("current_title", sa.String(length=128), nullable=True),
        sa.Column("years_experience", sa.Integer(), nullable=True),
        sa.Column("notice_period_days", sa.Integer(), nullable=True),
        sa.Column("employment_type", sa.String(length=32), nullable=True),
        sa.Column("willing_to_relocate", sa.Boolean(), nullable=True),
        sa.Column("remote_preference", sa.String(length=16), nullable=True),
        sa.Column("expected_salary_sar", sa.Numeric(10, 2), nullable=True),
        sa.Column("communication_language", sa.String(length=8), nullable=True),
        sa.Column("requested_path", sa.String(length=64), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_customer_profiles"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_customer_profiles_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", name="uq_customer_profiles_tenant_id"),
    )
    _tenant_rls("customer_profiles", "customer_profiles_tenant_isolation")

    # ── profile_facts (the confirm-gated achievement bank rows) ──────────────
    op.create_table(
        "profile_facts",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("category", sa.String(length=32), nullable=False),
        _jsonb("payload"),
        sa.Column("status", sa.String(length=24), nullable=False,
                  server_default=sa.text("'EXTRACTED'")),
        sa.Column("source", sa.String(length=24), nullable=False),
        _jsonb("original_payload", nullable=True),
        _ts("confirmed_at"),
        _ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_profile_facts"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_profile_facts_tenant_id_tenants", ondelete="CASCADE"),
    )
    op.create_index("ix_profile_facts_tenant_id_category", "profile_facts",
                    ["tenant_id", "category"])
    op.create_index("ix_profile_facts_tenant_id_status", "profile_facts",
                    ["tenant_id", "status"])
    _tenant_rls("profile_facts", "profile_facts_tenant_isolation")

    # ── forbidden_claims ─────────────────────────────────────────────────────
    op.create_table(
        "forbidden_claims",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("source_fact_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_forbidden_claims"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_forbidden_claims_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_fact_id"], ["profile_facts.id"],
                                name="fk_forbidden_claims_source_fact_id_profile_facts",
                                ondelete="SET NULL"),
    )
    op.create_index("ix_forbidden_claims_tenant_id", "forbidden_claims", ["tenant_id"])
    _tenant_rls("forbidden_claims", "forbidden_claims_tenant_isolation")

    # ── career_path_assessments (three layers + override) ────────────────────
    op.create_table(
        "career_path_assessments",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("requested_path", sa.String(length=64), nullable=False),
        _jsonb("suggested"),
        _jsonb("approved", nullable=True),
        sa.Column("fit_score", sa.Integer(), nullable=True),
        sa.Column("customer_override", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        _ts("override_acknowledged_at"),
        sa.Column("stretch_ratio_percent", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'draft'")),
        _ts("created_at", nullable=False, default_now=True),
        _ts("approved_at"),
        sa.PrimaryKeyConstraint("id", name="pk_career_path_assessments"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_career_path_assessments_tenant_id_tenants",
                                ondelete="CASCADE"),
    )
    op.create_index("ix_career_path_assessments_tenant_id", "career_path_assessments",
                    ["tenant_id"])
    _tenant_rls("career_path_assessments", "career_path_assessments_tenant_isolation")

    # ── search_policies (versioned; the gate reads the active version) ───────
    op.create_table(
        "search_policies",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'draft'")),
        _jsonb("approved_paths"),
        _jsonb("cities"),
        sa.Column("min_salary_sar", sa.Numeric(10, 2), nullable=True),
        sa.Column("unknown_salary_policy", sa.String(length=16), nullable=False,
                  server_default=sa.text("'balanced'")),
        sa.Column("remote_policy", sa.String(length=16), nullable=True),
        _jsonb("sectors_preferred"),
        _jsonb("sectors_avoided"),
        _jsonb("banned_companies"),
        sa.Column("daily_job_limit", sa.Integer(), nullable=True),
        _ts("confirmed_at"),
        _ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_search_policies"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_search_policies_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "version",
                            name="uq_search_policies_tenant_id_version"),
    )
    _tenant_rls("search_policies", "search_policies_tenant_isolation")

    # ── privacy_requests ─────────────────────────────────────────────────────
    op.create_table(
        "privacy_requests",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'received'")),
        sa.Column("source_inbound_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        _jsonb("details"),
        _ts("requested_at", nullable=False, default_now=True),
        _ts("deadline_at"),
        _ts("fulfilled_at"),
        sa.PrimaryKeyConstraint("id", name="pk_privacy_requests"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_privacy_requests_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_inbound_message_id"], ["inbound_messages.id"],
                                name="fk_privacy_requests_source_inbound_message_id_inbound_messages",
                                ondelete="SET NULL"),
    )
    op.create_index("ix_privacy_requests_tenant_id_status", "privacy_requests",
                    ["tenant_id", "status"])
    _tenant_rls("privacy_requests", "privacy_requests_tenant_isolation")

    # ── cv_uploads (file security pipeline verdicts; bytes live in storage) ──
    op.create_table(
        "cv_uploads",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("original_filename", sa.String(length=256), nullable=True),
        sa.Column("mime_detected", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("scan_status", sa.String(length=24), nullable=False,
                  server_default=sa.text("'received'")),
        _jsonb("scan_findings"),
        sa.Column("extracted_text_storage_key", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False,
                  server_default=sa.text("'received'")),
        _ts("created_at", nullable=False, default_now=True),
        _ts("processed_at"),
        sa.PrimaryKeyConstraint("id", name="pk_cv_uploads"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_cv_uploads_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"],
                                name="fk_cv_uploads_document_id_documents", ondelete="SET NULL"),
    )
    op.create_index("ix_cv_uploads_tenant_id", "cv_uploads", ["tenant_id"])
    _tenant_rls("cv_uploads", "cv_uploads_tenant_isolation")


def downgrade() -> None:
    for table, policy in (
        ("cv_uploads", "cv_uploads_tenant_isolation"),
        ("privacy_requests", "privacy_requests_tenant_isolation"),
        ("search_policies", "search_policies_tenant_isolation"),
        ("career_path_assessments", "career_path_assessments_tenant_isolation"),
        ("forbidden_claims", "forbidden_claims_tenant_isolation"),
        ("profile_facts", "profile_facts_tenant_isolation"),
        ("customer_profiles", "customer_profiles_tenant_isolation"),
        ("consent_events", "consent_events_tenant_isolation"),
        ("onboarding_sessions", "onboarding_sessions_tenant_isolation"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.drop_table("cv_uploads")
    op.drop_table("privacy_requests")
    op.drop_table("search_policies")
    op.drop_table("career_path_assessments")
    op.drop_table("forbidden_claims")
    op.drop_table("profile_facts")
    op.drop_table("customer_profiles")
    op.drop_table("consent_events")
    op.drop_table("onboarding_sessions")
