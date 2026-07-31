"""The nightly run — the binding §06 order, composed end to end.

Discover once for everyone, evaluate per tenant: families (D5, dynamic) →
the TWO production fetchers, each isolated in its own try/except with a
SANITIZED error status (exception class name only, never the message —
provider messages can carry keys) → same-run dedupe (§8.1) → the retrieval
cap → pool upsert by URL identity → enrichment for the top slice (cached,
once per posting) → per-tenant gate + decision log → suppression (before the
cap) → exported ranking → slots → the plan cut. Honest run statuses:
completed / partial (one source failed) / discovery_failed (all failed) /
no_active_tenants. digest_only (D9) is recorded — no send path exists in the
engine at all; delivery is C7's job.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import (
    DiscoveryRun,
    JobPosting,
    SearchPolicy,
    Tenant,
    TenantJobDecision,
)
from career.engine import identity as engine_identity
from career.engine import ranking as engine_ranking
from career.engine.enrichment import PageFetcher, enrich_posting
from career.engine.families import QueryFamily, derive_query_families
from career.engine.gate import (
    GateVerdict,
    PostingFacts,
    TenantGatePolicy,
    evaluate,
    persist_decision,
)
from career.engine.ranking import RANKING_POLICY_VERSION, RankCandidate
from career.engine.sources import (
    DiscoveredJob,
    JobSpyClient,
    SearchApiClient,
    fetch_google_jobs,
    fetch_jobspy,
)
from career_core.ssrf import Resolver

logger = logging.getLogger("career.engine")

#: §06: «قائمة استرجاع رخيصة 40–50 → إثراء محدود 20–30» — start points, wired
#: from Settings by the systemd entry point, never buried constants.
DEFAULT_RETRIEVAL_CAP = 50
DEFAULT_ENRICH_CAP = 30
DEFAULT_MAX_PER_QUERY = 5


@dataclass(frozen=True)
class RunReport:
    run_id: uuid.UUID
    status: str
    counts: dict[str, Any]
    per_tenant: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)


def _gate_policy_of(policy: SearchPolicy) -> TenantGatePolicy:
    cities = (policy.cities or {}).get("cities", [])
    return TenantGatePolicy(
        approved_paths=dict(policy.approved_paths or {}),
        cities_ar=tuple(cities),
        willing_to_relocate=bool((policy.cities or {}).get("willing_to_relocate")),
        region_ar=(policy.cities or {}).get("region"),
        remote_policy=policy.remote_policy,
        min_salary_sar=float(policy.min_salary_sar) if policy.min_salary_sar else None,
        unknown_salary_policy=policy.unknown_salary_policy,
        daily_job_limit=policy.daily_job_limit,
    )


def interleave_by_source(jobs: list[DiscoveredJob]) -> list[DiscoveredJob]:
    """Round-robin across sources so the retrieval cap is shared fairly —
    discovery order used to let a full google batch starve jobspy entirely
    (audit fix). Order within each source is preserved."""
    lanes: dict[str, list[DiscoveredJob]] = {}
    for job in jobs:
        lanes.setdefault(job.source, []).append(job)
    out: list[DiscoveredJob] = []
    i = 0
    while True:
        added = False
        for lane in lanes.values():
            if i < len(lane):
                out.append(lane[i])
                added = True
        if not added:
            return out
        i += 1


def _discover(
    families: list[QueryFamily],
    searchapi: SearchApiClient,
    jobspy_client: JobSpyClient,
    max_per_query: int,
) -> tuple[list[DiscoveredJob], dict[str, Any]]:
    """The digest path (§5.2): exactly two fetchers, each isolated — a failing
    source degrades the run, never crashes it, and its reason is sanitized."""
    jobs: list[DiscoveredJob] = []
    sources: dict[str, Any] = {}

    try:
        fetched = 0
        skips_total: dict[str, int] = {}
        for family in families:
            family_jobs, skips = fetch_google_jobs(
                searchapi, aliases=family.aliases, locations=family.locations,
                family=family.family, max_per_query=max_per_query,
            )
            jobs.extend(family_jobs)
            fetched += len(family_jobs)
            for key, value in skips.items():
                skips_total[key] = skips_total.get(key, 0) + value
        sources["searchapi_google_jobs"] = {
            "status": "ok", "fetched": fetched, "skips": skips_total,
        }
    except Exception as exc:  # noqa: BLE001 — isolation is the contract (§5.2)
        logger.warning("google_jobs discovery failed", exc_info=True)
        sources["searchapi_google_jobs"] = {
            "status": "error", "reason": type(exc).__name__,  # sanitized
        }

    try:
        fetched = 0
        skips_total = {}
        for family in families:
            family_jobs, skips = fetch_jobspy(
                jobspy_client, aliases=family.aliases, family=family.family,
                max_results=max_per_query,
            )
            jobs.extend(family_jobs)
            fetched += len(family_jobs)
            for key, value in skips.items():
                skips_total[key] = skips_total.get(key, 0) + value
        sources["jobspy"] = {"status": "ok", "fetched": fetched, "skips": skips_total}
    except Exception as exc:  # noqa: BLE001
        logger.warning("jobspy discovery failed", exc_info=True)
        sources["jobspy"] = {"status": "error", "reason": type(exc).__name__}

    return jobs, sources


#: §06 posting window for the repost quadruple — beyond it, a same-looking
#: posting is treated as a fresh opening, not a repost.
REPOST_WINDOW_DAYS = 14


def _repost_group_for(
    owner_session: Session,
    job: DiscoveredJob,
    ids: engine_identity.ExtendedIds,
    now: datetime,
) -> tuple[str | None, bool]:
    """(group_id, merged) — audit fix: should_merge was never called in the
    pipeline, so cross-source reposts always formed separate groups. A new
    posting whose fingerprint matches a pool row seen within the §06 window
    AND passes the conservative quadruple adopts that row's group."""
    if not ids.cross_source_fingerprint:
        return ids.repost_group_id, False
    candidate = owner_session.execute(
        select(JobPosting).where(
            JobPosting.cross_source_fingerprint == ids.cross_source_fingerprint,
            JobPosting.last_seen_at >= now - timedelta(days=REPOST_WINDOW_DAYS),
        ).order_by(JobPosting.last_seen_at.desc())
    ).scalars().first()
    if candidate is None:
        return ids.repost_group_id, False
    cand_job = DiscoveredJob(
        title=candidate.title, company=candidate.company, url=candidate.url,
        source=candidate.source, family=job.family, location=candidate.location,
    )
    cand_ids = engine_identity.ExtendedIds(
        url_identity=candidate.url_identity,
        cross_source_fingerprint=candidate.cross_source_fingerprint or "",
        repost_group_id=candidate.repost_group_id,
        dedupe_url_key=None,
    )
    if engine_identity.should_merge(ids, job, cand_ids, cand_job):
        return candidate.repost_group_id, True
    return ids.repost_group_id, False


def _upsert_pool(
    owner_session: Session, jobs: list[DiscoveredJob], now: datetime,
) -> tuple[list[JobPosting], dict[str, int]]:
    postings: list[JobPosting] = []
    counts = {"new": 0, "seen_again": 0, "unusable_url": 0, "merged_groups": 0}
    for job in jobs:
        ids = engine_identity.derive_ids(job)
        if ids.url_identity is None:
            counts["unusable_url"] += 1
            continue
        existing = owner_session.execute(
            select(JobPosting).where(JobPosting.url_identity == ids.url_identity)
        ).scalar_one_or_none()
        if existing is not None:
            existing.last_seen_at = now
            postings.append(existing)
            counts["seen_again"] += 1
            continue
        group_id, merged = _repost_group_for(owner_session, job, ids, now)
        if merged:
            counts["merged_groups"] += 1
        posting = JobPosting(
            id=uuid.uuid4(),
            url_identity=ids.url_identity,
            url=job.url,
            canonical_url=(job.route or {}).get("canonical_apply_url"),
            source_native_id=job.source_native_id,
            cross_source_fingerprint=ids.cross_source_fingerprint,
            repost_group_id=group_id,
            title=job.title,
            company=job.company,
            location=job.location,
            description_snippet=job.description,
            salary_raw=job.salary_raw,
            source=job.source,
            route=job.route or {},
            posted_at=_parse_posted_at(job.posted_at_raw, now=now),
            first_seen_at=now,
            last_seen_at=now,
        )
        owner_session.add(posting)
        postings.append(posting)
        counts["new"] += 1
    owner_session.flush()
    return postings, counts



_RELATIVE_POSTED_RE = re.compile(
    r"(\d+)\s*(hour|day|week|month)s?\s*ago", re.IGNORECASE
)


def _parse_posted_at(raw: str | None, *, now: datetime) -> datetime | None:
    """AUDIT ك-16: fetchers collected posted_at_raw but it was dropped at
    insert — the §06 freshness lane never fired in production. Handles the
    two real shapes: ISO dates (JobSpy date_posted) and Google's relative
    "N days ago" strings."""
    if not raw:
        return None
    text = str(raw).strip()
    m = _RELATIVE_POSTED_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        hours = {"hour": 1, "day": 24, "week": 168, "month": 720}[unit] * n
        return now - timedelta(hours=hours)
    try:
        parsed = datetime.fromisoformat(text[:19])
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _to_candidate(posting: JobPosting, verdict: GateVerdict) -> RankCandidate:
    return RankCandidate(
        ref=str(posting.id),
        salary_outcome=verdict.reasons["salary_outcome"],
        cq_score=verdict.reasons["cq_score"],
        role_score=verdict.reasons["role_score"],
        demoted=bool(verdict.reasons.get("demoted")),
        company_tier=verdict.reasons["company_tier"],
        title=posting.title,
        source=posting.source,
        posted_at=posting.posted_at,
        repost_group_id=posting.repost_group_id,
        dedupe_url_key=engine_identity._dedupe_url_key(posting.url),
    )


def run_nightly(
    owner_session: Session,
    *,
    searchapi: SearchApiClient,
    jobspy_client: JobSpyClient,
    fetcher: PageFetcher,
    now: datetime,
    tenant_ids: list[uuid.UUID] | None = None,
    resolver: Resolver | None = None,
    digest_only: bool = True,
    max_per_query: int = DEFAULT_MAX_PER_QUERY,
    retrieval_cap: int = DEFAULT_RETRIEVAL_CAP,
    enrich_cap: int = DEFAULT_ENRICH_CAP,
) -> RunReport:
    """One nightly run in the binding §06 order. Runs as the owner role."""
    run = DiscoveryRun(id=uuid.uuid4(), run_date=now.date(), digest_only=digest_only)
    owner_session.add(run)
    owner_session.flush()
    counts: dict[str, Any] = {}

    def _finish(status: str, per_tenant: dict[uuid.UUID, dict[str, Any]]) -> RunReport:
        run.status = status
        run.counts = counts
        run.finished_at = now
        owner_session.commit()
        return RunReport(run.id, status, counts, per_tenant)

    # 1) planning — dynamic families from ACTIVE tenants (D5)
    families = derive_query_families(owner_session, tenant_ids=tenant_ids)
    counts["families"] = [f.family for f in families]
    if not families:
        return _finish("no_active_tenants", {})

    # 2) discovery — two isolated fetchers
    jobs, sources = _discover(families, searchapi, jobspy_client, max_per_query)
    counts["sources"] = sources
    failed_sources = sum(1 for s in sources.values() if s["status"] == "error")
    if failed_sources == len(sources):
        return _finish("discovery_failed", {})

    # 3) same-run dedupe (§8.1) + the cheap-retrieval cap.
    # Fairness (audit fix): interleave sources round-robin BEFORE the cap —
    # discovery order used to let a full google batch starve jobspy rows out
    # of the pool entirely.
    deduped, removed = engine_identity.dedupe_same_run(jobs)
    counts["dedupe_removed"] = removed
    retrieval = interleave_by_source(deduped)[:retrieval_cap]
    counts["retrieval"] = len(retrieval)
    counts["retrieval_by_source"] = {}
    for job in retrieval:
        counts["retrieval_by_source"][job.source] = (
            counts["retrieval_by_source"].get(job.source, 0) + 1
        )

    # 4) pool upsert by identity
    postings, pool_counts = _upsert_pool(owner_session, retrieval, now)
    counts["pool"] = pool_counts

    # 5) enrichment — top slice, cached once per posting
    enriched = 0
    for posting in postings[:enrich_cap]:
        if not posting.description_snippet:  # already have salary/JD evidence?
            enrich_posting(
                owner_session, posting_id=posting.id, fetcher=fetcher,
                resolver=resolver, now=now,
            )
            enriched += 1
    counts["enriched"] = enriched

    # 6) per-tenant: gate → decision log → suppression → rank → slots → cut
    tenant_union: set[uuid.UUID] = set()
    for family in families:
        tenant_union.update(family.tenant_ids)

    per_tenant: dict[uuid.UUID, dict[str, Any]] = {}
    for tenant_id in sorted(tenant_union, key=str):
        policy_row = owner_session.execute(
            select(SearchPolicy).where(
                SearchPolicy.tenant_id == tenant_id, SearchPolicy.status == "active"
            )
        ).scalar_one_or_none()
        if policy_row is None:
            continue
        policy = _gate_policy_of(policy_row)

        passed: list[tuple[JobPosting, GateVerdict]] = []
        decisions: dict[str, Any] = {"evaluated": 0, "passed": 0, "near_miss": 0}
        rows_by_ref: dict[str, TenantJobDecision] = {}
        for posting in postings:
            facts = PostingFacts(
                title=posting.title,
                company=posting.company,
                url=posting.url,
                location=posting.location,
                jd_text=posting.jd_snippet or posting.description_snippet,
            )
            verdict = evaluate(policy, facts)
            row = persist_decision(
                owner_session, tenant_id=tenant_id, run_id=run.id,
                job_posting_id=posting.id, verdict=verdict,
            )
            rows_by_ref[str(posting.id)] = row
            decisions["evaluated"] += 1
            if verdict.decision == "PASS":
                passed.append((posting, verdict))
                decisions["passed"] += 1
            elif verdict.near_miss:
                decisions["near_miss"] += 1

        candidates = [_to_candidate(p, v) for p, v in passed]
        eligible = engine_ranking.filter_suppressed(
            owner_session, tenant_id=tenant_id, candidates=candidates, now=now
        )
        decisions["suppressed"] = len(candidates) - len(eligible)
        ranked = engine_ranking.rank(eligible)
        # `or 1` masked a snapshotted 0 into 1 — a plan with no daily
        # entitlement silently received one job a day. Only an ABSENT limit
        # falls back; a real 0 composes an empty list and the day closes
        # honestly as NO_MATCHES (§15.12).
        daily_limit = policy.daily_job_limit
        composition = engine_ranking.compose_final_list(
            ranked, daily_job_limit=1 if daily_limit is None else daily_limit,
            now=now,
        )
        traces = engine_ranking.rank_traces(ranked, composition=composition)

        # stamp the Rank Trace onto the PASS decision rows (§06)
        for ref, trace in traces.items():
            decision_row = rows_by_ref.get(ref)
            if decision_row is not None:
                decision_row.rank = trace
                decision_row.ranking_policy_version = RANKING_POLICY_VERSION
        owner_session.flush()

        postings_by_id = {str(p.id): p for p in postings}
        per_tenant[tenant_id] = {
            "final": [
                {"posting_id": ref, "url": postings_by_id[ref].url,
                 "title": postings_by_id[ref].title, "slot": composition.slots.get(ref)}
                for ref in composition.refs()
            ],
            "counts": decisions,
        }
        # AUDIT ك-17: journal + admin alerts print these counts — key by
        # TEN code, never the raw tenant uuid (§15.13).
        tenant_row = owner_session.get(Tenant, tenant_id)
        tenant_key = tenant_row.code if tenant_row is not None else "TEN-????"
        counts.setdefault("tenants", {})[tenant_key] = decisions

    return _finish("partial" if failed_sources else "completed", per_tenant)
