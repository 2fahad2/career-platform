"""The 24-hour window's numbers, each derived from the thing it measures.

`tests/test_ops_watchdogs.py` already proves the nudge BEHAVES — it fires on a
Saturday evening, stays quiet on a Thursday, respects an opt-out. What it
cannot prove is that the nudge is still pointed at the right hour, because the
hour lives in a systemd unit and the module holds a copy of it.

That copy is the whole reason this file exists. `_DELIVERY_HOUR` was moved from
04:30 to 11:00 on 2 August as a PRODUCT decision (see the reasoning in
`ops/systemd/career-engine-nightly.timer`, which is Fahad's and not
engineering's). Two places had to move together. If the timer moves again and
this module does not, `window_reminder_due` computes «will the window be shut
at delivery?» against a delivery that does not happen then — and it fails by
going QUIET, which is the shape of failure nothing notices until a delivery day
has already been spent on a paid template.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from career.whatsapp import window as win
from career.whatsapp.window import WindowState, window_state

REPO = Path(__file__).resolve().parents[1]
NIGHTLY_TIMER = REPO / "ops" / "systemd" / "career-engine-nightly.timer"
RIYADH = ZoneInfo("Asia/Riyadh")


def _timer_delivery_time() -> tuple[int, int]:
    """The hour the delivery run ACTUALLY fires, read from the unit itself."""
    for line in NIGHTLY_TIMER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("OnCalendar="):
            continue
        spec = line.partition("=")[2]
        assert "Asia/Riyadh" in spec, f"the delivery timer lost its zone: {spec!r}"
        match = re.search(r"(\d\d):(\d\d):\d\d", spec)
        assert match, f"unreadable OnCalendar: {spec!r}"
        return int(match.group(1)), int(match.group(2))
    raise AssertionError("career-engine-nightly.timer has no OnCalendar")


class TestTheNudgeIsAimedAtTheRunThatActuallyFires:
    def test_the_module_carries_the_timer_s_hour(self) -> None:
        hour, minute = _timer_delivery_time()
        assert (win._DELIVERY_HOUR, win._DELIVERY_MINUTE) == (hour, minute), (
            "career-engine-nightly.timer fires at "
            f"{hour:02d}:{minute:02d} Riyadh and window.py computes the "
            f"evening nudge against {win._DELIVERY_HOUR:02d}:"
            f"{win._DELIVERY_MINUTE:02d}. The hour is Fahad's product decision "
            "and this module is downstream of it — move this constant to match "
            "the unit, never the other way round."
        )

    def test_a_message_sent_anywhere_in_the_band_actually_covers_delivery(
        self,
    ) -> None:
        """The band's REASON, asserted rather than described.

        The nudge tells the operator «write to the service number now and
        tomorrow's bundle reaches you free-form». That sentence is only true if
        a message sent at that moment leaves the 24-hour window open when the
        run fires. An hour in the band that does not satisfy it is a nudge that
        asks for an action which does not work.
        """
        hour, minute = _timer_delivery_time()
        for band_hour in win._REMINDER_HOURS:
            # A Saturday evening: tomorrow is Sunday, a delivery day.
            sent = datetime(2026, 8, 8, band_hour, 30, tzinfo=RIYADH)
            delivery = datetime(
                2026, 8, 9, hour, minute, tzinfo=RIYADH,
            )
            assert window_state(
                last_inbound_at=sent, opt_out_at=None, now=delivery,
            ) is WindowState.OPEN, (
                f"a message at {band_hour:02d}:30 Riyadh does NOT keep the "
                f"window open until the {hour:02d}:{minute:02d} run — the "
                "nudge would be asking for something that does not help"
            )

    def test_the_band_lies_wholly_after_the_delivery_hour(self) -> None:
        hour, _ = _timer_delivery_time()
        assert min(win._REMINDER_HOURS) > hour


class TestTheBandSurvivesTheCadenceThatReadsIt:
    def test_the_band_is_wider_than_the_sweep_that_looks_at_it(self) -> None:
        """`window_reminder_due` is only ever consulted from the worker's
        hourly housekeeping gate, and that gate is NOT pinned to the wall
        clock: it re-arms `REMINDER_SWEEP_SECONDS` after each firing, so its
        phase drifts forward by one cycle's work every hour. A band no wider
        than the gate can therefore be stepped straight over, and the nudge
        fails by never firing — which nothing observes until a delivery day
        needs a paid template it does not have.
        """
        source = (REPO / "scripts" / "run_worker_loop.py").read_text(
            encoding="utf-8"
        )
        sweep_s = float(
            re.search(r"^REMINDER_SWEEP_SECONDS = ([\d.]+)", source, re.M).group(1)
        )
        band_s = len(win._REMINDER_HOURS) * 3600.0
        # Strictly wider is not enough to be comfortable: at exactly one gate
        # width a single slow cycle is the difference between firing and not.
        # Three firings inside the band is the margin, and four hours buys it.
        assert band_s >= 3 * sweep_s, (
            f"the evening band is {band_s / 3600:g}h and the sweep that reads "
            f"it runs every {sweep_s / 3600:g}h — an unlucky phase can skip "
            "the nudge entirely"
        )

    def test_the_hours_are_contiguous(self) -> None:
        """A gap in the band is a gap the drifting gate can land in."""
        hours = list(win._REMINDER_HOURS)
        assert hours == sorted(hours)
        assert hours == list(range(hours[0], hours[-1] + 1))


class TestTheWindowItselfIsMeta_s:
    def test_the_boundary_is_exactly_twenty_four_hours(self) -> None:
        """Not a cadence but the deadline every cadence here divides into, so
        a drift in it silently rescales the rest of the file."""
        assert win.WINDOW_HOURS == 24
        last = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)
        # One second inside is free-form; the boundary itself is not ours to
        # round, and `<` is what makes the 24-hour mark already closed.
        just_inside = last + timedelta(hours=24) - timedelta(seconds=1)
        assert window_state(
            last_inbound_at=last, opt_out_at=None, now=just_inside,
        ) is WindowState.OPEN
        assert window_state(
            last_inbound_at=last, opt_out_at=None, now=last + timedelta(hours=24),
        ) is WindowState.CLOSED
