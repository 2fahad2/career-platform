"""whatsapp channel — customer_channels, deliveries, messages, support

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-14

Whitepaper §08. All tables are tenant-scoped (ENABLE + FORCE RLS). Lookups by
phone (before the tenant is known, on inbound) run as the owner role — the same
system-worker pattern as Salla provisioning. phone_e164 is PII: stored here,
never logged, never shown in the admin channel (§15.13).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
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


def _tenant_fk(table: str) -> sa.Column:
    return sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column:
    kwargs: dict[str, object] = {"nullable": nullable}
    if default_now:
        kwargs["server_default"] = sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    # ── customer_channels ────────────────────────────────────────────────────
    op.create_table(
        "customer_channels",
        _uuid_pk(),
        _tenant_fk("customer_channels"),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        _ts("verified_at"),
        _ts("opt_in_at"),
        _ts("opt_out_at"),
        _ts("last_inbound_at"),
        _ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_customer_channels"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_customer_channels_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"],
                                name="fk_customer_channels_subscription_id_subscriptions",
                                ondelete="SET NULL"),
        sa.UniqueConstraint("provider", "phone_e164",
                            name="uq_customer_channels_provider_phone_e164"),
    )
    op.create_index("ix_customer_channels_tenant_id", "customer_channels", ["tenant_id"])
    _tenant_rls("customer_channels", "customer_channels_tenant_isolation")

    # ── deliveries (the adaptive delivery unit) ──────────────────────────────
    op.create_table(
        "deliveries",
        _uuid_pk(),
        _tenant_fk("deliveries"),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("window_state_at_start", sa.String(length=16), nullable=True),
        sa.Column("template_message_id", sa.String(length=128), nullable=True),
        sa.Column("bundle", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("opened_at"),
        _ts("completed_at"),
        _ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_deliveries"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_deliveries_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["customer_channels.id"],
                                name="fk_deliveries_channel_id_customer_channels", ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "run_date", name="uq_deliveries_tenant_id_run_date"),
    )
    _tenant_rls("deliveries", "deliveries_tenant_isolation")

    # ── delivery_messages (outbound log + delivery receipts) ─────────────────
    op.create_table(
        "delivery_messages",
        _uuid_pk(),
        _tenant_fk("delivery_messages"),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("wa_message_id", sa.String(length=128), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("template_name", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        _ts("status_updated_at"),
        _ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_messages"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_delivery_messages_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["customer_channels.id"],
                                name="fk_delivery_messages_channel_id_customer_channels",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["delivery_id"], ["deliveries.id"],
                                name="fk_delivery_messages_delivery_id_deliveries", ondelete="SET NULL"),
    )
    op.create_index("ix_delivery_messages_wa_message_id", "delivery_messages", ["wa_message_id"])
    _tenant_rls("delivery_messages", "delivery_messages_tenant_isolation")

    # ── inbound_messages ─────────────────────────────────────────────────────
    op.create_table(
        "inbound_messages",
        _uuid_pk(),
        _tenant_fk("inbound_messages"),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wa_message_id", sa.String(length=128), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("received_at", nullable=False, default_now=True),
        _ts("processed_at"),
        sa.PrimaryKeyConstraint("id", name="pk_inbound_messages"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_inbound_messages_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["customer_channels.id"],
                                name="fk_inbound_messages_channel_id_customer_channels",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("wa_message_id", name="uq_inbound_messages_wa_message_id"),
    )
    _tenant_rls("inbound_messages", "inbound_messages_tenant_isolation")

    # ── support_events ───────────────────────────────────────────────────────
    op.create_table(
        "support_events",
        _uuid_pk(),
        _tenant_fk("support_events"),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inbound_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("admin_note", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("resolved_at"),
        sa.PrimaryKeyConstraint("id", name="pk_support_events"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_support_events_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["customer_channels.id"],
                                name="fk_support_events_channel_id_customer_channels", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inbound_message_id"], ["inbound_messages.id"],
                                name="fk_support_events_inbound_message_id_inbound_messages",
                                ondelete="SET NULL"),
    )
    _tenant_rls("support_events", "support_events_tenant_isolation")


def downgrade() -> None:
    for table, policy in (
        ("support_events", "support_events_tenant_isolation"),
        ("inbound_messages", "inbound_messages_tenant_isolation"),
        ("delivery_messages", "delivery_messages_tenant_isolation"),
        ("deliveries", "deliveries_tenant_isolation"),
        ("customer_channels", "customer_channels_tenant_isolation"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.drop_table("support_events")
    op.drop_table("inbound_messages")
    op.drop_table("delivery_messages")
    op.drop_table("deliveries")
    op.drop_table("customer_channels")
