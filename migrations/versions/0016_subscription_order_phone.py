"""subscriptions.order_phone_e164 — zero-touch activation (CHANGELOG §11)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-19

The buyer's normalized order phone lets any reply FROM that number claim its
PAID_UNCLAIMED subscription without typing a token (the reply itself is the
ownership proof). Nullable: legacy rows and orders without a phone keep the
token-only path.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("order_phone_e164", sa.String(length=20), nullable=True),
    )
    op.create_index(
        "ix_subscriptions_order_phone_e164", "subscriptions",
        ["order_phone_e164"],
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_order_phone_e164", table_name="subscriptions")
    op.drop_column("subscriptions", "order_phone_e164")
