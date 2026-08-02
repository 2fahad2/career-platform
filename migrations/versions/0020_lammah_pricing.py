"""lammah pricing: one pass at 199, the basic tier retired from sale

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-02

Fahad's approved decision (CHANGELOG §19). Two passes replace three: the old
gap between basic and professional was ONE job a day, which no buyer could
justify to themselves, and a third option only delays the decision. The
difference is now human versus machine.

`basic` is NOT deleted. Historical subscriptions point at it and a plan row is
part of the money trail; deleting it would orphan them. It simply stops being
sold — the store lists two passes, and the catalog maps no product to it.

The price column is indicative only (the §09 triple match reads the real
amounts from SALLA_PRODUCT_PRICING), but it is what the watchtower's revenue
view quotes, so it has to be true.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE plan_entitlements SET indicative_price_sar = 199 "
        "WHERE plan_code = 'professional'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE plan_entitlements SET indicative_price_sar = 279 "
        "WHERE plan_code = 'professional'"
    )
