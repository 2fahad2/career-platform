"""Ranking, suppression, slots and the cut (whitepaper §06).

Deterministic rules decided eligibility (the gate); this stage only reorders
and selects what passed — the policy is exported as
:data:`RANKING_POLICY_VERSION` and every job gets a Rank Trace.

- **Order**: the legacy key (salary certainty → company quality → role match)
  with two additions: D4-demoted passes ride at the bottom, and ties break on
  the stable ref so runs are reproducible.
- **Suppression** (D3 port of §8.1): repost-group level within the TTL,
  fail-open (unknown jobs are never treated as delivered; expired entries
  resurface), filtered BEFORE the cap so a suppressed job never consumes a
  limited slot. Recording upserts (refresh on re-delivery) and prunes expired
  rows on write.
- **Slots** (§06): strong companies / Arabic ads / source diversity / very
  fresh / a small exploration quota — composed deterministically by swapping
  candidates in from below the cut, never displacing the top pick, each swap
  recorded in the trace.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from career.db.models import TenantJobSuppression
from career_core.gate import gate_sort_key

RANKING_POLICY_VERSION = "v1"
SUPPRESSION_TTL_DAYS = 30

_ARABIC_RE = re.compile(r"[؀-ۿ]")
_VERY_FRESH = timedelta(hours=48)


@dataclass(frozen=True)
class RankCandidate:
    """The ranking-relevant slice of a gate-passed job."""

    ref: str                       # opaque id (posting id / url) — stable tiebreak
    salary_outcome: str
    cq_score: int
    role_score: int
    demoted: bool
    company_tier: int
    title: str
    source: str
    posted_at: datetime | None
    repost_group_id: str | None
    dedupe_url_key: str | None


def _sort_key(candidate: RankCandidate) -> tuple[int, tuple[int, int, int], str]:
    return (
        1 if candidate.demoted else 0,   # D4 wide: بترتيب أدنى
        gate_sort_key(candidate.salary_outcome, candidate.cq_score, candidate.role_score),
        candidate.ref,                   # stable, reproducible ties
    )


def rank(candidates: list[RankCandidate]) -> list[RankCandidate]:
    return sorted(candidates, key=_sort_key)


# ── suppression (fail-open, before the cap) ──────────────────────────────────


def filter_suppressed(
    owner_session: Session,
    *,
    tenant_id: uuid.UUID,
    candidates: list[RankCandidate],
    now: datetime,
) -> list[RankCandidate]:
    """Drop candidates whose repost group or URL key is actively suppressed.
    Fail-open semantics: only a positive, unexpired match suppresses —
    unknown jobs are never treated as delivered (§8.1)."""
    rows = owner_session.execute(
        select(
            TenantJobSuppression.suppression_key,
            TenantJobSuppression.repost_group_id,
        ).where(
            TenantJobSuppression.tenant_id == tenant_id,
            TenantJobSuppression.expires_at > now,
        )
    ).all()
    keys = {r.suppression_key for r in rows}
    groups = {r.repost_group_id for r in rows if r.repost_group_id}
    return [
        c
        for c in candidates
        if not (
            (c.repost_group_id and c.repost_group_id in groups)
            or (c.dedupe_url_key and c.dedupe_url_key in keys)
        )
    ]


def record_suppression(
    owner_session: Session,
    *,
    tenant_id: uuid.UUID,
    candidate: RankCandidate,
    now: datetime,
    ttl_days: int = SUPPRESSION_TTL_DAYS,
) -> None:
    """Record a CONFIRMED delivery (C7 calls this after the ledger write).
    Upserts — a re-delivery refreshes the window — and prunes expired rows
    (§8.1 write semantics)."""
    record_suppression_by_url(
        owner_session, tenant_id=tenant_id,
        url=candidate.dedupe_url_key or "",
        repost_group_id=candidate.repost_group_id,
        now=now, ttl_days=ttl_days,
    )


def record_suppression_by_url(
    owner_session: Session,
    *,
    tenant_id: uuid.UUID,
    url: str,
    repost_group_id: str | None = None,
    now: datetime,
    ttl_days: int = SUPPRESSION_TTL_DAYS,
) -> None:
    """Same §8.1 semantics keyed directly by the delivered URL — what the C7
    daily close holds after a confirmed delivery."""
    from career.engine.identity import _dedupe_url_key

    candidate_key = _dedupe_url_key(url) if url else None
    if not candidate_key:
        return  # no usable key → never suppress (fail-open)
    if repost_group_id is None:
        # audit fix: the C7 close only knows the delivered URL — resolve the
        # posting's repost group here so group-level suppression actually
        # bites (rows used to land with a NULL group = URL-only suppression).
        from career.db.models import JobPosting

        repost_group_id = owner_session.execute(
            select(JobPosting.repost_group_id).where(JobPosting.url == url)
        ).scalar_one_or_none()
    owner_session.execute(
        delete(TenantJobSuppression).where(
            TenantJobSuppression.tenant_id == tenant_id,
            TenantJobSuppression.expires_at <= now,
        )
    )
    existing = owner_session.execute(
        select(TenantJobSuppression).where(
            TenantJobSuppression.tenant_id == tenant_id,
            TenantJobSuppression.suppression_key == candidate_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.delivered_at = now
        existing.expires_at = now + timedelta(days=ttl_days)
        existing.repost_group_id = repost_group_id
    else:
        owner_session.add(
            TenantJobSuppression(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                suppression_key=candidate_key,
                repost_group_id=repost_group_id,
                delivered_at=now,
                expires_at=now + timedelta(days=ttl_days),
            )
        )
    owner_session.flush()


# ── slots + the cut ──────────────────────────────────────────────────────────

_SLOT_ORDER = (
    "strong_company", "arabic_ad", "source_diversity", "very_fresh", "exploration",
)


def _slot_predicate(
    slot: str, selected: list[RankCandidate], now: datetime | None
) -> Callable[[RankCandidate], bool]:
    if slot == "strong_company":
        return lambda c: c.company_tier in (1, 2)
    if slot == "arabic_ad":
        return lambda c: bool(_ARABIC_RE.search(c.title))
    if slot == "source_diversity":
        present = {c.source for c in selected}
        return lambda c: c.source not in present
    if slot == "very_fresh":
        if now is None:
            return lambda c: False
        return lambda c: c.posted_at is not None and (now - c.posted_at) <= _VERY_FRESH
    # exploration: a small out-of-pattern quota — the best demoted candidate
    return lambda c: c.demoted


@dataclass(frozen=True)
class Composition:
    """The final selection plus the slot each swapped-in candidate filled."""

    selected: tuple[RankCandidate, ...]
    slots: dict[str, str]

    def __len__(self) -> int:
        return len(self.selected)

    def refs(self) -> list[str]:
        return [c.ref for c in self.selected]


def compose_final_list(
    ranked: list[RankCandidate],
    *,
    daily_job_limit: int,
    now: datetime | None = None,
) -> Composition:
    """Top-N by rank, then deterministic slot swaps: for each unsatisfied slot
    (in fixed order) the best matching candidate below the cut replaces the
    lowest non-protected selected item — position 0 is never displaced, and
    each swapped-in candidate is protected from later swaps. N=1 is always
    just the top pick."""
    limit = max(0, daily_job_limit)
    selected = list(ranked[:limit])
    if limit <= 1 or len(ranked) <= limit:
        return Composition(tuple(selected), {})

    protected: set[str] = {selected[0].ref} if selected else set()
    slotted: dict[str, str] = {}

    for slot in _SLOT_ORDER:
        predicate = _slot_predicate(slot, selected, now)
        if any(predicate(c) for c in selected):
            continue  # already satisfied naturally
        pool = [c for c in ranked[limit:] if predicate(c)]
        if not pool:
            continue  # nothing to guarantee — never invent
        incoming = pool[0]  # best-ranked matching candidate below the cut
        for i in range(len(selected) - 1, 0, -1):  # never touch position 0
            if selected[i].ref not in protected:
                selected[i] = incoming
                protected.add(incoming.ref)
                slotted[incoming.ref] = slot
                break

    # keep rank order within the final list
    return Composition(tuple(sorted(selected, key=_sort_key)), slotted)


def rank_traces(
    ranked: list[RankCandidate],
    *,
    composition: Composition | None = None,
) -> dict[str, dict[str, Any]]:
    """The exported Rank Trace: position, sort-key components, version, slot."""
    final_refs = set(composition.refs()) if composition is not None else None
    slots = composition.slots if composition is not None else {}
    traces: dict[str, dict[str, Any]] = {}
    for position, candidate in enumerate(ranked, start=1):
        traces[candidate.ref] = {
            "position": position,
            "sort_key": {
                "demoted": candidate.demoted,
                "salary_outcome": candidate.salary_outcome,
                "cq_score": candidate.cq_score,
                "role_score": candidate.role_score,
            },
            "ranking_policy_version": RANKING_POLICY_VERSION,
            "selected": (candidate.ref in final_refs) if final_refs is not None else None,
            "slot": slots.get(candidate.ref),
        }
    return traces
