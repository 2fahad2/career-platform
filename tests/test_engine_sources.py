"""Source-adapter acceptance tests (LEGACY §5.2/5.3/5.4 verbatim) — before code.

The apply-routing layer turns aggregator landings into canonical employer/ATS
links (score ats > employer > unknown > aggregator), never returns None and
never blocks a row; promotion to the effective URL fails closed. The
google_jobs adapter (SearchAPI.io per D13; response shape live-probed 16 Jul
2026 — root key ``jobs``, HTML-bearing descriptions) enforces row acceptance
(title+company), URL preference apply>sharing, tag-stripped descriptions with
highlights fallback, and the LENIENT family title filter; skip reasons are
counted, never silent. The HTTP client anchors gl=sa, translates the "Remote"
pseudo-location (rejected by the provider) into a remote-flavored query, and
fails loudly on non-200 with a body-free message. The JobSpy adapter uses
indeed-only, 14-day window, OR-joined alias expansion and the CONSERVATIVE
two-tier post-filter; a missing optional dependency yields [] without
breaking anything.
"""

from __future__ import annotations

from typing import Any

from career.engine import sources

# ── §5.4 routing: classification, selection, fail-closed promotion ───────────


def test_route_classification_priorities() -> None:
    assert sources.classify_route("https://boards.greenhouse.io/acme/jobs/1")[0] == "ats"
    assert sources.classify_route("https://careers.acme.com/jobs/1")[0] == "employer"
    assert sources.classify_route("https://www.linkedin.com/jobs/view/1")[0] == "aggregator"
    # live-observed 16 Jul 2026: bebee dominates Saudi google_jobs results
    assert sources.classify_route("https://bebee.com/sa/jobs/x")[0] == "aggregator"
    assert sources.classify_route("https://acme.example/positions/1")[0] == "unknown"
    # confidence is high ONLY for ats/employer
    assert sources.classify_route("https://jobs.acme.com/x")[1] == "high"
    assert sources.classify_route("https://something.example/x")[1] != "high"


def test_routing_picks_the_best_apply_option_and_never_none() -> None:
    item = {
        "apply_options": [
            {"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/9"},
            {"title": "Employer", "link": "https://careers.acme.com/jobs/9"},
            {"title": "ATS", "link": "https://acme.wd3.myworkdayjobs.com/en/job/9"},
            {"title": "junk", "link": "javascript:void(0)"},
        ]
    }
    routing = sources.route_apply_url(item, "https://www.bayt.com/en/job/9/")
    assert routing["route_type"] == "ats"
    assert routing["canonical_apply_url"] == "https://acme.wd3.myworkdayjobs.com/en/job/9"
    assert routing["original_apply_url"] == "https://www.bayt.com/en/job/9/"
    assert routing["route_confidence"] == "high"

    # no options at all → still a verdict, never None, row never blocked
    bare = sources.route_apply_url({}, "https://www.bayt.com/en/job/9/")
    assert bare["route_type"] == "aggregator"
    assert bare["canonical_apply_url"] == "https://www.bayt.com/en/job/9/"


def test_promotion_fails_closed() -> None:
    keep = "https://www.bayt.com/en/job/9/"
    assert sources.promote_canonical_apply_url(keep, None) == keep
    assert sources.promote_canonical_apply_url(keep, "") == keep
    assert sources.promote_canonical_apply_url(keep, "ftp://x/y") == keep
    assert sources.promote_canonical_apply_url(keep, "https://") == keep
    assert sources.promote_canonical_apply_url(keep, 42) == keep  # type: ignore[arg-type]
    promoted = sources.promote_canonical_apply_url(keep, "https://careers.acme.com/j/9")
    assert promoted == "https://careers.acme.com/j/9"
    # a usable URL is never replaced by an empty value — and never raises
    assert sources.promote_canonical_apply_url("", "https://careers.acme.com/j") \
        == "https://careers.acme.com/j"


# ── google_jobs adapter ──────────────────────────────────────────────────────


class FakeSearchApi:
    def __init__(self, pages: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        return {"jobs": self.pages.get((params["q"], params["location"]), [])}


def _row(title: str, company: str | None = "Acme", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"title": title, "company_name": company}
    row.update(kw)
    return row


def test_google_jobs_row_acceptance_and_url_preference() -> None:
    fake = FakeSearchApi({
        ("Business Analyst", "Saudi Arabia"): [
            _row("Business Analyst", None),                       # no company → skip
            _row("Business Analyst", "Acme",
                 apply_link="https://careers.acme.com/j/1",
                 sharing_link="https://share.example/1"),
            _row("Requirements Analyst", "Beta",
                 sharing_link="https://share.example/2"),         # falls to sharing
            _row("BA", "Gamma"),                                  # no URL at all → skip
        ],
    })
    jobs, skips = sources.fetch_google_jobs(
        fake, aliases=("Business Analyst",), locations=("Saudi Arabia",),
        family="business_analyst", max_per_query=5,
    )
    assert [j.url for j in jobs] == [
        "https://careers.acme.com/j/1", "https://share.example/2",
    ]
    assert skips["missing_company"] == 1
    assert skips["missing_url"] == 1


def test_google_jobs_description_falls_back_to_highlights() -> None:
    fake = FakeSearchApi({
        ("Business Analyst", "Saudi Arabia"): [
            _row("Business Analyst", "Acme",
                 apply_link="https://careers.acme.com/j/2",
                 job_highlights=[{"title": "Qualifications",
                                  "items": ["SQL", "5 years BA experience"]}]),
        ],
    })
    jobs, _ = sources.fetch_google_jobs(
        fake, aliases=("Business Analyst",), locations=("Saudi Arabia",),
        family="business_analyst", max_per_query=5,
    )
    assert "SQL" in (jobs[0].description or "")


def test_google_jobs_lenient_family_filter() -> None:
    """Positive signal → accept; else negative pattern → reject; NEITHER →
    accept (lenient — §5.2)."""
    fake = FakeSearchApi({
        ("Business Analyst", "Saudi Arabia"): [
            _row("Senior Business Analyst", "A", apply_link="https://c.a/1"),  # positive
            _row("Sales Manager", "B", apply_link="https://c.b/2"),            # negative
            _row("Transformation Consultant", "C", apply_link="https://c.c/3"),  # neither
        ],
    })
    jobs, skips = sources.fetch_google_jobs(
        fake, aliases=("Business Analyst",), locations=("Saudi Arabia",),
        family="business_analyst", max_per_query=5,
    )
    assert [j.title for j in jobs] == [
        "Senior Business Analyst", "Transformation Consultant",
    ]
    assert skips["family_filter"] == 1


def test_google_jobs_fanout_and_cap() -> None:
    rows = [
        _row(f"Business Analyst {i}", "Acme", apply_link=f"https://c.a/{i}")
        for i in range(10)
    ]
    fake = FakeSearchApi({
        ("Business Analyst", "Riyadh, Saudi Arabia"): rows,
        ("Business Analyst", "Saudi Arabia"): rows,
        ("محلل أعمال", "Riyadh, Saudi Arabia"): [],
        ("محلل أعمال", "Saudi Arabia"): [],
    })
    jobs, _ = sources.fetch_google_jobs(
        fake, aliases=("Business Analyst", "محلل أعمال"),
        locations=("Riyadh, Saudi Arabia", "Saudi Arabia"),
        family="business_analyst", max_per_query=3,
    )
    assert len(fake.calls) == 4                     # aliases × locations
    assert len(jobs) == 6                           # ≤ max_per_query per call
    assert all(j.source == "searchapi_google_jobs" for j in jobs)


def test_google_jobs_routing_promotes_the_effective_url() -> None:
    fake = FakeSearchApi({
        ("Business Analyst", "Saudi Arabia"): [
            _row("Business Analyst", "Acme",
                 apply_link="https://www.bayt.com/en/job/5/",
                 apply_options=[{"link": "https://boards.greenhouse.io/acme/5"}]),
        ],
    })
    jobs, _ = sources.fetch_google_jobs(
        fake, aliases=("Business Analyst",), locations=("Saudi Arabia",),
        family="business_analyst", max_per_query=5,
    )
    job = jobs[0]
    assert job.url == "https://boards.greenhouse.io/acme/5"      # promoted
    assert job.route["route_type"] == "ats"
    assert job.route["original_apply_url"] == "https://www.bayt.com/en/job/5/"


def test_google_jobs_descriptions_are_tag_stripped() -> None:
    """Live-probed: SearchAPI descriptions carry HTML (<p>, <h4>…) — the gate
    scores on text, so tags must not glue tokens together."""
    fake = FakeSearchApi({
        ("Business Analyst", "Saudi Arabia"): [
            _row("Business Analyst", "Acme",
                 apply_link="https://careers.acme.com/j/7",
                 description="<p>Requirements gathering.</p><h4>Key:</h4>SQL"),
        ],
    })
    jobs, _ = sources.fetch_google_jobs(
        fake, aliases=("Business Analyst",), locations=("Saudi Arabia",),
        family="business_analyst", max_per_query=5,
    )
    assert jobs[0].description == "Requirements gathering. Key: SQL"


# ── the HTTP client (SearchAPI.io, D13) ──────────────────────────────────────


class FakeOpener:
    def __init__(self, status: int = 200, body: bytes = b'{"jobs": []}') -> None:
        self.status = status
        self.body = body
        self.requests: list[tuple[str, dict[str, str], float]] = []

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.requests.append((url, headers, timeout))
        return self.status, self.body


def test_http_client_builds_an_authorized_country_anchored_request() -> None:
    opener = FakeOpener(body=b'{"jobs": [{"title": "BA"}]}')
    client = sources.HttpSearchApiClient("key-123", opener=opener)
    payload = client.search({
        "engine": "google_jobs", "q": "Business Analyst",
        "location": "Riyadh, Saudi Arabia", "hl": "en",
    })
    assert payload == {"jobs": [{"title": "BA"}]}
    url, headers, timeout = opener.requests[0]
    assert url.startswith("https://www.searchapi.io/api/v1/search?")
    assert "engine=google_jobs" in url
    assert "location=Riyadh%2C+Saudi+Arabia" in url
    assert "gl=sa" in url                              # country anchor, always
    assert headers["Authorization"] == "Bearer key-123"
    assert timeout > 0


def test_http_client_translates_the_remote_pseudo_location() -> None:
    """Live-probed twice: the provider 400s on location="Remote", and an
    unanchored remote query returns US on-site jobs that waste the retrieval
    cap — so it becomes a remote-flavored query anchored to Saudi Arabia."""
    opener = FakeOpener()
    sources.HttpSearchApiClient("k", opener=opener).search({
        "engine": "google_jobs", "q": "Business Analyst", "location": "Remote",
    })
    url, _, _ = opener.requests[0]
    assert "location=Saudi+Arabia" in url
    assert "q=Business+Analyst+remote" in url
    assert "gl=sa" in url


def test_http_client_fails_loudly_and_body_free_on_non_200() -> None:
    opener = FakeOpener(status=402, body=b'{"error": "quota details..."}')
    client = sources.HttpSearchApiClient("k", opener=opener)
    try:
        client.search({"engine": "google_jobs", "q": "BA"})
        raise AssertionError("expected SearchApiError")
    except sources.SearchApiError as exc:
        assert "402" in str(exc)
        assert "quota" not in str(exc)                 # provider body never quoted


def test_http_client_retries_transient_transport_errors() -> None:
    """Live lesson (16 Jul): the provider has latency windows past any sane
    timeout — one retry with a pause rides them out."""
    calls: list[str] = []

    def flaky(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return 200, b'{"jobs": []}'

    naps: list[float] = []
    client = sources.HttpSearchApiClient(
        "k", opener=flaky, sleeper=naps.append
    )
    assert client.search({"engine": "google_jobs", "q": "BA"}) == {"jobs": []}
    assert len(calls) == 2
    assert naps                                     # backed off between tries


def test_http_client_gives_up_after_retries_with_the_real_error() -> None:
    def dead(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        raise TimeoutError("read timed out")

    client = sources.HttpSearchApiClient(
        "k", opener=dead, retries=2, sleeper=lambda s: None
    )
    try:
        client.search({"engine": "google_jobs", "q": "BA"})
        raise AssertionError("expected TimeoutError")
    except TimeoutError:
        pass


def test_http_client_does_not_retry_permanent_statuses() -> None:
    opener = FakeOpener(status=401, body=b"{}")
    client = sources.HttpSearchApiClient("k", opener=opener, sleeper=lambda s: None)
    try:
        client.search({"engine": "google_jobs", "q": "BA"})
        raise AssertionError("expected SearchApiError")
    except sources.SearchApiError:
        pass
    assert len(opener.requests) == 1                # 4xx is not transient


def test_google_jobs_isolates_failing_queries_but_raises_when_all_fail() -> None:
    """One flaky alias×location must not kill the healthy nine (§15.12 —
    counted, never silent); a source with ZERO successful queries is DOWN and
    must raise so the runner records an honest error status."""

    class HalfDead:
        def search(self, params: dict[str, Any]) -> dict[str, Any]:
            if params["location"] == "Saudi Arabia":
                raise TimeoutError("slow window")
            return {"jobs": [
                {"title": "Business Analyst", "company_name": "Acme",
                 "apply_link": "https://careers.acme.com/j/1"},
            ]}

    jobs, skips = sources.fetch_google_jobs(
        HalfDead(), aliases=("Business Analyst",),
        locations=("Riyadh, Saudi Arabia", "Saudi Arabia"),
        family="business_analyst", max_per_query=5,
    )
    assert len(jobs) == 1                           # the healthy query survived
    assert skips["query_errors"] == 1               # the sick one is counted

    class AllDead:
        def search(self, params: dict[str, Any]) -> dict[str, Any]:
            raise TimeoutError("provider down")

    try:
        sources.fetch_google_jobs(
            AllDead(), aliases=("Business Analyst",),
            locations=("Riyadh, Saudi Arabia",),
            family="business_analyst", max_per_query=5,
        )
        raise AssertionError("expected TimeoutError")
    except TimeoutError:
        pass


def test_http_client_propagates_malformed_json() -> None:
    import json

    opener = FakeOpener(body=b"<html>upstream proxy error</html>")
    client = sources.HttpSearchApiClient("k", opener=opener)
    try:
        client.search({"engine": "google_jobs", "q": "BA"})
        raise AssertionError("expected JSONDecodeError")
    except json.JSONDecodeError:
        pass                                           # run.py sanitizes to class name


# ── JobSpy adapter ───────────────────────────────────────────────────────────


class FakeJobSpy:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def scrape(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.rows


def test_jobspy_settings_and_or_joined_expansion() -> None:
    fake = FakeJobSpy([])
    sources.fetch_jobspy(
        fake, aliases=("Business Analyst", "Business Systems Analyst"),
        family="business_analyst", max_results=5,
    )
    call = fake.calls[0]
    assert call["site_name"] == ["indeed"]           # Glassdoor N/A, Zip geoblocked
    assert call["hours_old"] == 336                  # 14 days
    assert call["country_indeed"] == "Saudi Arabia"
    assert call["search_term"] == "Business Analyst OR Business Systems Analyst"


def test_jobspy_two_tier_conservative_post_filter() -> None:
    rows = [
        {"title": "Business Analyst", "company": "A", "job_url": "https://a/1"},
        {"title": "Financial Analyst", "company": "B", "job_url": "https://b/2"},  # reject
        {"title": "Operations Coordinator", "company": "C", "job_url": "https://c/3"},
        # rule exists but neither require nor reject matched → conservative skip
    ]
    jobs, skips = sources.fetch_jobspy(
        FakeJobSpy(rows), aliases=("Business Analyst",),
        family="business_analyst", max_results=10,
    )
    assert [j.title for j in jobs] == ["Business Analyst"]
    assert skips["post_filter"] == 2
    assert jobs[0].source == "jobspy"


def test_jobspy_nan_fields_never_reach_the_pool() -> None:
    """Live lesson (16 Jul): jobspy rows come from a pandas frame — missing
    values are float NaN, which is TRUTHY, so 'nan' companies would pass the
    required-fields check and pollute the pool."""
    nan = float("nan")
    fake = FakeJobSpy([
        {"title": "Business Analyst", "company": nan,
         "job_url": "https://sa.indeed.com/j/1"},                # NaN company
        {"title": nan, "company": "Acme", "job_url": "https://sa.indeed.com/j/2"},
        {"title": "Senior Business Analyst", "company": "Acme",
         "job_url": "https://sa.indeed.com/j/3",
         "location": nan, "description": nan, "salary": nan},    # NaN optionals
    ])
    jobs, skips = sources.fetch_jobspy(
        fake, aliases=("Business Analyst",), family="business_analyst",
        max_results=10,
    )
    assert skips["missing_fields"] == 2
    assert len(jobs) == 1
    job = jobs[0]
    assert job.location is None and job.description is None    # never "nan"
    assert job.salary_raw is None


def test_jobspy_missing_dependency_returns_empty() -> None:
    """The real client degrades to [] when python-jobspy is not installed —
    the optional dependency never breaks the app (LEGACY §5.3)."""
    client = sources.PythonJobSpyClient(importer=lambda name: (_ for _ in ()).throw(ImportError))
    assert client.scrape(search_term="x", site_name=["indeed"], results_wanted=5,
                         hours_old=336, country_indeed="Saudi Arabia") == []
