"""Ranking + suppression + slots + cut acceptance tests (whitepaper §06).

Deterministic rules decided eligibility (C6.6); ranking only reorders what
passed — exported as ranking_policy_version with a Rank Trace per job.
Suppression operates at the repost-group level, is fail-open, and filters
BEFORE the cap so a suppressed job never consumes a limited slot. Slots
(strong companies / Arabic ads / source diversity / very fresh / exploration)
compose deterministically without ever displacing the top pick; the final
list is cut at the plan's daily_job_limit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.engine import ranking

NOW = datetime(2026, 7, 15, 23, 30, tzinfo=UTC)


def _candidate(
    key: str, *, salary_outcome: str = "PASS_CONFIRMED", cq: int = 95,
    role: int = 90, demoted: bool = False, company_tier: int = 1,
    title: str = "Business Analyst", source: str = "serpapi_google_jobs",
    posted_at: datetime | None = None, repost_group_id: str | None = None,
    dedupe_url_key: str | None = None,
) -> ranking.RankCandidate:
    return ranking.RankCandidate(
        ref=key,
        salary_outcome=salary_outcome,
        cq_score=cq,
        role_score=role,
        demoted=demoted,
        company_tier=company_tier,
        title=title,
        source=source,
        posted_at=posted_at,
        repost_group_id=repost_group_id or f"grp:{key}",
        dedupe_url_key=dedupe_url_key or f"https://x.example/{key}",
    )


# ── the exported ranking ─────────────────────────────────────────────────────


def test_rank_order_salary_certainty_then_cq_then_match() -> None:
    ranked = ranking.rank(
        [
            _candidate("possible", salary_outcome="PASS_POSSIBLE_HIGH"),
            _candidate("confirmed_low_cq", cq=55),
            _candidate("confirmed", cq=95),
            _candidate("likely", salary_outcome="PASS_LIKELY_HIGH"),
            _candidate("confirmed_low_match", cq=95, role=75),
        ]
    )
    assert [c.ref for c in ranked] == [
        "confirmed", "confirmed_low_match", "confirmed_low_cq",
        "likely", "possible",
    ]


def test_demoted_jobs_rank_after_everything_else() -> None:
    """D4 wide policy: UNKNOWN-salary passes ride at the bottom."""
    ranked = ranking.rank(
        [
            _candidate("demoted_strong", salary_outcome="UNKNOWN_INSUFFICIENT_EVIDENCE",
                       demoted=True, cq=95, role=95),
            _candidate("plain", salary_outcome="PASS_POSSIBLE_HIGH", cq=55, role=70),
        ]
    )
    assert [c.ref for c in ranked] == ["plain", "demoted_strong"]


def test_rank_trace_is_complete_and_versioned() -> None:
    traces = ranking.rank_traces(ranking.rank([_candidate("a"), _candidate("b", cq=55)]))

    assert traces["a"]["position"] == 1
    assert traces["b"]["position"] == 2
    assert traces["a"]["ranking_policy_version"] == ranking.RANKING_POLICY_VERSION
    assert "sort_key" in traces["a"]


def test_ranking_is_deterministic_on_ties() -> None:
    a = ranking.rank([_candidate("zz"), _candidate("aa")])
    b = ranking.rank([_candidate("aa"), _candidate("zz")])
    assert [c.ref for c in a] == [c.ref for c in b] == ["aa", "zz"]  # ref tiebreak


# ── suppression: repost-group level, fail-open, BEFORE the cap ───────────────


def _seed_suppression(
    owner: Session, tenant_id: str, key: str, group: str,
    *, expires: datetime,
) -> None:
    owner.execute(
        sql_text(
            "INSERT INTO tenant_job_suppressions "
            "(id, tenant_id, suppression_key, repost_group_id, delivered_at, expires_at) "
            "VALUES (:id, :tid, :key, :grp, :d, :e)"
        ),
        {"id": str(uuid.uuid4()), "tid": tenant_id, "key": key, "grp": group,
         "d": NOW - timedelta(days=1), "e": expires},
    )


def test_suppression_filters_before_the_cap(owner_engine: Engine) -> None:
    tenant_id = str(uuid.uuid4())
    with Session(owner_engine) as s:
        s.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
                  {"id": tenant_id, "c": f"TEN-R{uuid.uuid4().hex[:4]}"})
        _seed_suppression(
            s, tenant_id, "https://x.example/top", "grp:top",
            expires=NOW + timedelta(days=10),
        )
        _seed_suppression(  # expired → resurfaces
            s, tenant_id, "https://x.example/old", "grp:old",
            expires=NOW - timedelta(days=1),
        )
        s.commit()
        try:
            candidates = [
                _candidate("top"),                          # suppressed (active)
                _candidate("old", cq=90),                   # expired → eligible
                _candidate("fresh", cq=85),                 # unknown → eligible
            ]
            kept = ranking.filter_suppressed(
                s, tenant_id=uuid.UUID(tenant_id), candidates=candidates, now=NOW
            )
            assert [c.ref for c in kept] == ["old", "fresh"]
            # the suppressed job did NOT consume a slot: cap 2 still yields 2
            final = ranking.compose_final_list(
                ranking.rank(kept), daily_job_limit=2
            )
            assert len(final) == 2
        finally:
            s.execute(sql_text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            s.commit()


def test_recording_suppression_upserts_and_prunes(owner_engine: Engine) -> None:
    tenant_id = str(uuid.uuid4())
    with Session(owner_engine) as s:
        s.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
                  {"id": tenant_id, "c": f"TEN-R{uuid.uuid4().hex[:4]}"})
        _seed_suppression(
            s, tenant_id, "https://x.example/expired", "grp:e",
            expires=NOW - timedelta(days=2),
        )
        s.commit()
        try:
            candidate = _candidate("job1")
            ranking.record_suppression(
                s, tenant_id=uuid.UUID(tenant_id), candidate=candidate, now=NOW
            )
            ranking.record_suppression(  # idempotent refresh, not a crash
                s, tenant_id=uuid.UUID(tenant_id), candidate=candidate,
                now=NOW + timedelta(hours=1),
            )
            s.commit()
            rows = s.execute(
                sql_text(
                    "SELECT suppression_key, expires_at FROM tenant_job_suppressions "
                    "WHERE tenant_id = :tid ORDER BY suppression_key"
                ),
                {"tid": tenant_id},
            ).all()
            assert [r.suppression_key for r in rows] == ["https://x.example/job1"]
            assert rows[0].expires_at == NOW + timedelta(hours=1) + timedelta(
                days=ranking.SUPPRESSION_TTL_DAYS
            )
        finally:
            s.execute(sql_text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            s.commit()


# ── slots: deterministic composition, top pick untouched ─────────────────────


def test_slots_guarantee_diversity_without_displacing_the_top() -> None:
    ranked = ranking.rank([
        _candidate("c1", cq=95, role=98),                       # top — untouchable
        _candidate("c2", cq=95, role=97),
        _candidate("c3", cq=95, role=96),
        _candidate("c4", cq=95, role=95),
        _candidate("arabic", cq=60, role=80, title="محلل أعمال أول",
                   company_tier=3),
        _candidate("jobspy_one", cq=55, role=78, source="jobspy", company_tier=3),
        _candidate("fresh_one", cq=50, role=75, company_tier=3,
                   posted_at=NOW - timedelta(hours=12)),
    ])
    composition = ranking.compose_final_list(ranked, daily_job_limit=4, now=NOW)
    refs = composition.refs()
    assert len(refs) == 4
    assert refs[0] == "c1"                       # the top pick is never displaced
    assert "arabic" in refs                      # إعلانات عربية slot
    assert "jobspy_one" in refs                  # تنوع مصادر slot
    traces = ranking.rank_traces(ranked, composition=composition)
    assert traces["arabic"]["slot"] == "arabic_ad"
    assert traces["jobspy_one"]["slot"] == "source_diversity"


def test_small_limits_just_take_the_top() -> None:
    ranked = ranking.rank([
        _candidate("best", cq=95), _candidate("arabic", title="محلل أعمال", cq=50),
    ])
    composition = ranking.compose_final_list(ranked, daily_job_limit=1, now=NOW)
    assert composition.refs() == ["best"]        # N=1: slots never override rank


def test_cut_respects_the_plan_limit() -> None:
    ranked = ranking.rank([_candidate(f"c{i}", role=99 - i) for i in range(8)])
    assert len(ranking.compose_final_list(ranked, daily_job_limit=3, now=NOW)) == 3
