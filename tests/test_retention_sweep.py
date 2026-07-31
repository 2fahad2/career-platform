"""§12 — the 90-day retention promise, finally executed by something.

The consent text, §12 and the published privacy page all say the same thing:
kept in full while subscribed, 90 days after it ends, then professionally
deleted. Nothing ran it. These tests pin the behaviour of the job that now
does — including, importantly, the customers it must NOT touch.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.onboarding import retention
from career.salla import subscriptions as sub_states

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=200)


def _tenant_with_profile(
    session: Session, *, status: str, period_end: datetime | None,
) -> uuid.UUID:
    tid = uuid.uuid4()
    session.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:i, :c)"),
                    {"i": str(tid), "c": f"TEN-R{uuid.uuid4().int % 100_000:05d}"})
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency, current_period_end)"
        " VALUES (:i, :t, 'professional', :s, :o, :a, 'SAR', :e)"),
        {"i": str(uuid.uuid4()), "t": str(tid), "s": status,
         "o": f"ORD-{uuid.uuid4()}", "a": Decimal("279"), "e": period_end})
    session.execute(sql_text(
        "INSERT INTO customer_profiles (id, tenant_id, cv_full_name, city)"
        " VALUES (:i, :t, 'Test Person', 'Riyadh')"),
        {"i": str(uuid.uuid4()), "t": str(tid)})
    session.commit()
    return tid


def _cleanup(session: Session, *tenant_ids: uuid.UUID) -> None:
    session.rollback()
    for tid in tenant_ids:
        for table in ("privacy_requests", "customer_profiles",
                      "subscription_events", "subscriptions"):
            session.execute(sql_text(
                f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                {"t": str(tid)})
        session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                        {"t": str(tid)})
    session.commit()


def _has_profile(session: Session, tid: uuid.UUID) -> bool:
    return session.execute(sql_text(
        "SELECT count(*) FROM customer_profiles WHERE tenant_id = :t"),
        {"t": str(tid)}).scalar_one() > 0


def test_a_customer_gone_past_ninety_days_is_deleted(owner_session):
    tid = _tenant_with_profile(
        owner_session, status=sub_states.EXPIRED, period_end=LONG_AGO)
    try:
        counts = retention.sweep_retention(owner_session, now=NOW)
        owner_session.commit()

        assert counts["swept"] == 1
        assert not _has_profile(owner_session, tid)
        # the financial record survives by design (§12 regulatory exception)
        assert owner_session.execute(sql_text(
            "SELECT count(*) FROM subscriptions WHERE tenant_id = :t"),
            {"t": str(tid)}).scalar_one() == 1
        # and the deletion has an auditable trail with a date
        assert owner_session.execute(sql_text(
            "SELECT count(*) FROM privacy_requests WHERE tenant_id = :t"
            " AND kind = 'delete' AND status = 'fulfilled'"),
            {"t": str(tid)}).scalar_one() == 1
    finally:
        _cleanup(owner_session, tid)


def test_a_live_customer_is_never_swept(owner_session):
    """The one mistake this job must never make."""
    active = _tenant_with_profile(
        owner_session, status=sub_states.ACTIVE, period_end=LONG_AGO)
    paused = _tenant_with_profile(
        owner_session, status=sub_states.PAUSED, period_end=LONG_AGO)
    try:
        retention.sweep_retention(owner_session, now=NOW)
        owner_session.commit()

        assert _has_profile(owner_session, active)
        assert _has_profile(owner_session, paused)
    finally:
        _cleanup(owner_session, active, paused)


def test_someone_who_lapsed_last_month_still_has_their_data(owner_session):
    tid = _tenant_with_profile(
        owner_session, status=sub_states.EXPIRED,
        period_end=NOW - timedelta(days=30))
    try:
        counts = retention.sweep_retention(owner_session, now=NOW)
        owner_session.commit()

        assert counts["swept"] == 0
        assert _has_profile(owner_session, tid)   # day 30 of 90
    finally:
        _cleanup(owner_session, tid)


def test_a_returning_customer_keeps_everything(owner_session):
    """Lapsed long ago but renewed — the renewal row is live, so the tenant
    is not «gone» no matter how old the previous period is."""
    tid = _tenant_with_profile(
        owner_session, status=sub_states.EXPIRED, period_end=LONG_AGO)
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency, current_period_end)"
        " VALUES (:i, :t, 'professional', 'ACTIVE', :o, 279, 'SAR', :e)"),
        {"i": str(uuid.uuid4()), "t": str(tid), "o": f"ORD-{uuid.uuid4()}",
         "e": NOW + timedelta(days=20)})
    owner_session.commit()
    try:
        retention.sweep_retention(owner_session, now=NOW)
        owner_session.commit()

        assert _has_profile(owner_session, tid)
    finally:
        _cleanup(owner_session, tid)


def test_the_sweep_is_safe_to_run_every_night_forever(owner_session):
    tid = _tenant_with_profile(
        owner_session, status=sub_states.EXPIRED, period_end=LONG_AGO)
    try:
        first = retention.sweep_retention(owner_session, now=NOW)
        owner_session.commit()
        second = retention.sweep_retention(
            owner_session, now=NOW + timedelta(days=1))
        owner_session.commit()
        third = retention.sweep_retention(
            owner_session, now=NOW + timedelta(days=2))
        owner_session.commit()

        assert first["swept"] == 1
        assert second["swept"] == 0 and third["swept"] == 0
        # exactly one recorded deletion, not one per night
        assert owner_session.execute(sql_text(
            "SELECT count(*) FROM privacy_requests WHERE tenant_id = :t"),
            {"t": str(tid)}).scalar_one() == 1
    finally:
        _cleanup(owner_session, tid)
