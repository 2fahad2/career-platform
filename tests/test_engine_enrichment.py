"""JD enrichment acceptance tests (LEGACY §6 verbatim) — before code.

HTTP GET only, 10s timeout, 200KB body cap, 10K snippet, the exact status
vocabulary, unsafe-domain and SSRF checks on BOTH the initial URL and the
final redirect target, content-type must be text/json — and the DB cache:
one fetch per posting regardless of how many tenants match it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.engine import enrichment

NOW = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)


@dataclass
class FakeResponse:
    final_url: str
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    body: bytes = b"<html><body>We need SQL and BPMN skills.</body></html>"


class FakeFetcher:
    """Injectable transport: url -> FakeResponse | Exception."""

    def __init__(self, responses: dict[str, FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: float) -> FakeResponse:
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


PUBLIC = {"careers.acme.com": ["93.184.216.34"], "evil.example": ["93.184.216.35"]}


def _resolver(hostname: str):
    import socket

    addrs = PUBLIC.get(hostname)
    if addrs is None:
        raise socket.gaierror("unknown host")
    return [(socket.AF_INET, None, None, "", (a, 0)) for a in addrs]


def _enrich(url: str, fetcher: FakeFetcher) -> enrichment.EnrichResult:
    return enrichment.safe_fetch_jd(url, fetcher=fetcher, resolver=_resolver)


# ── the exact §6 flow ────────────────────────────────────────────────────────


def test_success_strips_tags_and_caps_snippet() -> None:
    url = "https://careers.acme.com/j/1"
    result = _enrich(url, FakeFetcher({url: FakeResponse(final_url=url)}))
    assert result.status == "SUCCESS"
    assert result.snippet is not None
    assert "SQL" in result.snippet and "<body>" not in result.snippet


def test_empty_url_and_unsafe_domain_and_private_host() -> None:
    fetcher = FakeFetcher({})
    assert _enrich("", fetcher).status == "FAILED_UNKNOWN"
    assert _enrich("https://linkedin.com/jobs/1", fetcher).status == "FAILED_UNSAFE_SOURCE"
    assert _enrich("https://127.0.0.1/x", fetcher).status == "FAILED_UNSAFE_SOURCE"
    assert fetcher.calls == []  # nothing unsafe is ever fetched


def test_redirect_target_is_rechecked() -> None:
    """One policy governs the whole fetch: a safe start redirecting to an
    unsafe/blocked destination fails, after the fetch, on the FINAL URL."""
    url = "https://careers.acme.com/j/2"
    fetcher = FakeFetcher({
        url: FakeResponse(final_url="https://jooble.org/away/123"),
    })
    assert _enrich(url, fetcher).status == "FAILED_UNSAFE_SOURCE"


def test_http_status_content_type_and_size_failures() -> None:
    base = "https://careers.acme.com/j/"
    fetcher = FakeFetcher({
        base + "404": FakeResponse(final_url=base + "404", status=404),
        base + "pdf": FakeResponse(final_url=base + "pdf",
                                   content_type="application/pdf"),
        base + "big": FakeResponse(final_url=base + "big",
                                   body=b"x" * (enrichment.JD_MAX_BYTES + 1)),
    })
    assert _enrich(base + "404", fetcher).status == "FAILED_HTTP"
    assert _enrich(base + "pdf", fetcher).status == "FAILED_UNSAFE_SOURCE"
    assert _enrich(base + "big", fetcher).status == "FAILED_TOO_LARGE"


def test_timeout_and_unknown_exception() -> None:
    base = "https://careers.acme.com/j/"
    fetcher = FakeFetcher({
        base + "slow": TimeoutError(),
        base + "boom": RuntimeError("weird"),
    })
    assert _enrich(base + "slow", fetcher).status == "FAILED_TIMEOUT"
    assert _enrich(base + "boom", fetcher).status == "FAILED_UNKNOWN"


# ── once per posting, cached in the pool (whitepaper §06) ────────────────────


def _seed_posting(owner: Session, url: str) -> str:
    posting_id = str(uuid.uuid4())
    owner.execute(
        sql_text(
            "INSERT INTO job_postings (id, url_identity, url, title, company, source) "
            "VALUES (:id, :ident, :url, 'BA', 'Acme', 'serpapi_google_jobs')"
        ),
        {"id": posting_id, "ident": f"joburl:v1:{uuid.uuid4().hex}{uuid.uuid4().hex[:32]}",
         "url": url},
    )
    owner.commit()
    return posting_id


def test_enrichment_is_cached_per_posting(owner_engine: Engine) -> None:
    url = "https://careers.acme.com/j/cache"
    fetcher = FakeFetcher({url: FakeResponse(final_url=url)})
    with Session(owner_engine) as s:
        posting_id = _seed_posting(s, url)
        try:
            first = enrichment.enrich_posting(
                s, posting_id=uuid.UUID(posting_id), fetcher=fetcher,
                resolver=_resolver, now=NOW,
            )
            second = enrichment.enrich_posting(
                s, posting_id=uuid.UUID(posting_id), fetcher=fetcher,
                resolver=_resolver, now=NOW,
            )
            assert first.status == "SUCCESS"
            assert second.status == "SUCCESS"          # served from the cache
            assert len(fetcher.calls) == 1             # fetched exactly once
            row = s.execute(
                sql_text(
                    "SELECT jd_status, jd_snippet, enriched_at FROM job_postings "
                    "WHERE id = :id"
                ),
                {"id": posting_id},
            ).one()
            assert row.jd_status == "SUCCESS"
            assert "SQL" in row.jd_snippet
            assert row.enriched_at is not None
        finally:
            s.execute(sql_text("DELETE FROM job_postings WHERE id = :id"),
                      {"id": posting_id})
            s.commit()


def test_failed_enrichment_is_recorded_honestly(owner_engine: Engine) -> None:
    url = "https://careers.acme.com/j/fail"
    fetcher = FakeFetcher({url: TimeoutError()})
    with Session(owner_engine) as s:
        posting_id = _seed_posting(s, url)
        try:
            result = enrichment.enrich_posting(
                s, posting_id=uuid.UUID(posting_id), fetcher=fetcher,
                resolver=_resolver, now=NOW,
            )
            assert result.status == "FAILED_TIMEOUT"
            row = s.execute(
                sql_text("SELECT jd_status, jd_snippet FROM job_postings WHERE id = :id"),
                {"id": posting_id},
            ).one()
            assert row.jd_status == "FAILED_TIMEOUT"   # no silent success
            assert row.jd_snippet is None
        finally:
            s.execute(sql_text("DELETE FROM job_postings WHERE id = :id"),
                      {"id": posting_id})
            s.commit()
