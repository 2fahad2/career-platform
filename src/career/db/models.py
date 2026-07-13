"""ORM models for the RLS foundation (C2).

Only the tables needed to establish and test tenant isolation are defined here.
The full schema groups (identity, profile, jobs, delivery — whitepaper §10) are
added in later phases. ``Document`` deliberately mirrors §10: the DB stores the
storage key + SHA + type + size + owner + status, never the PDF bytes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
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
