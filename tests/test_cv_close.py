"""The honest daily close (constant §15.12) — before code.

ONE honest state per tenant per day out of exactly seven — never a silent
success. Suppression is recorded ONLY for delivered groups (a failed job
returns tomorrow; a delivered one never repeats inside the TTL); a ledger
write failure is itself an honest state (LEDGER_FAILED), not an exception.
The admin summary carries TEN-#### codes and numbers only — no PII, ever.
Usage events roll up into per-tenant/day cost allocations (§14 fuel).
"""

from __future__ import annotations

import ast
import pathlib
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


def test_the_vocabulary_is_exactly_the_eight() -> None:
    # CHANGELOG §12: seven computational states + the event-driven eighth
    assert set(close.DAILY_STATES) == {
        "DELIVERED", "NO_MATCHES", "PARTIAL_DELIVERY", "DISCOVERY_FAILED",
        "CV_GENERATION_FAILED", "WHATSAPP_FAILED", "LEDGER_FAILED",
        "SKIPPED_OPTED_OUT",
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


def test_opt_out_after_a_real_delivery_never_erases_it(
    owner_session: Session,
) -> None:
    """P1-6. The opt-out close runs AFTER the delivery phase, so this is the
    ordinary morning of a customer who receives his jobs and then presses
    «إيقاف»: his DELIVERED day, with its real counts, must survive. It did not
    — the row was rewritten SKIPPED_OPTED_OUT with delivered: 0 while the
    suppression ledger still recorded those jobs as sent."""
    tid = _seed_tenant(owner_session)
    try:
        delivered = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=2, cv_resolved=2, cv_failed=0,
            delivered_groups=["https://a.example/j/1", "https://a.example/j/2"],
            failed_groups=[],
        )
        owner_session.commit()
        assert delivered.state == "DELIVERED"

        after = close.close_skipped_opted_out(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW, gate_passes=2,
        )
        owner_session.commit()
        assert after.state == "DELIVERED"
        assert after.counts["delivered"] == 2
    finally:
        _cleanup(owner_session, tid)


def test_opt_out_still_records_a_day_that_delivered_nothing(
    owner_session: Session,
) -> None:
    """The other half of the same rule: nothing was received, so the eighth
    state is the latest truth and must be written."""
    tid = _seed_tenant(owner_session)
    try:
        close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=0, cv_resolved=0, cv_failed=0,
            delivered_groups=[], failed_groups=[],
        )
        owner_session.commit()
        state = close.close_skipped_opted_out(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW, gate_passes=3,
        )
        owner_session.commit()
        assert state.state == "SKIPPED_OPTED_OUT"
        assert state.counts["gate_passes"] == 3
    finally:
        _cleanup(owner_session, tid)


def test_the_day_state_has_exactly_one_writer() -> None:
    """The rule was correct and merely bypassable, so the second writer that
    arrived (CHANGELOG §12's opt-out close) bypassed it and cost a real
    delivery. A ninth state will be written by somebody who never read
    _outranks — this fails the moment that somebody touches the row directly
    instead of going through the one door."""
    tree = ast.parse(
        pathlib.Path("src/career/cv/close.py").read_text(encoding="utf-8")
    )
    writers = set()
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        for node in ast.walk(func):
            builds = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "TenantDayState"
            )
            mutates = isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Attribute)
                and target.attr in ("state", "counts", "recorded_at")
                for target in node.targets
            )
            if builds or mutates:
                writers.add(func.name)
    assert writers == {"_record_day_state"}, (
        f"tenant_day_states is written outside the authority by: "
        f"{sorted(writers - {'_record_day_state'})}"
    )


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
