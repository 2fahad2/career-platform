"""ORM models for the RLS foundation (C2).

Only the tables needed to establish and test tenant isolation are defined here.
The full schema groups (identity, profile, jobs, delivery — whitepaper §10) are
added in later phases. ``Document`` deliberately mirrors §10: the DB stores the
storage key + SHA + type + size + owner + status, never the PDF bytes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from career.db.base import Base


class Tenant(Base):
    """A tenant == a customer. Surfaced operationally only as a TEN-#### code
    (no PII in the admin channel — §15.13)."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Human-facing opaque code, e.g. "TEN-0001". Never a name or phone number.
    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    documents: Mapped[list[Document]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class Document(Base):
    """Tenant-scoped document metadata. RLS confines every row to its tenant."""

    __tablename__ = "documents"
    __table_args__ = (
        # Composite uniqueness that always carries the tenant — defence in depth
        # beyond RLS (§15.10: isolation is more than a tenant_id column).
        UniqueConstraint("tenant_id", "storage_key", name="uq_documents_tenant_id_storage_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Object-storage key, e.g. "tenants/<tenant_id>/documents/<document_id>.pdf".
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped[Tenant] = relationship(back_populates="documents")


class OutboxEvent(Base):
    """Transactional outbox (§15 / whitepaper §10). Written in the same
    transaction as the business change; a relay (owner role) publishes
    unpublished rows to the queue and stamps ``published_at``. Payload is
    PII-free by contract."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        # Partial index for the relay's "unpublished" scan.
        Index(
            "ix_outbox_events_unpublished", "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ProcessedMessage(Base):
    """Idempotency ledger. A redelivered message whose (tenant_id,
    idempotency_key) already exists is a no-op (§15.11 companion)."""

    __tablename__ = "processed_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_processed_messages_tenant_id_idempotency_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
    """Tenant-scoped, append-only audit trail (whitepaper §10). Actor/action are
    opaque tokens; details are PII-free by contract (and secret-redacted by the
    writer). The admin channel never renders PII (§15.13)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlanEntitlement(Base):
    """Plan → feature entitlements (whitepaper §04). Reference data (no RLS).
    Features are built from day one; only the price numbers are provisional."""

    __tablename__ = "plan_entitlements"

    plan_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    daily_job_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_cv_safety_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    intro_blurb: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cover_letter: Mapped[bool] = mapped_column(Boolean, nullable=False)
    human_review_monthly: Mapped[bool] = mapped_column(Boolean, nullable=False)
    weekly_report: Mapped[bool] = mapped_column(Boolean, nullable=False)
    queue_priority: Mapped[str] = mapped_column(String(16), nullable=False)
    support_sla_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    seats_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indicative_price_sar: Mapped[int] = mapped_column(Integer, nullable=False)


class Subscription(Base):
    """A customer's subscription (whitepaper §05). Tenant-scoped. Provisioned
    only on payment=paid; states drive the daily loop and billing."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("salla_order_id", name="uq_subscriptions_salla_order_id"),
        Index("ix_subscriptions_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("plan_entitlements.plan_code"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    salla_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_sar: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubscriptionEvent(Base):
    """Append-only trail of subscription state transitions and order events."""

    __tablename__ = "subscription_events"
    __table_args__ = (
        Index("ix_subscription_events_subscription_id", "subscription_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    salla_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ActivationToken(Base):
    """Short-lived token linking a paid order ↔ the phone that starts WhatsApp ↔
    the customer. The raw token is never stored — only its SHA-256 hash."""

    __tablename__ = "activation_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_activation_tokens_token_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WebhookEvent(Base):
    """System-level intake of provider webhooks (no RLS — an order arrives before
    the customer/tenant is known). event_fingerprint is unique → idempotency."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("event_fingerprint", name="uq_webhook_events_event_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    salla_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CustomerChannel(Base):
    """A customer's messaging channel (whitepaper §08). phone_e164 is PII —
    stored here, never logged, never shown in the admin channel. Unique per
    (provider, phone): one WhatsApp number maps to exactly one customer."""

    __tablename__ = "customer_channels"
    __table_args__ = (
        UniqueConstraint("provider", "phone_e164",
                         name="uq_customer_channels_provider_phone_e164"),
        Index("ix_customer_channels_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Delivery(Base):
    """The adaptive-delivery unit for one customer-day (whitepaper §08). The
    bundle is a generic parts list here; C7 fills it with real CV/job refs."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_date", name="uq_deliveries_tenant_id_run_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_channels.id", ondelete="CASCADE"), nullable=False
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    window_state_at_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    template_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bundle: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DeliveryMessage(Base):
    """Outbound message log + delivery receipts (statuses[])."""

    __tablename__ = "delivery_messages"
    __table_args__ = (
        Index("ix_delivery_messages_wa_message_id", "wa_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_channels.id", ondelete="CASCADE"), nullable=False
    )
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="SET NULL"), nullable=True
    )
    wa_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    template_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InboundMessage(Base):
    """Inbound customer message (whitepaper §08). wa_message_id unique →
    idempotency. text_body is PII."""

    __tablename__ = "inbound_messages"
    __table_args__ = (
        UniqueConstraint("wa_message_id", name="uq_inbound_messages_wa_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_channels.id", ondelete="CASCADE"), nullable=False
    )
    wa_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportEvent(Base):
    """A 'support' escalation to the admin channel (whitepaper §08)."""

    __tablename__ = "support_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_channels.id", ondelete="CASCADE"), nullable=False
    )
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
