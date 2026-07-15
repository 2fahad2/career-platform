"""JD enrichment — LEGACY §6 ported verbatim, cached once per posting (§06).

HTTP GET only — no browser, no JS, no forms, no credentials. 10s timeout,
200KB body cap, 10K snippet. One policy governs the whole fetch: the unsafe-
domain list and the fail-closed SSRF check (career_core) run on BOTH the
initial URL and the final redirect target. Status vocabulary is exact; a
posting is enriched once regardless of how many tenants match it — the pool
row is the cache.

The transport is an injectable Protocol so every §6 branch is tested with no
network; the stdlib implementation mirrors the legacy fetch (browser-like
headers, redirects followed by the client, final URL surfaced for re-check).
"""

from __future__ import annotations

import re
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import JobPosting
from career_core.ssrf import Resolver, is_public_url, is_unsafe_domain

# §6 constants — verbatim.
JD_TIMEOUT_SECONDS = 10.0
JD_MAX_BYTES = 200_000
JD_SNIPPET_CHARS = 10_000

# §6 status vocabulary — verbatim.
JD_ENRICH_NOT_REQUESTED = "NOT_REQUESTED"
JD_ENRICH_SUCCESS = "SUCCESS"
JD_ENRICH_FAILED_TIMEOUT = "FAILED_TIMEOUT"
JD_ENRICH_FAILED_HTTP = "FAILED_HTTP"
JD_ENRICH_FAILED_UNSAFE = "FAILED_UNSAFE_SOURCE"
JD_ENRICH_FAILED_TOO_LARGE = "FAILED_TOO_LARGE"
JD_ENRICH_FAILED_UNKNOWN = "FAILED_UNKNOWN"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CareerDigest/1.0)",
    "Accept": "text/html,text/plain;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True)
class FetchedPage:
    final_url: str
    status: int
    content_type: str
    body: bytes


class PageFetcher(Protocol):
    def get(self, url: str, *, timeout: float) -> FetchedPage: ...


@dataclass(frozen=True)
class EnrichResult:
    status: str
    snippet: str | None = None


_TAG_RE = re.compile(r"<[^>]{0,300}>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def safe_fetch_jd(
    url: str,
    *,
    fetcher: PageFetcher,
    resolver: Resolver | None = None,
) -> EnrichResult:
    """The §6 flow, step by step — each guard in its documented order."""
    if not url:
        return EnrichResult(JD_ENRICH_FAILED_UNKNOWN)
    if is_unsafe_domain(url):
        return EnrichResult(JD_ENRICH_FAILED_UNSAFE)
    ssrf_kwargs = {"resolver": resolver} if resolver is not None else {}
    if not is_public_url(url, **ssrf_kwargs):
        return EnrichResult(JD_ENRICH_FAILED_UNSAFE)

    try:
        page = fetcher.get(url, timeout=JD_TIMEOUT_SECONDS)
    except TimeoutError:
        return EnrichResult(JD_ENRICH_FAILED_TIMEOUT)
    except Exception:  # noqa: BLE001 — §6: any other failure is UNKNOWN
        return EnrichResult(JD_ENRICH_FAILED_UNKNOWN)

    # One policy governs the whole fetch: re-check the FINAL redirect target.
    final = page.final_url or ""
    if (
        not final
        or is_unsafe_domain(final)
        or not is_public_url(final, **ssrf_kwargs)
    ):
        return EnrichResult(JD_ENRICH_FAILED_UNSAFE)
    if page.status not in (200, 203):
        return EnrichResult(JD_ENRICH_FAILED_HTTP)
    content_type = (page.content_type or "").lower()
    if "text" not in content_type and "json" not in content_type:
        return EnrichResult(JD_ENRICH_FAILED_UNSAFE)
    if len(page.body) > JD_MAX_BYTES:
        return EnrichResult(JD_ENRICH_FAILED_TOO_LARGE)

    return EnrichResult(JD_ENRICH_SUCCESS, snippet=_strip_tags(page.body)[:JD_SNIPPET_CHARS])


class UrllibPageFetcher:  # pragma: no cover — exercised live in C6's gate
    """Stdlib GET mirroring the legacy fetch: browser-like headers, redirects
    followed, the final URL surfaced so the caller re-checks it."""

    def get(self, url: str, *, timeout: float) -> FetchedPage:
        request = urllib.request.Request(url, headers=_HEADERS)  # noqa: S310
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(JD_MAX_BYTES + 1)
            return FetchedPage(
                final_url=resp.geturl(),
                status=resp.status,
                content_type=resp.headers.get("Content-Type", ""),
                body=body,
            )


# ── the pool cache: enrich once per posting (whitepaper §06) ─────────────────


def enrich_posting(
    owner_session: Session,
    *,
    posting_id: uuid.UUID,
    fetcher: PageFetcher,
    now: datetime,
    resolver: Resolver | None = None,
) -> EnrichResult:
    """Fetch the JD once and record the verdict on the pool row — an already-
    enriched posting is served from the cache, whatever its verdict was
    (an honest failure is a verdict too, §15.12)."""
    posting = owner_session.execute(
        select(JobPosting).where(JobPosting.id == posting_id)
    ).scalar_one()
    if posting.jd_status != JD_ENRICH_NOT_REQUESTED:
        return EnrichResult(posting.jd_status, snippet=posting.jd_snippet)

    result = safe_fetch_jd(posting.url, fetcher=fetcher, resolver=resolver)
    posting.jd_status = result.status
    posting.jd_snippet = result.snippet
    posting.enriched_at = now
    owner_session.flush()
    return result
