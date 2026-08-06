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
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Imported for its import-time side effect, not for a name: `career.db.session`
# installs the process-wide role guard when it loads. The three D20 services do
# not import that module — they build their own engine and go straight to the
# models — so without this line the guard would be installed in the API process
# and absent from precisely the three processes it was written for. Every
# process that touches the database imports this file; that is the only edge
# all of them share.
from career.db import session as _install_role_guard  # noqa: F401
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
    # buyer's normalized order phone — zero-touch activation (CHANGELOG §11)
    order_phone_e164: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
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
    the customer/tenant is known). event_fingerprint is unique → idempotency.

    The payload is the provider's body verbatim, which means it is personal:
    the buyer's name, mobile and email from Salla; the customer's phone,
    profile name and typed text from Meta. Until 0024 that put it outside the
    reach of «حذف بياناتي» entirely — no tenant column, so no deletion list, no
    export, no pruning. `subject_tenant_id` is the link that gives an erasure
    request something to aim at, and `payload_redacted_at` is the record that
    the body has already been replaced by a PII-free skeleton. The row itself
    is never deleted: it is the idempotency record, and a webhook whose
    fingerprint we forgot would provision the same customer twice."""

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
    #: 0022: when this row becomes eligible again. NULL means «due now», which
    #: is what the intake writes and what every row predating the retry budget
    #: is — a backoff clock only ever exists for a row that has already failed
    #: a retryable way.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: 0022: the worker's verdict on the last failure, one PII-free slug
    #: (see ``whatsapp.worker``: transient / poison / exhausted / already_spoke).
    failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 0022: the exception's CLASS name and nothing else. Never str(exc) —
    #: those strings carry phone numbers and customer text (constant 13).
    failure_detail: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 0024: the tenant this body is ABOUT, resolved at intake where it can be
    #: and by the nightly late-link pass where it becomes resolvable later.
    #: NOT named tenant_id: this table is outside RLS on purpose and the RLS
    #: meta-test keys on that exact name (same reasoning as
    #: break_glass_log.target_tenant_id). NULL is a real answer — an install
    #: hook is about a store, an order webhook can arrive before its tenant
    #: exists, and one Meta POST can carry two customers.
    subject_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    #: 0024: when the raw body was replaced by a PII-free skeleton — by the
    #: retention prune, or immediately by a customer's deletion request.
    payload_redacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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


class OutcomeEvent(Base):
    """The customer's «قدّمت/تجاهل» feedback on a delivered job (whitepaper
    §08/§14) — append-only measurement fuel; never rewritten."""

    __tablename__ = "outcome_events"
    __table_args__ = (
        Index("ix_outcome_events_tenant_id_occurred_at", "tenant_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_ref: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FunnelSession(Base):
    """The CV-analysis product journey (§04, C8) — one row per funnel tenant;
    deliberately separate from onboarding_sessions."""

    __tablename__ = "funnel_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customer_channels.id", ondelete="CASCADE"), nullable=False,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    report: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageEvent(Base):
    """One raw metered event (LLM call, enrichment fetch…) — append-only;
    the fuel for cost_allocations and the C9 pricing report (§14)."""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_tenant_id_occurred_at", "tenant_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Prompt-cache tokens, reported separately by the API and priced
    #: differently (write 1.25× input, read 0.1× input) — folding them into
    #: input_tokens would misprice every cached call (§14).
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CostAllocation(Base):
    """Per-tenant/day/category cost rollup — upserted by the daily close."""

    __tablename__ = "cost_allocations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "day", "category",
            name="uq_cost_allocations_tenant_day_category",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )


class TenantDayState(Base):
    """The ONE honest daily state per tenant (§15.12) — never silent."""

    __tablename__ = "tenant_day_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "run_date", name="uq_tenant_day_states_tenant_run_date"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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


# ── C5 — onboarding, consents, profile, facts, policies, privacy (whitepaper §05) ──


class OnboardingSession(Base):
    """One onboarding journey per tenant (whitepaper §05): the 10-state FSM
    PAID_UNCLAIMED → … → ACTIVE. Resumable at any time; a >24h stall makes it
    reminder-eligible; «دعم» escalates from every state. ``completed_at`` is the
    anchor the 30-day subscription countdown starts from (not payment time)."""

    __tablename__ = "onboarding_sessions"
    __table_args__ = (
        # Exactly one journey per tenant — restarts resume, never duplicate.
        UniqueConstraint("tenant_id", name="uq_onboarding_sessions_tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_channels.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    state_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_interaction_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cursor inside a multi-step state (e.g. which basic-data field is next).
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConsentEvent(Base):
    """Append-only, purpose-scoped consent trail (whitepaper §12): granted and
    withdrawn events both live here with timestamps — history is never edited.
    App role gets INSERT/SELECT only (same posture as audit_events)."""

    __tablename__ = "consent_events"
    __table_args__ = (
        Index("ix_consent_events_tenant_id_purpose", "tenant_id", "purpose"),
        Index("ix_consent_events_seq", "seq", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # Total insertion order — now() is transaction-fixed, so same-transaction
    # events share occurred_at and need an unambiguous tie-break.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    # basic_processing | external_providers | daily_messages | anonymous_stats
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # granted | withdrawn
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CustomerProfile(Base):
    """The basic-data answers collected by buttons/lists (whitepaper §05) —
    one row per tenant, filled progressively. cv_full_name is PII (it goes on
    the CV); expected_salary is a per-tenant target, not a hard filter (D4).
    Never contains national id or date of birth — by design, no columns."""

    __tablename__ = "customer_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_customer_profiles_tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    cv_full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # CHANGELOG v1.1 §9: CV contact line + region-aware location matching.
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    city: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    years_experience: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    willing_to_relocate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    remote_preference: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # D4: a target number (7k/15k/…), optional and soft — never a hard gate.
    expected_salary_sar: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    communication_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    requested_path: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProfileFact(Base):
    """One extracted/asserted fact about the customer (whitepaper §05):
    extraction is NOT truth — only CUSTOMER_CONFIRMED / CUSTOMER_CORRECTED /
    OPERATOR_VERIFIED facts constitute the achievement bank (§15.5). A
    correction keeps the original payload for the audit trail."""

    __tablename__ = "profile_facts"
    __table_args__ = (
        Index("ix_profile_facts_tenant_id_category", "tenant_id", "category"),
        Index("ix_profile_facts_tenant_id_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # experience | education | certification | skill | achievement | language
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # EXTRACTED | CUSTOMER_CONFIRMED | CUSTOMER_CORRECTED | CUSTOMER_REJECTED
    # | OPERATOR_VERIFIED
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="EXTRACTED")
    # cv_extraction | conversation | operator
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    original_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ForbiddenClaim(Base):
    """A claim the CV generator must never make for this tenant (§15.5):
    customer-rejected facts land here, plus operator additions."""

    __tablename__ = "forbidden_claims"
    __table_args__ = (
        Index("ix_forbidden_claims_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)  # customer_rejected | operator
    source_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profile_facts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CareerPathAssessment(Base):
    """Three-layer career-path evaluation (whitepaper §05): requested (what the
    customer wants), suggested (what the system sees: score + strengths + gaps +
    closest 3), approved (Primary/Secondary/Stretch with the customer's consent).
    Insisting on a weak path sets customer_override with explicit acknowledgment."""

    __tablename__ = "career_path_assessments"
    __table_args__ = (
        Index("ix_career_path_assessments_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    requested_path: Mapped[str] = mapped_column(String(64), nullable=False)
    # [{path, score, strengths: [], gaps: []}, …] — closest paths included.
    suggested: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # {primary, secondary, stretch} once the customer approves.
    approved: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    override_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Optional agreed realistic/stretch split (~80/20) when overriding.
    stretch_ratio_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SearchPolicy(Base):
    """Versioned per-tenant search policy (whitepaper §05/§06) — the gate reads
    the active version; edits create a new version (audit + gate_policy_version
    provenance in C6 decision logs). unknown_salary_policy defaults to
    'balanced' (D4): unadvertised salaries are not blocked by default."""

    __tablename__ = "search_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_search_policies_tenant_id_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # {primary, secondary, stretch} — mirrors the approved assessment.
    approved_paths: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    min_salary_sar: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    unknown_salary_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="balanced"
    )
    remote_policy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sectors_preferred: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sectors_avoided: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    banned_companies: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Snapshot from plan_entitlements at confirmation time.
    daily_job_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PrivacyRequest(Base):
    """A standing privacy command (whitepaper §12): export / delete / pause /
    stop_messages / withdraw_consent / status — fulfilled within a declared
    deadline; financial, consent and limited security records survive deletion
    (regulatory retention)."""

    __tablename__ = "privacy_requests"
    __table_args__ = (
        Index("ix_privacy_requests_tenant_id_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")
    source_inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ── C6 — the shared nightly pool + per-tenant decisions (whitepaper §06) ─────


class JobPosting(Base):
    """One posting in the SHARED nightly pool (whitepaper §06): discovered once
    for everyone, evaluated per tenant — cost grows with role families, not
    customers. System table (no RLS): job ads are public data with no PII; the
    engine writes as the owner role, the app role reads only.

    Identity: ``url_identity`` (joburl:v1:<sha256>) is the base and unique key;
    the extended ids exist because the same job appears under many URLs.
    ``repost_group_id`` groups reposts for suppression (§06/§8.1)."""

    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("url_identity", name="uq_job_postings_url_identity"),
        Index("ix_job_postings_cross_source_fingerprint", "cross_source_fingerprint"),
        Index("ix_job_postings_repost_group_id", "repost_group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    url_identity: Mapped[str] = mapped_column(String(80), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_native_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    job_entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cross_source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repost_group_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    company: Mapped[str] = mapped_column(String(256), nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    salary_raw: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # apply_options routing verdict (§5.4): original/canonical/route_type/…
    route: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # enrichment — once per posting regardless of tenant count (§06)
    jd_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="NOT_REQUESTED"
    )
    jd_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DiscoveryRun(Base):
    """One nightly discovery run (whitepaper §06) — honest statuses per source
    and stage; digest_only mirrors D9 (no send, no ledger writes)."""

    __tablename__ = "discovery_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    digest_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TenantJobDecision(Base):
    """The per-tenant decision record for one posting in one run (whitepaper
    §06): answers «ليش أرسلتوها لي؟» and feeds calibration. Near-misses are
    captured for the zero-day report. rank/rank trace filled for PASSed jobs."""

    __tablename__ = "tenant_job_decisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "run_id", "job_posting_id",
            name="uq_tenant_job_decisions_tenant_run_job",
        ),
        Index("ix_tenant_job_decisions_tenant_id_run_id", "tenant_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("discovery_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_posting_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(8), nullable=False)  # PASS | BLOCK
    near_miss: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # role_score, salary_status, company_tier, seniority, location, final_score…
    reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    gate_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    # none_as_null: a BLOCK row has NO rank — SQL NULL, never JSON null
    rank: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    ranking_policy_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantJobSuppression(Base):
    """Delivered-jobs suppression (D3 port of §8.1): a delivered posting is
    suppressed per tenant for a bounded TTL at the repost-group level, keyed by
    the SAME normalized-URL identity as same-run dedupe. Reads are fail-open in
    code (an unreadable ledger never hides a job); rows are written only after
    confirmed delivery (C7)."""

    __tablename__ = "tenant_job_suppressions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "suppression_key",
            name="uq_tenant_job_suppressions_tenant_key",
        ),
        Index("ix_tenant_job_suppressions_repost_group_id", "repost_group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    suppression_key: Mapped[str] = mapped_column(String(512), nullable=False)
    repost_group_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CvUpload(Base):
    """One uploaded CV file and its security-pipeline verdict (whitepaper §05 +
    §11 controls: MIME sniffing, size/page limits, sandboxed parsing, metadata
    removal, JS/embedded blocking, timeout). Bytes live in object storage via
    StorageAdapter — never in the DB (§10); scan_findings is PII-free."""

    __tablename__ = "cv_uploads"
    __table_args__ = (
        Index("ix_cv_uploads_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    original_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    mime_detected: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_status: Mapped[str] = mapped_column(String(24), nullable=False, default="received")
    scan_findings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    extracted_text_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="received")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminBotState(Base):
    """The watchtower console's getUpdates cursor — one system row (id=1),
    owner-written, no tenant data (no RLS, like webhook_events)."""

    __tablename__ = "admin_bot_state"
    # audit minor: mirror the 0014 single-row CHECK so model and schema agree
    __table_args__ = (CheckConstraint("id = 1", name="ck_admin_bot_state_singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    update_offset: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    #: 0023: the week-ending Sunday (Riyadh) of the last weekly report actually
    #: sent. The guard used to be a variable inside the worker loop's main(),
    #: so every restart inside the Sunday window sent the report again and an
    #: outage across it dropped the week in silence. NULL = never sent.
    weekly_report_sent_for: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RoleEnrichment(Base):
    """The once-ever ledger for the thin-role enrichment nudge (F-ENRICH,
    §13). One row per (tenant, role fact); presence ⟹ never re-ask."""

    __tablename__ = "role_enrichments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fact_id",
                         name="uq_role_enrichments_tenant_fact"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    fact_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profile_facts.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)   # ASKED|ENRICHED|SKIPPED
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BreakGlassLog(Base):
    """Every deliberate crossing of the tenant boundary, and why.

    Migration 0021 created this table and no model matched it, so
    ``alembic check`` — the gate step whose whole job is to notice the schema
    and the models drifting apart — asked to DROP it. That is the correct
    behaviour of the guard and the reason this class exists: a table the ORM
    cannot see is a table the next autogenerate will try to delete.

    Written only by ``app.break_glass``, which is the audited escape hatch for
    an admin path that genuinely must span tenants. It is never on a daily
    service path — the sweeps use the NOLOGIN sweep role, whose grants are
    column-level and cannot reach a message body at all. So a row here is a
    question worth asking, not routine.

    ``db_user`` is Postgres's ``name`` type, which SQLAlchemy has no direct
    mapping for; ``String`` reads it faithfully and the column is never
    written from Python.
    """

    __tablename__ = "break_glass_log"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    db_user: Mapped[str] = mapped_column(String, nullable=False)
    target_tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
