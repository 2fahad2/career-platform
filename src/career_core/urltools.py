"""Single authority for URL normalization and tracking-param stripping.

Source: LEGACY §3 (URL-v1 normalization) + §8.1 (delivered key). The delivered
suppression key deliberately reuses normalize_job_url_v1 so same-run dedupe and
cross-day suppression can never diverge (bug P4-DUP-2). Never introduce a
second normalizer.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# The ONE tracking-param definition (LEGACY §3).
_UTM_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "google_jobs_apply", "ref", "fbclid", "gclid",
})


def strip_tracking_params(url: str) -> str:
    if not url:
        return url
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: v for k, v in qs.items() if k.lower() not in _UTM_PARAMS}
        return urlunparse(parsed._replace(query=urlencode(filtered, doseq=True)))
    except Exception:
        return url


def normalize_job_url_v1(raw_url: object) -> str:
    """Normalization steps, in order: trim → strip tracking → rstrip '/' → lower."""
    u = str(raw_url or "").strip()
    if not u:
        return ""
    return strip_tracking_params(u).rstrip("/").lower()


def delivered_key(job: Mapping[str, object]) -> str | None:
    """Cross-day suppression key — byte-identical to same-run dedupe (§8.1)."""
    raw_link = job.get("apply_link") or job.get("url") or ""
    if not raw_link:
        return None
    return normalize_job_url_v1(raw_link) or None
