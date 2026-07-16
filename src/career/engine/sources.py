"""Discovery source adapters — LEGACY §5.2/§5.3/§5.4 ported verbatim.

Two production fetchers (whitepaper §06: the digest path calls exactly these,
each isolated by the runner):

- **SearchAPI.io google_jobs** (provider per D13; same engine and §5.2 rules):
  row acceptance (title AND company required), URL preference
  ``apply_link > sharing_link`` (never a bare Google page), descriptions
  tag-stripped (the provider returns full but HTML-bearing text — live-probed)
  with job_highlights fallback, the LENIENT per-family title filter (positive
  signal → accept; else negative pattern → reject; neither → accept), and
  §5.4 apply-routing: aggregator landings are re-routed to the best
  ``apply_links`` destination (ats > employer > unknown > aggregator) and the
  canonical is structurally PROMOTED to the effective URL — otherwise
  identity/dedupe/binding key on a rotating aggregator page.
- **JobSpy (indeed only)**: 14-day window, one call with the family's aliases
  OR-joined, and the CONSERVATIVE two-tier post-filter (require hit → keep;
  reject hit → skip; a rule existed but neither matched → skip; no rule →
  keep). The optional dependency degrades to [] without breaking the app.

Skip reasons are counted, never silent (§15.12). Caps are parameters wired
from Settings by the runner — never constants buried here.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

# ── §5.4 apply routing ───────────────────────────────────────────────────────

_TYPE_PRIORITY = {"ats": 4, "employer": 3, "unknown": 1, "aggregator": 0}

_EMPLOYER_DOMAIN_SIGNALS = (
    "careers.", "career.", "jobs.", "recruiting.", "hire.", "apply.",
    "talent.", "joinus.",
)

#: Known ATS domains — derived from the §5.2 ATS-targeted site list.
_ATS_DOMAINS = (
    "workdayjobs.com", "myworkdayjobs.com", "greenhouse.io", "lever.co",
    "smartrecruiters.com", "taleo.net", "successfactors.com", "icims.com",
)

_AGGREGATOR_DOMAINS = frozenset(
    d
    for base in (
        "glassdoor.com", "linkedin.com", "bayt.com", "naukrigulf.com",
        "gulftalent.com", "monster.com", "ziprecruiter.com",
        "careerbuilder.com", "simplyhired.com", "reed.co.uk", "totaljobs.com",
        "expertini.com", "talent.com", "laimoon.com", "drjobpro.com",
        "wuzzuf.net", "tanqeeb.com", "akhtaboot.com", "rozee.pk",
        "jobrapido.com", "neuvoo.com", "jora.com", "adzuna.com",
        "whatjobs.com", "snagajob.com", "bebee.com", "jooble.org",
    )
    for d in (base, f"www.{base}")
)


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def classify_route(url: str) -> tuple[str, str]:
    """(route_type, confidence). Confidence is 'high' only for ats/employer."""
    domain = _domain(url)
    if not domain:
        return "unknown", "low"
    if domain in _AGGREGATOR_DOMAINS:
        return "aggregator", "low"
    if any(domain == a or domain.endswith("." + a) for a in _ATS_DOMAINS):
        return "ats", "high"
    if any(domain.startswith(sig) for sig in _EMPLOYER_DOMAIN_SIGNALS):
        return "employer", "high"
    return "unknown", "low"


def route_apply_url(item: dict[str, Any], selected_url: str) -> dict[str, Any]:
    """Pick the best apply destination — never returns None, never blocks a row
    (§5.4). Scans apply_options/apply_links entries (keys link/apply_link/url,
    must start with http) and keeps the highest-priority link."""
    best_url = selected_url
    best_type, best_conf = classify_route(selected_url)

    for options_key in ("apply_options", "apply_links"):
        for entry in item.get(options_key) or []:
            if not isinstance(entry, dict):
                continue
            for url_key in ("link", "apply_link", "url"):
                candidate = entry.get(url_key)
                if not isinstance(candidate, str) or not candidate.startswith("http"):
                    continue
                cand_type, cand_conf = classify_route(candidate)
                if _TYPE_PRIORITY[cand_type] > _TYPE_PRIORITY[best_type]:
                    best_url, best_type, best_conf = candidate, cand_type, cand_conf

    return {
        "original_apply_url": selected_url,
        "canonical_apply_url": best_url,
        "route_type": best_type,
        "route_confidence": best_conf,
        "canonical_domain": _domain(best_url),
        "phase": "discovery",
    }


def promote_canonical_apply_url(current_url: str, canonical: Any) -> str:
    """Structurally promote the canonical URL to the job's effective URL.

    Fails closed (keeps ``current_url``) for any non-str, empty, malformed,
    non-http(s), or hostless value; a usable current URL is never replaced by
    an empty value; never raises (§5.4)."""
    if not isinstance(canonical, str) or not canonical.strip():
        return current_url
    try:
        parsed = urlparse(canonical)
    except ValueError:
        return current_url
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return current_url
    return canonical


# ── the normalized discovered row ────────────────────────────────────────────


@dataclass(frozen=True)
class DiscoveredJob:
    title: str
    company: str
    url: str
    source: str
    family: str
    location: str | None = None
    description: str | None = None
    salary_raw: str | None = None
    source_native_id: str | None = None
    posted_at_raw: str | None = None
    route: dict[str, Any] = field(default_factory=dict)


# ── family title filters (data, keyed by family) ─────────────────────────────

#: Anchored noise-title rejects shared by the manager/analyst families (§5.2).
_SHARED_NEGATIVE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^sales manager", r"^store manager", r"^restaurant manager",
        r"^area manager", r"^regional manager", r"^procurement manager$",
        r"^warehouse manager", r"^kitchen manager", r"^outlet manager",
        r"^branch manager", r"^account manager$", r"^general manager$",
        r"^marketing manager$", r"^hr manager$", r"^finance manager$",
    )
)

#: JobSpy conservative post-filter (§5.3): analyst-noise rejects for the
#: business-analysis families — data_analyst legitimately wants some of these,
#: so the lists are per family, never global.
_JOBSPY_REJECTS: dict[str, tuple[re.Pattern[str], ...]] = {
    "business_analyst": tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\bdata analyst\b", r"\bfinancial analyst\b", r"\bcredit analyst\b",
            r"\brisk analyst\b", r"\bfraud analyst\b", r"\bhr analyst\b",
            r"\bmarketing analyst\b", r"\bsupply chain analyst\b",
            r"\binvestment analyst\b", r"\bresearch analyst\b",
            r"\bcompensation analyst\b", r"\bpayroll analyst\b",
        )
    ),
}


def _family_positive_tokens(family: str) -> tuple[str, ...]:
    from career.onboarding.paths import DEFAULT_FAMILIES

    for fam in DEFAULT_FAMILIES:
        if fam.key == family:
            return fam.title_tokens
    return ()


def _passes_lenient_family_filter(title: str, family: str) -> bool:
    """google_jobs filter (§5.2): positive → accept; else negative → reject;
    NEITHER → accept (lenient)."""
    title_l = title.lower()
    if any(tok in title_l for tok in _family_positive_tokens(family)):
        return True
    return not any(p.search(title_l) for p in _SHARED_NEGATIVE_PATTERNS)


def _passes_conservative_post_filter(title: str, family: str) -> bool:
    """JobSpy filter (§5.3), two-tier: require hit → keep; reject hit → skip;
    a rule existed but neither matched → skip; no rule at all → keep."""
    requires = _family_positive_tokens(family)
    rejects = _JOBSPY_REJECTS.get(family, ())
    if not requires and not rejects:
        return True  # permissive when no rule exists
    title_l = title.lower()
    if any(tok in title_l for tok in requires):
        return True
    if any(p.search(title_l) for p in rejects):
        return False
    return False  # a rule existed but neither matched → conservative skip


# ── SearchAPI.io google_jobs (production digest fetcher — D13) ────────────────


class SearchApiClient(Protocol):
    def search(self, params: dict[str, Any]) -> dict[str, Any]: ...


_HTML_TAG_RE = re.compile(r"<[^>]{0,300}>")
_HTML_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """Live-probed: SearchAPI descriptions carry HTML tags — strip them so the
    gate's text scoring never sees glued tokens."""
    return _HTML_WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", text)).strip()


def _highlights_text(row: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for block in row.get("job_highlights") or []:
        if isinstance(block, dict):
            parts.extend(str(item) for item in block.get("items") or [])
    return "\n".join(parts) if parts else None


def fetch_google_jobs(
    client: SearchApiClient,
    *,
    aliases: tuple[str, ...],
    locations: tuple[str, ...],
    family: str,
    max_per_query: int,
) -> tuple[list[DiscoveredJob], dict[str, int]]:
    """Fan out aliases × locations; normalize, filter and route each row.

    Consumes the SearchAPI.io google_jobs shape as live-probed 16 Jul 2026:
    root key ``jobs``; per-row ``apply_link``/``apply_links``/``sharing_link``;
    full but HTML-bearing ``description``; ``detected_extensions.posted_at``."""
    jobs: list[DiscoveredJob] = []
    skips = {"missing_title": 0, "missing_company": 0, "missing_url": 0,
             "family_filter": 0}

    skips["query_errors"] = 0
    succeeded = 0
    last_exc: Exception | None = None

    for alias in aliases:
        for location in locations:
            try:
                payload = client.search({
                    "engine": "google_jobs", "q": alias, "location": location,
                    "hl": "en",
                })
            except Exception as exc:  # noqa: BLE001 — per-query isolation:
                # one slow alias×location must not kill the healthy rest
                # (live lesson 16 Jul: provider latency windows); counted,
                # never silent (§15.12).
                skips["query_errors"] += 1
                last_exc = exc
                continue
            succeeded += 1
            accepted = 0
            for row in payload.get("jobs") or []:
                if accepted >= max_per_query:
                    break
                title = row.get("title")
                company = row.get("company_name")
                if not title:
                    skips["missing_title"] += 1
                    continue
                if not company:
                    skips["missing_company"] += 1
                    continue
                url = row.get("apply_link") or row.get("sharing_link")
                if not url:
                    skips["missing_url"] += 1
                    continue
                if not _passes_lenient_family_filter(str(title), family):
                    skips["family_filter"] += 1
                    continue
                routing = route_apply_url(row, str(url))
                effective = promote_canonical_apply_url(
                    str(url), routing["canonical_apply_url"]
                )
                raw_description = row.get("description") or _highlights_text(row)
                jobs.append(
                    DiscoveredJob(
                        title=str(title), company=str(company), url=effective,
                        source="searchapi_google_jobs", family=family,
                        location=row.get("location"),
                        description=(
                            _strip_html(str(raw_description))
                            if raw_description else None
                        ),
                        salary_raw=row.get("salary"),
                        source_native_id=row.get("job_id"),
                        posted_at_raw=(row.get("detected_extensions") or {}).get(
                            "posted_at"
                        ),
                        route=routing,
                    )
                )
                accepted += 1

    if succeeded == 0 and last_exc is not None:
        raise last_exc  # zero successful queries = the source is DOWN — honest
    return jobs, skips


class SearchApiError(RuntimeError):
    """Non-200 from SearchAPI.io — message carries the status only, never the
    provider body (which may quote query/account details)."""


_SEARCHAPI_ENDPOINT = "https://www.searchapi.io/api/v1/search"


def _urllib_opener(  # pragma: no cover — exercised live in C6's gate
    url: str, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


class HttpSearchApiClient:
    """GET https://www.searchapi.io/api/v1/search with Bearer auth (D13).

    Live-probed 16 Jul 2026: ``location`` accepts "Riyadh, Saudi Arabia" but
    rejects "Remote" with a 400 — the pseudo-location becomes a remote-flavored
    query instead, and every request is country-anchored with ``gl=sa``.
    Live lesson (same night): the provider has transient latency windows past
    15s — the timeout is generous and transport errors retry with a pause;
    non-200 statuses are permanent and never retried."""

    def __init__(
        self,
        api_key: str,
        timeout: float = 45.0,
        opener: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
        retries: int = 1,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._opener = opener or _urllib_opener
        self._retries = max(0, retries)
        self._sleeper = sleeper or time.sleep

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(params)
        query.setdefault("gl", "sa")
        if query.get("location") == "Remote":
            del query["location"]
            query["q"] = f"{query.get('q', '')} remote".strip()
        url = f"{_SEARCHAPI_ENDPOINT}?{urlencode(query)}"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        for attempt in range(self._retries + 1):
            try:
                status, body = self._opener(url, headers, self._timeout)
            except (TimeoutError, urllib.error.URLError, OSError):
                if attempt >= self._retries:
                    raise
                self._sleeper(2.0 * (attempt + 1))
                continue
            if status != 200:
                raise SearchApiError(f"searchapi returned HTTP {status}")
            result: dict[str, Any] = json.loads(body)
            return result
        raise SearchApiError("unreachable")  # pragma: no cover — loop exits above


# ── JobSpy (indeed) ──────────────────────────────────────────────────────────


class JobSpyClient(Protocol):
    def scrape(self, **kwargs: Any) -> list[dict[str, Any]]: ...


def fetch_jobspy(
    client: JobSpyClient,
    *,
    aliases: tuple[str, ...],
    family: str,
    max_results: int,
) -> tuple[list[DiscoveredJob], dict[str, int]]:
    """ONE call per family with aliases OR-joined (§5.2 digest path), then the
    conservative two-tier post-filter."""
    rows = client.scrape(
        search_term=" OR ".join(aliases),
        site_name=["indeed"],           # Glassdoor N/A for Saudi; Zip geoblocked
        results_wanted=max_results,
        hours_old=336,                  # 14 days
        country_indeed="Saudi Arabia",
    )
    jobs: list[DiscoveredJob] = []
    skips = {"missing_fields": 0, "post_filter": 0}
    for row in rows:
        title, company, url = row.get("title"), row.get("company"), row.get("job_url")
        if not title or not company or not url:
            skips["missing_fields"] += 1
            continue
        if not _passes_conservative_post_filter(str(title), family):
            skips["post_filter"] += 1
            continue
        jobs.append(
            DiscoveredJob(
                title=str(title), company=str(company), url=str(url),
                source="jobspy", family=family,
                location=row.get("location"),
                description=row.get("description"),
                salary_raw=row.get("salary") or row.get("compensation"),
                posted_at_raw=str(row.get("date_posted") or "") or None,
            )
        )
    return jobs, skips


class PythonJobSpyClient:
    """The real JobSpy client — optional dependency: if python-jobspy is not
    installed the adapter returns [] without breaking the app (§5.3)."""

    def __init__(self, importer: Callable[[str], Any] | None = None) -> None:
        self._importer = importer or __import__

    def scrape(self, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            jobspy = self._importer("jobspy")
        except ImportError:
            return []
        frame = jobspy.scrape_jobs(**kwargs)  # pragma: no cover — needs the dep
        records: list[dict[str, Any]] = frame.to_dict("records")  # pragma: no cover
        return records  # pragma: no cover
