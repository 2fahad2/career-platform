"""usage_events: prompt-cache token columns (§14 cost coverage)

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-31

The closure audit found cost metering covering only two of ~ten paid
categories. Wiring the rest in surfaced a pricing gap in the schema itself:
the Anthropic API reports cache-creation and cache-read tokens SEPARATELY
from input tokens and bills them at different rates (write 1.25× input, read
0.1× input). Folding them into input_tokens would price a cached call at up
to ~10× its real cost, so they get their own nullable columns. Existing rows
stay NULL and keep their recorded cost — no backfill is possible or honest.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usage_events", sa.Column("cache_write_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "usage_events", sa.Column("cache_read_tokens", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("usage_events", "cache_read_tokens")
    op.drop_column("usage_events", "cache_write_tokens")
