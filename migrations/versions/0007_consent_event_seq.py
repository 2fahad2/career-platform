"""consent_events.seq — total insertion order for same-timestamp events

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-15

``occurred_at`` uses now(), which is transaction-fixed in Postgres: several
consent events written in one transaction share the same timestamp, and
tie-breaking by random UUID made "latest event wins" nondeterministic — a
withdrawal could sort before the grant it revokes. An identity column gives
an unambiguous total order. (Identity sequences need no separate grant for
the app role — privileges are checked against the table.)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "consent_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
    )
    op.create_index("ix_consent_events_seq", "consent_events", ["seq"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_consent_events_seq", table_name="consent_events")
    op.drop_column("consent_events", "seq")
