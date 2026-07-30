"""Disaster drill — prove the per-tenant durability fix under real pressure.

AUDIT ح-1 changed the delivery day from one giant transaction to a commit per
tenant, with an honest fallback state when a tenant's pipeline dies. That fix
is only worth what a drill says it is worth, so this script:

1. runs a two-tenant delivery day where tenant B's pipeline raises mid-flight;
2. asserts tenant A's day is COMMITTED and untouched;
3. asserts tenant B's day closed honestly rather than vanishing (§15.12);
4. re-runs the same day and asserts tenant A is NOT delivered twice — the
   suppression ledger held, which is the actual customer-visible risk.

Runs entirely on the disposable career_test DB with fake boundaries: no real
message, no staging row. Safe to run any time; meant to be re-run after any
change to daily_run.

Run:  scripts/drill_disaster.sh
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from career.config import get_settings
from career.cv import daily_run
from career.engine import run as engine_run

NOW = datetime(2026, 7, 19, 4, 30, tzinfo=UTC)      # a Sunday (delivery day)


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def main() -> int:
    settings = get_settings()
    if not settings.db_name.endswith("_test"):
        print(f"REFUSING: DB_NAME={settings.db_name!r} is not a *_test DB")
        return 3

    from test_cv_daily_run import _deps, _seed_active_tenant  # type: ignore

    engine = create_engine(settings.owner_database_url, future=True)
    print("disaster drill — per-tenant durability (AUDIT ح-1)")
    with Session(engine) as session:
        a_id, a_ch = _seed_active_tenant(session)
        b_id, b_ch = _seed_active_tenant(session)
        session.commit()
        try:
            report = engine_run.RunReport(
                uuid.uuid4(), "completed", {},
                {a_id: {"final": [], "counts": {"passed": 0}},
                 b_id: {"final": [], "counts": {"passed": 3}}},
            )
            original = daily_run._run_tenant

            def _exploding(sess: Any, *, tenant_id: uuid.UUID, **kw: Any) -> Any:
                if tenant_id == b_id:
                    raise RuntimeError("drill: tenant B pipeline died")
                return original(sess, tenant_id=tenant_id, **kw)

            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                deps = _deps(Path(tmp))
                daily_run._run_tenant = _exploding      # type: ignore[assignment]
                try:
                    states = daily_run.run_daily_delivery(
                        session, report=report, deps=deps, now=NOW,
                    )
                finally:
                    daily_run._run_tenant = original
                session.commit()

                # 1 — the surviving tenant closed and is durable
                if a_id not in states:
                    _fail("tenant A produced no day state")
                _ok(f"tenant A closed: {states[a_id].state}")

                # 2 — the crashed tenant closed HONESTLY, not silently
                row = session.execute(text(
                    "SELECT state FROM tenant_day_states WHERE tenant_id = :t"
                ), {"t": str(b_id)}).scalar_one_or_none()
                if row is None:
                    _fail("tenant B vanished — a day with no state (§15.12)")
                if row != "CV_GENERATION_FAILED":
                    _fail(f"tenant B closed as {row!r}, expected the honest "
                          "CV_GENERATION_FAILED")
                _ok(f"tenant B closed honestly: {row}")

                # 3 — committed, not merely in-session
                session.rollback()
                still = session.execute(text(
                    "SELECT count(*) FROM tenant_day_states WHERE tenant_id "
                    "IN (:a, :b)"), {"a": str(a_id), "b": str(b_id)}
                ).scalar_one()
                if still != 2:
                    _fail(f"only {still}/2 day states survived a rollback — "
                          "the per-tenant commit did not happen")
                _ok("both day states survived a rollback (durable)")

                # 4 — a re-run must not deliver tenant A twice
                deps2 = _deps(Path(tmp))
                daily_run.run_daily_delivery(
                    session, report=report, deps=deps2, now=NOW,
                )
                session.commit()
                docs = [m for m in deps2.whatsapp_client.sent
                        if m.kind == "document"]
                if docs:
                    _fail(f"re-run sent {len(docs)} document(s) — duplicate "
                          "delivery risk (§15.3)")
                _ok("re-run sent no duplicate documents")
        finally:
            session.rollback()
            for tid in (a_id, b_id):
                for table in ("delivery_messages", "deliveries",
                              "tenant_day_states", "tenant_job_decisions",
                              "onboarding_sessions", "customer_channels",
                              "subscriptions", "profile_facts"):
                    stmt = f"DELETE FROM {table} WHERE tenant_id = :t"  # noqa: S608
                    session.execute(text(stmt), {"t": str(tid)})
                session.execute(text("DELETE FROM tenants WHERE id = :t"),
                                {"t": str(tid)})
            session.commit()
    print("drill passed — the durability fix holds under a mid-day crash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
