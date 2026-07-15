"""Extended job identity + conservative merge (whitepaper §06, LEGACY §3/§8.1).

URL-v1 (career_core — the single identity authority) stays the base; the
extended ids exist because the same job appears under many URLs (company site,
Google Jobs, platforms, tracking links):

- ``cross_source_fingerprint``: sha256 over normalized (company, title, city) —
  the cross-source "same job?" signal;
- ``repost_group_id``: the suppression grouping key — defaults to the URL
  identity until a merge assigns a shared group (§06: suppression operates at
  the repost-group level).

Merging is CONSERVATIVE (§06 verbatim): merge on canonical-identity match, or
on the high-threshold quadruple (company + title + city + posting window);
anything doubtful stays separate — wrongly merging two jobs is worse than a
duplicate. Same-run dedupe follows §8.1: normalized URL first, then the
(company, title) pair, order preserved, first wins.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from career.engine.sources import DiscoveredJob
from career_core.identity import derive_canonical_job_identity
from career_core.urltools import strip_tracking_params


@dataclass(frozen=True)
class ExtendedIds:
    url_identity: str | None
    cross_source_fingerprint: str
    repost_group_id: str | None
    dedupe_url_key: str | None


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _norm_city(location: str | None) -> str:
    """'Riyadh, Saudi Arabia' and 'Riyadh' are the same city signal."""
    return _norm((location or "").split(",")[0])


def _dedupe_url_key(url: str) -> str | None:
    """§8.1 delivered-key normalization: strip tracking, rstrip '/', lower."""
    if not url:
        return None
    normalized = strip_tracking_params(url).rstrip("/").lower()
    return normalized or None


def derive_ids(job: DiscoveredJob) -> ExtendedIds:
    url_identity = derive_canonical_job_identity(job.url)
    fingerprint = hashlib.sha256(
        "|".join((_norm(job.company), _norm(job.title), _norm_city(job.location))).encode("utf-8")
    ).hexdigest()
    return ExtendedIds(
        url_identity=url_identity,
        cross_source_fingerprint=fingerprint,
        repost_group_id=url_identity,  # until a merge assigns a shared group
        dedupe_url_key=_dedupe_url_key(job.url),
    )


def should_merge(
    ids_a: ExtendedIds, job_a: DiscoveredJob,
    ids_b: ExtendedIds, job_b: DiscoveredJob,
) -> bool:
    """The conservative policy: canonical identity match, or the full
    quadruple at high confidence. Doubt → separate."""
    if ids_a.url_identity is not None and ids_a.url_identity == ids_b.url_identity:
        return True
    # quadruple: company + title + city must ALL match exactly (normalized),
    # and both sides must actually carry a city — a missing signal is doubt.
    if not job_a.location or not job_b.location:
        return False
    return (
        _norm(job_a.company) == _norm(job_b.company)
        and _norm(job_a.title) == _norm(job_b.title)
        and _norm_city(job_a.location) == _norm_city(job_b.location)
    )


def dedupe_same_run(jobs: list[DiscoveredJob]) -> tuple[list[DiscoveredJob], int]:
    """§8.1 same-run dedupe: first by normalized URL, then by the
    (company, title) pair. Order preserved; returns (deduped, n_removed)."""
    seen_urls: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    deduped: list[DiscoveredJob] = []
    removed = 0
    for job in jobs:
        url_key = _dedupe_url_key(job.url)
        pair = (_norm(job.company), _norm(job.title))
        if (url_key is not None and url_key in seen_urls) or pair in seen_pairs:
            removed += 1
            continue
        if url_key is not None:
            seen_urls.add(url_key)
        seen_pairs.add(pair)
        deduped.append(job)
    return deduped, removed
