"""OPS watchdogs (Fahad's calls): SearchAPI credit alerts + the canary
evening window nudge. Both pure — deterministic clocks, zero network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from career.engine.quota import quota_alert
from career.whatsapp.window import window_reminder_due

RIYADH = ZoneInfo("Asia/Riyadh")


# ── quota_alert ──────────────────────────────────────────────────────────────


def test_trial_all_zero_stays_quiet() -> None:
    assert quota_alert({"monthly_allowance": 0, "remaining_credits": 0}) is None


def test_healthy_paid_plan_stays_quiet() -> None:
    assert quota_alert(
        {"monthly_allowance": 10_000, "remaining_credits": 8_000}
    ) is None


def test_low_credits_warns_with_numbers() -> None:
    alert = quota_alert({"monthly_allowance": 10_000, "remaining_credits": 900})
    assert alert is not None
    assert "900" in alert and "10000" in alert


def test_small_plan_uses_the_floor() -> None:
    # 10% of 500 is 50 < floor(100) — 80 remaining must still warn
    assert quota_alert(
        {"monthly_allowance": 500, "remaining_credits": 80}
    ) is not None


def test_exhausted_credits_alert_red() -> None:
    alert = quota_alert({"monthly_allowance": 5_000, "remaining_credits": 0})
    assert alert is not None
    assert "🔴" in alert


def test_garbage_fields_never_raise() -> None:
    assert quota_alert({"monthly_allowance": "n/a", "remaining_credits": None}) is None
    assert quota_alert({}) is None


# ── window_reminder_due ──────────────────────────────────────────────────────

# Wed 2026-07-15 20:00 Riyadh — tomorrow (Thu) is a delivery day.
WED_EVENING = datetime(2026, 7, 15, 20, 0, tzinfo=RIYADH)


def test_nudges_when_window_will_be_closed_at_dawn() -> None:
    stale = WED_EVENING - timedelta(hours=20)   # closed by 04:30 (>24h)
    assert window_reminder_due(
        last_inbound_at=stale, opt_out_at=None, now=WED_EVENING
    )


def test_quiet_when_window_still_open_at_dawn() -> None:
    fresh = WED_EVENING - timedelta(hours=1)    # open until tomorrow 19:00
    assert not window_reminder_due(
        last_inbound_at=fresh, opt_out_at=None, now=WED_EVENING
    )


def test_quiet_outside_the_evening_hours() -> None:
    noon = WED_EVENING.replace(hour=12)
    assert not window_reminder_due(
        last_inbound_at=None, opt_out_at=None, now=noon
    )


def test_quiet_on_thursday_evening_weekend_ahead() -> None:
    thu_evening = WED_EVENING + timedelta(days=1)   # tomorrow = Friday
    assert not window_reminder_due(
        last_inbound_at=None, opt_out_at=None, now=thu_evening
    )


def test_saturday_evening_nudges_for_sunday() -> None:
    sat_evening = datetime(2026, 7, 18, 21, 0, tzinfo=RIYADH)
    assert window_reminder_due(
        last_inbound_at=sat_evening - timedelta(days=2),
        opt_out_at=None, now=sat_evening,
    )


def test_opt_out_never_nudges() -> None:
    assert not window_reminder_due(
        last_inbound_at=None, opt_out_at=WED_EVENING, now=WED_EVENING
    )


def test_utc_clock_is_converted_to_riyadh() -> None:
    # 17:30 UTC == 20:30 Riyadh — inside the evening window
    utc_evening = datetime(2026, 7, 15, 17, 30, tzinfo=UTC)
    assert window_reminder_due(
        last_inbound_at=None, opt_out_at=None, now=utc_evening
    )
