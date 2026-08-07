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
from career_core.identity import derive_canonical_job_identity

NOW = datetime(2026, 7, 15, 23, 30, tzinfo=UTC)


def _candidate(
    key: str, *, salary_outcome: str = "PASS_CONFIRMED", cq: int = 95,
    role: int = 90, demoted: bool = False, company_tier: int = 1,
    title: str = "Business Analyst", source: str = "searchapi_google_jobs",
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


def _seed_posting(
    owner: Session, *, url: str, group: str, source: str = "searchapi_google_jobs"
) -> None:
    """One posting in the shared pool, stored with its URL exactly as the
    source handed it over — which is what engine.run does."""
    owner.execute(
        sql_text(
            "INSERT INTO job_postings "
            "(id, url_identity, url, repost_group_id, title, company, source, "
            " route, first_seen_at, last_seen_at) "
            "VALUES (:id, :ident, :url, :grp, :t, :c, :s, '{}'::jsonb, :n, :n)"
        ),
        {"id": str(uuid.uuid4()),
         "ident": derive_canonical_job_identity(url),
         "url": url, "grp": group, "t": "Business Analyst",
         "c": "Example Co", "s": source, "n": NOW},
    )


def test_tracked_delivered_url_still_suppresses_the_whole_repost_group(
    owner_engine: Engine,
) -> None:
    """A delivered link carrying tracking parameters must suppress the GROUP.

    The repost group used to be resolved with ``JobPosting.url == url`` — a raw
    string comparison against the URL the source handed over. A delivered link
    picks up ``utm_*``/``gclid`` and a host the customer's client lower-cases,
    so the exact match found nothing, the suppression row landed with a NULL
    group, and group-level suppression silently degraded to URL-only: the very
    same job, discovered again under its repost's URL, came back to the
    customer inside the TTL with nothing reporting it.
    """
    tenant_id = str(uuid.uuid4())
    group = "joburl:v1:" + "a" * 64          # the shared group of one repost pair
    delivered_raw = "https://jobs.example.com/Careers/BA-7712"
    # what actually left the building: the same job, tracked and lower-cased
    delivered_tracked = (
        "https://jobs.example.com/careers/BA-7712"
        "?utm_source=whatsapp&utm_campaign=daily&gclid=Cj0KxYZ/"
    )
    repost_url = "https://aggregator.example.com/j/9931"
    with Session(owner_engine) as s:
        s.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
                  {"id": tenant_id, "c": f"TEN-R{uuid.uuid4().hex[:4]}"})
        _seed_posting(s, url=delivered_raw, group=group)
        _seed_posting(s, url=repost_url, group=group, source="jobspy")
        s.commit()
        try:
            ranking.record_suppression_by_url(
                s, tenant_id=uuid.UUID(tenant_id), url=delivered_tracked, now=NOW
            )
            s.commit()
            row = s.execute(
                sql_text(
                    "SELECT suppression_key, repost_group_id "
                    "FROM tenant_job_suppressions WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).one()
            # the group was resolved DESPITE the tracking parameters
            assert row.repost_group_id == group
            # …and the key is still the §8.1 normalized delivered key
            assert row.suppression_key == "https://jobs.example.com/careers/ba-7712"

            # the repost — a DIFFERENT url, same group — must not come back
            repost = _candidate(
                "repost", repost_group_id=group, dedupe_url_key=repost_url,
            )
            kept = ranking.filter_suppressed(
                s, tenant_id=uuid.UUID(tenant_id), candidates=[repost], now=NOW
            )
            assert kept == []
        finally:
            s.rollback()
            s.execute(sql_text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            s.execute(
                sql_text("DELETE FROM job_postings WHERE repost_group_id = :g"),
                {"g": group},
            )
            s.commit()


def test_unusable_delivered_url_never_crashes_the_close(owner_engine: Engine) -> None:
    """Fail-open at the identity boundary: a delivered string that is not an
    http(s) URL has no posting to find, so the row is written with the URL key
    alone rather than raising inside the day's ledger write (§8.1/§15.12)."""
    tenant_id = str(uuid.uuid4())
    with Session(owner_engine) as s:
        s.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
                  {"id": tenant_id, "c": f"TEN-R{uuid.uuid4().hex[:4]}"})
        s.commit()
        try:
            ranking.record_suppression_by_url(
                s, tenant_id=uuid.UUID(tenant_id), url="ftp://x.example/j/1", now=NOW
            )
            s.commit()
            row = s.execute(
                sql_text(
                    "SELECT suppression_key, repost_group_id "
                    "FROM tenant_job_suppressions WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).one()
            assert row.suppression_key == "ftp://x.example/j/1"
            assert row.repost_group_id is None
        finally:
            s.rollback()
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
