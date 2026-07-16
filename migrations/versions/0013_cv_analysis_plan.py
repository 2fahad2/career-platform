"""plan_entitlements — the cv_analysis one-shot product (whitepaper §04, C8)

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-17

The funnel product is a plan row by necessity (subscriptions.plan_code FK),
with SEARCH entitlements zeroed BY DESIGN: it never builds a policy, never
enters query families, never receives daily jobs. One analysis per purchase
(monthly_cv_safety_cap=1 documents the single-report semantic). Price is
indicative like the others — fixed after the 30-day measurement.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO plan_entitlements
            (plan_code, daily_job_limit, monthly_cv_safety_cap, intro_blurb,
             cover_letter, human_review_monthly, weekly_report,
             queue_priority, support_sla_hours, seats_cap, indicative_price_sar)
        VALUES
            ('cv_analysis', 0, 1, false, false, false, false,
             'normal', 48, NULL, 29)
        ON CONFLICT (plan_code) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM plan_entitlements WHERE plan_code = 'cv_analysis'")
