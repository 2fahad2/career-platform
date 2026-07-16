"""customer_profiles — email, LinkedIn URL, and region (CHANGELOG v1.1 §9)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-16

Fahad-approved onboarding expansion: the CV contact line needs an email,
LinkedIn belongs on the CV when the customer has it, and the Saudi
administrative region improves gate location matching (live postings say
"Eastern Province", not the city). All three are additive nullable columns
on the existing FORCE-RLS profile table — no data movement, no new grants.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "customer_profiles",
        sa.Column("email", sa.String(length=254), nullable=True),
    )
    op.add_column(
        "customer_profiles",
        sa.Column("linkedin_url", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "customer_profiles",
        sa.Column("region", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customer_profiles", "region")
    op.drop_column("customer_profiles", "linkedin_url")
    op.drop_column("customer_profiles", "email")
