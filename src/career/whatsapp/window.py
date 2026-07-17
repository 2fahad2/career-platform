"""The 24-hour WhatsApp service window (whatsapp §08).

Meta lets us send free-form messages only within 24h of the customer's last
inbound message; outside it, only approved paid templates. Opt-out overrides
everything. Pure function with ``now`` injected so behavior is deterministic in
tests (never reads the clock itself).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

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


# ── the canary evening reminder (admin channel, template-independence) ───────

_RIYADH = ZoneInfo("Asia/Riyadh")
_DELIVERY_HOUR = 4          # the nightly timer fires 04:30 Riyadh
_REMINDER_HOURS = (19, 20, 21, 22)   # evening nudge window (Riyadh)
_SAUDI_WEEKEND = (4, 5)     # Fri/Sat — no delivery next dawn on Thu/Fri eve


def window_reminder_due(
    *,
    last_inbound_at: datetime | None,
    opt_out_at: datetime | None,
    now: datetime,
) -> bool:
    """True when the operator should be nudged TONIGHT: tomorrow's dawn run
    is a delivery day and the 24h window will already be CLOSED at 04:30 —
    so without an approved template nothing free-form can go out. Pure;
    the caller de-duplicates per evening."""
    if opt_out_at is not None:
        return False
    local = now.astimezone(_RIYADH)
    if local.hour not in _REMINDER_HOURS:
        return False
    tomorrow = local.date() + timedelta(days=1)
    if tomorrow.weekday() in _SAUDI_WEEKEND:
        return False
    dawn = datetime.combine(
        tomorrow, time(hour=_DELIVERY_HOUR, minute=30), tzinfo=_RIYADH
    )
    return window_state(
        last_inbound_at=last_inbound_at, opt_out_at=opt_out_at, now=dawn
    ) is WindowState.CLOSED
