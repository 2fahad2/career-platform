"""The 24-hour WhatsApp service window (whatsapp §08).

Meta lets us send free-form messages only within 24h of the customer's last
inbound message; outside it, only approved paid templates. Opt-out overrides
everything. Pure function with ``now`` injected so behavior is deterministic in
tests (never reads the clock itself).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

WINDOW_HOURS = 24


class WindowState(StrEnum):
    OPEN = "open"          # free-form allowed
    CLOSED = "closed"      # only approved templates
    OPTED_OUT = "opted_out"  # send nothing


def window_state(
    *,
    last_inbound_at: datetime | None,
    opt_out_at: datetime | None,
    now: datetime,
) -> WindowState:
    if opt_out_at is not None:
        return WindowState.OPTED_OUT
    if last_inbound_at is None:
        return WindowState.CLOSED
    if now - last_inbound_at < timedelta(hours=WINDOW_HOURS):
        return WindowState.OPEN
    return WindowState.CLOSED
