"""admin_bot_state.weekly_report_sent_for — the week the report already went

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-06

The Sunday report was guarded by `last_weekly_report`, a local variable in
`scripts/run_worker_loop.py`'s `main()`. A variable in a process is a promise
about a process, and this one is `Restart=always`: any restart inside the
Sunday 06:00–12:00 Riyadh window reset it to None and sent the whole report
again, so a deploy with three restarts put three identical weekly reports on
the operator's phone. The mirror failure is worse because it is silent — if
the process was down for that six-hour window, or busy inside a long Claude
turn on the hourly sweep it rides on, the week was simply never reported and
nothing anywhere remembered that it was owed.

So the marker becomes a row: the week-ending date (the Sunday, in Riyadh) of
the last report actually sent. It lives on `admin_bot_state`, which is already
exactly this kind of thing — the singleton, owner-written, no-RLS row holding
the console's `getUpdates` cursor so a restart neither replays commands nor
loses them (0014). One more «what has this operator already been told» fact
belongs beside it rather than in a table of its own.

NULL means «no weekly report has ever been sent», which is the honest state of
the existing row: the first run after this migration reports the current week
and stamps it. Nothing is backfilled — inventing a Sunday we never sent would
suppress a real report.

The claim is a conditional UPDATE (`WHERE weekly_report_sent_for IS NULL OR
< :week_ending`), so it is atomic against itself: the row lock serializes two
processes and exactly one of them sees rowcount 1. That is what lets the
worker loop and a future `career-weekly-report.timer` both call it without
coordinating — whoever gets there first sends, the other is a no-op.

Expected lock profile: one ADD COLUMN, nullable, no default — catalog only, no
rewrite, no scan — on a table with exactly one row. `lock_timeout` is set for
the same reason as 0021 and 0022: to abort rather than queue behind a reader.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("ALTER TABLE admin_bot_state ADD COLUMN weekly_report_sent_for date")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("ALTER TABLE admin_bot_state DROP COLUMN weekly_report_sent_for")
    op.execute("RESET lock_timeout")
