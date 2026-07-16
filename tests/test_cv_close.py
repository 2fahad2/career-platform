"""The honest daily close (constant §15.12) — before code.

ONE honest state per tenant per day out of exactly seven — never a silent
success. Suppression is recorded ONLY for delivered groups (a failed job
returns tomorrow; a delivered one never repeats inside the TTL); a ledger
write failure is itself an honest state (LEDGER_FAILED), not an exception.
The admin summary carries TEN-#### codes and numbers only — no PII, ever.
Usage events roll up into per-tenant/day cost allocations (§14 fuel).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.cv import close

NOW = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
DAY = date(2026, 7, 17)


# ── the one-state authority (pure) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(discovery_ok=False, gate_passes=0, cv_resolved=0, cv_failed=0,
              delivered=0, failed_sends=0), "DISCOVERY_FAILED"),
        (dict(discovery_ok=True, gate_passes=0, cv_resolved=0, cv_failed=0,
              delivered=0, failed_sends=0), "NO_MATCHES"),
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=0, cv_failed=3,
              delivered=0, failed_sends=0), "CV_GENERATION_FAILED"),
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
              delivered=0, failed_sends=2), "WHATSAPP_FAILED"),
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
              delivered=1, failed_sends=1), "PARTIAL_DELIVERY"),
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=3, cv_failed=0,
              delivered=3, failed_sends=0), "DELIVERED"),
        # CV failures with full delivery of the resolved rest → still partial
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
              delivered=2, failed_sends=0), "PARTIAL_DELIVERY"),
    ],
)
def test_daily_state_authority(kwargs: dict, expected: str) -> None:
    assert close.daily_state(ledger_ok=True, **kwargs) == expected


def test_ledger_failure_dominates_everything() -> None:
    assert close.daily_state(
        ledger_ok=False, discovery_ok=True, gate_passes=3, cv_resolved=3,
        cv_failed=0, delivered=3, failed_sends=0,
    ) == "LEDGER_FAILED"


def test_the_vocabulary_is_exactly_the_seven() -> None:
    assert set(close.DAILY_STATES) == {
        "DELIVERED", "NO_MATCHES", "PARTIAL_DELIVERY", "DISCOVERY_FAILED",
        "CV_GENERATION_FAILED", "WHATSAPP_FAILED", "LEDGER_FAILED",
    }


# ── the atomic close (DB) ────────────────────────────────────────────────────


def _seed_tenant(owner_session: Session) -> uuid.UUID:
    tid = uuid.uuid4()
    owner_session.execute(
        sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
        {"id": str(tid), "c": f"TEN-C{uuid.uuid4().hex[:4]}"},
    )
    owner_session.commit()
    return tid


def _cleanup(owner_session: Session, tid: uuid.UUID) -> None:
    owner_session.execute(
        sql_text("DELETE FROM tenants WHERE id = :id"), {"id": str(tid)}
    )
    owner_session.commit()


def test_close_records_state_and_suppresses_only_delivered(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)
    try:
        state = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
            delivered_groups=["https://a.example/j/1"],
            failed_groups=["https://a.example/j/2"],
        )
        owner_session.commit()
        assert state.state == "PARTIAL_DELIVERY"
        rows = owner_session.execute(
            sql_text("SELECT suppression_key FROM tenant_job_suppressions "
                     "WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).all()
        assert [r.suppression_key for r in rows] == ["https://a.example/j/1"]
        # rerun the close (idempotent day): upserts, never duplicates
        state2 = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
            delivered_groups=["https://a.example/j/1"], failed_groups=[],
        )
        owner_session.commit()
        assert state2.id == state.id
        count = owner_session.execute(
            sql_text("SELECT count(*) FROM tenant_day_states WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).scalar_one()
        assert count == 1
    finally:
        _cleanup(owner_session, tid)


def test_ledger_write_failure_is_an_honest_state_not_an_exception(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)

    def broken_suppressor(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    try:
        state = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=1, cv_resolved=1, cv_failed=0,
            delivered_groups=["https://a.example/j/1"], failed_groups=[],
            suppressor=broken_suppressor,
        )
        owner_session.commit()
        assert state.state == "LEDGER_FAILED"
    finally:
        _cleanup(owner_session, tid)


# ── the admin summary: TEN codes and numbers only ────────────────────────────


def test_admin_summary_has_codes_and_numbers_never_pii() -> None:
    text = close.format_admin_summary(
        DAY,
        [
            ("TEN-0001", "DELIVERED", {"delivered": 3}),
            ("TEN-0002", "NO_MATCHES", {"evaluated": 41}),
        ],
    )
    assert "TEN-0001" in text and "DELIVERED" in text
    assert "TEN-0002" in text and "NO_MATCHES" in text
    assert "2026-07-17" in text
    for forbidden in ("+9665", "@", "Fahad"):
        assert forbidden not in text


# ── usage → cost allocations (§14 fuel) ──────────────────────────────────────


def test_usage_rolls_up_into_cost_allocations(owner_session: Session) -> None:
    tid = _seed_tenant(owner_session)
    try:
        for cost in ("0.011", "0.014"):
            close.record_usage(
                owner_session, tenant_id=tid, kind="llm_generation",
                input_tokens=1000, output_tokens=400,
                cost_usd=Decimal(cost), now=NOW,
            )
        close.record_usage(
            owner_session, tenant_id=tid, kind="jd_enrichment",
            cost_usd=None, now=NOW,
        )
        owner_session.commit()
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)  # idempotent
        owner_session.commit()
        rows = owner_session.execute(
            sql_text("SELECT category, events, cost_usd FROM cost_allocations "
                     "WHERE tenant_id = :t ORDER BY category"),
            {"t": str(tid)},
        ).all()
        assert [(r.category, r.events) for r in rows] == [
            ("jd_enrichment", 1), ("llm_generation", 2),
        ]
        assert rows[1].cost_usd == Decimal("0.025000")
    finally:
        _cleanup(owner_session, tid)


def test_usage_budget_guard(owner_session: Session) -> None:
    tid = _seed_tenant(owner_session)
    try:
        budget = close.UsageBudget(
            owner_session, tenant_id=tid, kind="llm_generation", cap=2, day=DAY
        )
        assert budget.allow() == (True, None)
        for _ in range(2):
            close.record_usage(
                owner_session, tenant_id=tid, kind="llm_generation", now=NOW
            )
        owner_session.commit()
        allowed, reason = budget.allow()
        assert allowed is False and reason == "budget_cap_reached:llm_generation"
    finally:
        _cleanup(owner_session, tid)
