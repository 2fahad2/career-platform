"""grant DELETE on funnel_sessions to the app role (privacy deletion §12)

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-19

The §12 «حذف بياناتي» path runs as career_app and must delete the funnel
session (it holds the full analysis report JSON). 0012 granted only
SELECT/INSERT/UPDATE, so the deletion raised InsufficientPrivilege.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"


def upgrade() -> None:
    op.execute(f"GRANT DELETE ON funnel_sessions TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE DELETE ON funnel_sessions FROM {APP_ROLE}")
