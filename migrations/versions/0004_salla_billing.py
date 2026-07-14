"""salla billing — subscriptions, plans, activation tokens, webhook intake

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-14

Whitepaper §05/§09. Tables:
- plan_entitlements: reference data (no RLS), seeded from §04. Features are
  entitlements from day one; only the price numbers are provisional.
- subscriptions / subscription_events / activation_tokens: tenant-scoped
  (ENABLE + FORCE RLS). Provisioning runs as the owner/superuser (bypasses RLS)
  because it creates the tenant itself; per-customer reads use the app role.
- webhook_events: system intake (no RLS) — an order arrives before the customer
  is known. event_fingerprint is unique, giving idempotent webhook handling.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
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


def upgrade() -> None:
    # ── plan_entitlements (reference data, no RLS) ───────────────────────────
    op.create_table(
        "plan_entitlements",
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("daily_job_limit", sa.Integer(), nullable=False),
        sa.Column("monthly_cv_safety_cap", sa.Integer(), nullable=False),
        sa.Column("intro_blurb", sa.Boolean(), nullable=False),
        sa.Column("cover_letter", sa.Boolean(), nullable=False),
        sa.Column("human_review_monthly", sa.Boolean(), nullable=False),
        sa.Column("weekly_report", sa.Boolean(), nullable=False),
        sa.Column("queue_priority", sa.String(length=16), nullable=False),
        sa.Column("support_sla_hours", sa.Integer(), nullable=False),
        sa.Column("seats_cap", sa.Integer(), nullable=True),
        sa.Column("indicative_price_sar", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("plan_code", name="pk_plan_entitlements"),
    )
    op.execute(f"GRANT SELECT ON plan_entitlements TO {APP_ROLE}")

    plans = sa.table(
        "plan_entitlements",
        sa.column("plan_code", sa.String), sa.column("daily_job_limit", sa.Integer),
        sa.column("monthly_cv_safety_cap", sa.Integer), sa.column("intro_blurb", sa.Boolean),
        sa.column("cover_letter", sa.Boolean), sa.column("human_review_monthly", sa.Boolean),
        sa.column("weekly_report", sa.Boolean), sa.column("queue_priority", sa.String),
        sa.column("support_sla_hours", sa.Integer), sa.column("seats_cap", sa.Integer),
        sa.column("indicative_price_sar", sa.Integer),
    )
    op.bulk_insert(plans, [
        {"plan_code": "basic", "daily_job_limit": 1, "monthly_cv_safety_cap": 35,
         "intro_blurb": False, "cover_letter": False, "human_review_monthly": False,
         "weekly_report": False, "queue_priority": "normal", "support_sla_hours": 48,
         "seats_cap": None, "indicative_price_sar": 149},
        {"plan_code": "professional", "daily_job_limit": 2, "monthly_cv_safety_cap": 70,
         "intro_blurb": True, "cover_letter": False, "human_review_monthly": False,
         "weekly_report": False, "queue_priority": "high", "support_sla_hours": 24,
         "seats_cap": None, "indicative_price_sar": 279},
        {"plan_code": "executive", "daily_job_limit": 2, "monthly_cv_safety_cap": 70,
         "intro_blurb": True, "cover_letter": True, "human_review_monthly": True,
         "weekly_report": True, "queue_priority": "high", "support_sla_hours": 24,
         "seats_cap": 15, "indicative_price_sar": 449},
    ])

    # ── subscriptions (tenant-scoped) ────────────────────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("salla_order_id", sa.String(length=64), nullable=False),
        sa.Column("amount_sar", sa.Numeric(10, 2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_subscriptions"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_subscriptions_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_code"], ["plan_entitlements.plan_code"],
                                name="fk_subscriptions_plan_code_plan_entitlements"),
        # One creating order maps to exactly one subscription (provisioning
        # idempotency at the order level).
        sa.UniqueConstraint("salla_order_id", name="uq_subscriptions_salla_order_id"),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON subscriptions TO {APP_ROLE}")
    _tenant_rls("subscriptions", "subscriptions_tenant_isolation")

    # ── subscription_events (tenant-scoped) ──────────────────────────────────
    op.create_table(
        "subscription_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("salla_order_id", sa.String(length=64), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_subscription_events"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_subscription_events_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"],
                                name="fk_subscription_events_subscription_id_subscriptions",
                                ondelete="CASCADE"),
    )
    op.create_index("ix_subscription_events_subscription_id", "subscription_events",
                    ["subscription_id"])
    op.execute(f"GRANT SELECT, INSERT ON subscription_events TO {APP_ROLE}")
    _tenant_rls("subscription_events", "subscription_events_tenant_isolation")

    # ── activation_tokens (tenant-scoped; store hash, never the raw token) ───
    op.create_table(
        "activation_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_activation_tokens"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_activation_tokens_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"],
                                name="fk_activation_tokens_subscription_id_subscriptions",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_activation_tokens_token_hash"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON activation_tokens TO {APP_ROLE}")
    _tenant_rls("activation_tokens", "activation_tokens_tenant_isolation")

    # ── webhook_events (system intake, no RLS) ───────────────────────────────
    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("salla_order_id", sa.String(length=64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_events"),
        sa.UniqueConstraint("event_fingerprint", name="uq_webhook_events_event_fingerprint"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON webhook_events TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.execute("DROP POLICY IF EXISTS activation_tokens_tenant_isolation ON activation_tokens")
    op.drop_table("activation_tokens")
    op.execute("DROP POLICY IF EXISTS subscription_events_tenant_isolation ON subscription_events")
    op.drop_table("subscription_events")
    op.execute("DROP POLICY IF EXISTS subscriptions_tenant_isolation ON subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plan_entitlements")
