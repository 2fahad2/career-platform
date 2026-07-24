"""outbox_events: add FORCE ROW LEVEL SECURITY (AUDIT ح-5 meta-test catch)

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-24

The new RLS meta-test (tests/test_rls_meta.py) asserts ENABLE+FORCE on every
table carrying tenant_id; outbox_events was the single ENABLE-only outlier
(0002 predates the _tenant_rls helper). FORCE is inert while the owner role
is a superuser (ك-3), but the catalog invariant must be uniform so future
owner-hardening cannot open a hole.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE outbox_events NO FORCE ROW LEVEL SECURITY")
