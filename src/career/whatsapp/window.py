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

#: NOT ours and not tunable: Meta's own rule. Every other number in this file
#: is derived from it, and nothing in this repository may «adjust» it — a
#: window we believe is 25 hours long is a free-form send that Meta rejects at
#: the moment of delivery, and a window we believe is 23 hours long is a paid
#: template bought for a message that was already free.
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
#: The delivery run's hour, Riyadh. Moved from 04:30 to 11:00 (Fahad, 2
#: August): at dawn EVERY customer's window is shut, so every delivery needed
#: a paid template; at eleven, anyone who wrote to us the previous evening is
#: still inside their 24h window and the bundle goes out free-form.
#: A COPY of `ops/systemd/career-engine-nightly.timer`'s OnCalendar hour, and
#: the copy is the risk: that hour is Fahad's product decision and this module
#: is engineering downstream of it. If the timer moves and this does not, the
#: nudge is computed against a delivery that does not happen then — it would go
#: quiet exactly when it was needed, which is the failure that does not
#: announce itself. `tests/test_whatsapp_window.py` reads the timer file and
#: fails on the difference, so the two cannot drift silently.
_DELIVERY_HOUR = 11
_DELIVERY_MINUTE = 0

#: The evening band, Riyadh — derived from the delivery hour twice over.
#:
#: WHY THESE HOURS AT ALL. A message the customer sends at hour H holds the
#: window open until H+24. For the window to still be open at tomorrow's
#: delivery, they must write to us AFTER the delivery hour today — so any hour
#: after 11:00 would technically work. The band is late because the nudge asks
#: for an action: it has to land when he is holding the phone and can answer in
#: one tap, and a reminder at 13:00 is a reminder he intends to act on later.
#:
#: WHY IT IS FOUR HOURS WIDE, which is the cadence question. This is read by
#: the worker's hourly housekeeping gate (REMINDER_SWEEP_SECONDS), and a band
#: NARROWER than that gate could be stepped over entirely by an unlucky phase —
#: the gate drifts forward by a cycle's work each hour, so its firings are not
#: pinned to the clock. Four hours guarantees at least three firings inside the
#: band no matter where the phase sits. A one-hour band would be a nudge that
#: silently never fires, and the whole point of it is that its absence is
#: invisible until a delivery day costs a paid template.
#:
#: The band must also lie entirely after `_DELIVERY_HOUR`, or the earliest hour
#: in it would ask the customer for a message that expires BEFORE the run it is
#: meant to cover. Asserted, not assumed — see tests/test_whatsapp_window.py.
_REMINDER_HOURS = (19, 20, 21, 22)

#: Fri/Sat. §08 delivers Sunday–Thursday, so a Thursday or Friday evening nudge
#: would ask the customer to hold a window open for a run that never fires —
#: and spend the goodwill of an unnecessary message to do it.
_SAUDI_WEEKEND = (4, 5)


def window_reminder_due(
    *,
    last_inbound_at: datetime | None,
    opt_out_at: datetime | None,
    now: datetime,
) -> bool:
    """True when the operator should be nudged TONIGHT: tomorrow IS a delivery
    day and their 24h window will STILL be closed when the run fires — so
    without an approved template nothing free-form can go out. Pure; the
    caller de-duplicates per evening.

    Note the eleven o'clock move makes this fire far less often: a message
    sent at nine in the evening leaves the window open until nine the next
    evening, which now covers the run instead of missing it by seven hours."""
    if opt_out_at is not None:
        return False
    local = now.astimezone(_RIYADH)
    if local.hour not in _REMINDER_HOURS:
        return False
    tomorrow = local.date() + timedelta(days=1)
    if tomorrow.weekday() in _SAUDI_WEEKEND:
        return False
    delivery_at = datetime.combine(
        tomorrow, time(hour=_DELIVERY_HOUR, minute=_DELIVERY_MINUTE),
        tzinfo=_RIYADH,
    )
    return window_state(
        last_inbound_at=last_inbound_at, opt_out_at=opt_out_at,
        now=delivery_at,
    ) is WindowState.CLOSED
