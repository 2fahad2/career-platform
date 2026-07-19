"""The nightly run + the C6 exit condition (whitepaper §13) — before code.

THE exit test: one shared pool, two tenants with different policies → two
DIFFERENT correct lists, a decision record for every (tenant, posting) that
answers «ليش أرسلتوها لي؟», and a versioned Rank Trace on every PASS. Plus:
each source is isolated (one failing source degrades the run to 'partial'
with a sanitized reason, never a crash), the pool upserts by identity, and
honest statuses end-to-end.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.engine import run as engine_run

NOW = datetime(2026, 7, 16, 3, 30, tzinfo=UTC)

RIYADH = "Riyadh, Saudi Arabia"
_RICH = ("We need a senior business analyst. Requirements gathering, "
         "stakeholder management, 8 years of experience. ERP background. ")

JOBS = [
    # J1: confirmed 12,000 — passes a low-target tenant, blocked by a 20k one
    {"title": "Senior Business Analyst", "company_name": "Delta Solutions",
     "apply_link": "https://careers.delta.example/j/1", "location": RIYADH,
     "description": _RICH + "Salary: 12,000 SAR per month."},
    # J3: confirmed 25,000 at a tier-1 company — passes both tenants
    {"title": "Senior Business Analyst", "company_name": "Saudi Aramco",
     "apply_link": "https://careers.aramco.example/j/3", "location": RIYADH,
     "description": _RICH + "Salary: 25,000 SAR per month."},
]


class FakeSearchApi:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("secret-laden provider message — must not leak")
        if params["q"] == "Business Analyst" and params["location"] == RIYADH:
            return {"jobs": JOBS}
        return {"jobs": []}


class FakeJobSpy:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def scrape(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.rows


class NoFetch:
    """Enrichment transport that must never be reached in these tests (the
    descriptions already carry the salary evidence)."""

    def get(self, url: str, *, timeout: float):  # pragma: no cover - guard
        raise AssertionError("network fetch attempted in a fake-only test")


def _resolver(hostname: str):
    import socket

    return [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))]


def _seed_tenant(
    owner: Session, *, min_salary: float, unknown_policy: str, limit: int,
) -> str:
    tenant_id = str(uuid.uuid4())
    owner.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
                  {"id": tenant_id, "c": f"TEN-E{uuid.uuid4().hex[:4]}"})
    owner.execute(
        sql_text(
            "INSERT INTO subscriptions "
            "(id, tenant_id, plan_code, status, salla_order_id, amount_sar, currency) "
            "VALUES (:id, :tid, 'basic', 'ACTIVE', :oid, 149, 'SAR')"
        ),
        {"id": str(uuid.uuid4()), "tid": tenant_id, "oid": f"O-{uuid.uuid4()}"},
    )
    owner.execute(
        sql_text(
            "INSERT INTO search_policies "
            "(id, tenant_id, version, status, approved_paths, cities, min_salary_sar, "
            " unknown_salary_policy, remote_policy, sectors_preferred, sectors_avoided, "
            " banned_companies, daily_job_limit) "
            "VALUES (:id, :tid, 1, 'active', CAST(:paths AS jsonb), CAST(:cities AS jsonb), "
            " :minsal, :unknown, 'hybrid', '{}', '{}', '{}', :lim)"
        ),
        {"id": str(uuid.uuid4()), "tid": tenant_id,
         "paths": json.dumps({"primary": "business_analyst", "secondary": None,
                              "stretch": None}),
         "cities": json.dumps({"cities": ["الرياض"], "willing_to_relocate": False}),
         "minsal": min_salary, "unknown": unknown_policy, "lim": limit},
    )
    return tenant_id


def _cleanup(owner_engine: Engine, tenant_ids: list[str], run_id: str | None) -> None:
    with Session(owner_engine) as s:
        for tid in tenant_ids:
            s.execute(sql_text("DELETE FROM tenants WHERE id = :id"), {"id": tid})
        if run_id:
            s.execute(sql_text("DELETE FROM discovery_runs WHERE id = :id"), {"id": run_id})
        s.execute(sql_text(
            "DELETE FROM job_postings WHERE url LIKE '%delta.example%' "
            "OR url LIKE '%aramco.example%'"
        ))
        s.commit()


# ═════════════ THE C6 EXIT CONDITION (whitepaper §13) ════════════════════════


def test_two_tenants_one_pool_two_different_correct_lists(
    owner_engine: Engine,
) -> None:
    tids: list[str] = []
    run_id: str | None = None
    with Session(owner_engine) as s:
        low = _seed_tenant(s, min_salary=8000.0, unknown_policy="balanced", limit=2)
        high = _seed_tenant(s, min_salary=20000.0, unknown_policy="strict", limit=2)
        tids = [low, high]
        s.commit()
        try:
            report = engine_run.run_nightly(
                s,
                searchapi=FakeSearchApi(),
                jobspy_client=FakeJobSpy(),
                fetcher=NoFetch(),
                resolver=_resolver,
                now=NOW,
                tenant_ids=[uuid.UUID(t) for t in tids],
            )
            run_id = str(report.run_id)
            assert report.status == "completed"

            low_list = report.per_tenant[uuid.UUID(low)]["final"]
            high_list = report.per_tenant[uuid.UUID(high)]["final"]
            # ONE pool → TWO different, correct lists (§13)
            assert len(low_list) == 2      # J3 + J1 both clear an 8k target
            assert len(high_list) == 1     # only J3 clears a 20k target
            assert low_list != high_list
            # ordering is correct: the tier-1 confirmed job leads for `low`
            assert "aramco" in low_list[0]["url"]
            assert "aramco" in high_list[0]["url"]

            # a decision record exists for EVERY (tenant, posting) pair
            decisions = s.execute(
                sql_text(
                    "SELECT tenant_id, decision, reasons, rank, "
                    "ranking_policy_version, gate_policy_version "
                    "FROM tenant_job_decisions WHERE run_id = :rid"
                ),
                {"rid": run_id},
            ).all()
            assert len(decisions) == 4     # 2 tenants × 2 pooled jobs
            for row in decisions:
                assert row.decision in ("PASS", "BLOCK")
                assert row.gate_policy_version == "v1"
                assert row.reasons["role_score"] is not None
            # every PASS carries a versioned Rank Trace (§06)
            passes = [r for r in decisions if r.decision == "PASS"]
            assert passes and all(
                r.rank is not None
                and r.ranking_policy_version == "v1"
                and r.rank["position"] >= 1
                for r in passes
            )
            # and a BLOCK carries none — SQL NULL, never JSON null
            assert all(r.rank is None for r in decisions if r.decision == "BLOCK")
            # the high-target tenant blocked J1 for the salary, recorded honestly
            blocked = [
                r for r in decisions
                if str(r.tenant_id) == high and r.decision == "BLOCK"
            ]
            assert blocked[0].reasons["gate_reason"] == "salary_likely_below_min"

            # the pool is shared: exactly 2 postings, discovered once
            pool = s.execute(
                sql_text(
                    "SELECT count(*) FROM job_postings "
                    "WHERE url LIKE '%.example/j/%'"
                )
            ).scalar_one()
            assert pool == 2
        finally:
            _cleanup(owner_engine, tids, run_id)


# ═════════════ isolation, honesty, idempotent pool ═══════════════════════════


def test_a_failing_source_degrades_to_partial_with_sanitized_reason(
    owner_engine: Engine,
) -> None:
    tids: list[str] = []
    run_id: str | None = None
    with Session(owner_engine) as s:
        tids = [_seed_tenant(s, min_salary=8000.0, unknown_policy="balanced", limit=2)]
        s.commit()
        try:
            report = engine_run.run_nightly(
                s, searchapi=FakeSearchApi(fail=True), jobspy_client=FakeJobSpy([
                    # probed: bare "Business Analyst" scores 63 (<70); Senior → 73
                    {"title": "Senior Business Analyst", "company": "Delta Solutions",
                     "job_url": "https://careers.delta.example/j/9",
                     "location": RIYADH, "description": _RICH + "Salary: 12,000 SAR per month."},
                ]),
                fetcher=NoFetch(), resolver=_resolver, now=NOW,
                tenant_ids=[uuid.UUID(tids[0])],
            )
            run_id = str(report.run_id)
            assert report.status == "partial"
            google = report.counts["sources"]["searchapi_google_jobs"]
            assert google["status"] == "error"
            assert google["reason"] == "ConnectionError"          # class name only
            assert "secret-laden" not in json.dumps(report.counts)  # sanitized
            # the healthy source still contributed
            assert report.counts["sources"]["jobspy"]["fetched"] == 1
            assert len(report.per_tenant[uuid.UUID(tids[0])]["final"]) == 1
        finally:
            _cleanup(owner_engine, tids, run_id)


def test_no_active_tenants_is_an_honest_short_run(owner_engine: Engine) -> None:
    with Session(owner_engine) as s:
        report = engine_run.run_nightly(
            s, searchapi=FakeSearchApi(), jobspy_client=FakeJobSpy(),
            fetcher=NoFetch(), resolver=_resolver, now=NOW,
            tenant_ids=[uuid.uuid4()],  # nobody active
        )
        try:
            assert report.status == "no_active_tenants"
            assert report.per_tenant == {}
        finally:
            s.execute(sql_text("DELETE FROM discovery_runs WHERE id = :id"),
                      {"id": str(report.run_id)})
            s.commit()


def test_pool_upserts_by_identity_across_runs(owner_engine: Engine) -> None:
    tids: list[str] = []
    run_ids: list[str] = []
    with Session(owner_engine) as s:
        tids = [_seed_tenant(s, min_salary=8000.0, unknown_policy="balanced", limit=2)]
        s.commit()
        try:
            for _ in range(2):
                report = engine_run.run_nightly(
                    s, searchapi=FakeSearchApi(), jobspy_client=FakeJobSpy(),
                    fetcher=NoFetch(), resolver=_resolver, now=NOW,
                    tenant_ids=[uuid.UUID(tids[0])],
                )
                run_ids.append(str(report.run_id))
            pool = s.execute(
                sql_text("SELECT count(*) FROM job_postings WHERE url LIKE '%.example/j/%'")
            ).scalar_one()
            assert pool == 2                       # same identities → no duplicates
            second_counts = report.counts["pool"]
            assert second_counts["seen_again"] == 2
            assert second_counts["new"] == 0
        finally:
            with Session(owner_engine) as cleanup:
                for rid in run_ids:
                    cleanup.execute(
                        sql_text("DELETE FROM discovery_runs WHERE id = :id"), {"id": rid}
                    )
                cleanup.commit()
            _cleanup(owner_engine, tids, None)


def test_interleave_by_source_shares_the_cap_fairly() -> None:
    """Audit fix: a full google batch must not starve jobspy out of the
    retrieval slice — round-robin, order preserved within each source."""
    from career.engine.run import interleave_by_source
    from career.engine.sources import DiscoveredJob

    def job(source: str, n: int) -> DiscoveredJob:
        return DiscoveredJob(
            title=f"T{n}", company=f"C{n}", url=f"https://x.example/{source}/{n}",
            source=source, family="business_analyst",
        )

    google = [job("searchapi_google_jobs", i) for i in range(6)]
    spy = [job("jobspy", i) for i in range(2)]
    mixed = interleave_by_source(google + spy)
    cap4 = mixed[:4]
    assert sum(1 for j in cap4 if j.source == "jobspy") == 2   # both survive
    google_kept = [j for j in mixed if j.source == "searchapi_google_jobs"]
    assert [j.title for j in google_kept] == [f"T{i}" for i in range(6)]
