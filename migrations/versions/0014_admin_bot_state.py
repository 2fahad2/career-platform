"""admin_bot_state — the watchtower console's getUpdates cursor

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-17

One system row (id=1) holding the last processed Telegram update offset so a
restart neither replays commands nor loses them. System table like
webhook_events: owner-written, no tenant data, no RLS.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_bot_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("update_offset", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_admin_bot_state_singleton"),
    )
    op.execute("INSERT INTO admin_bot_state (id) VALUES (1)")
    op.execute("GRANT SELECT, UPDATE ON admin_bot_state TO career_app")


def downgrade() -> None:
    op.drop_table("admin_bot_state")
