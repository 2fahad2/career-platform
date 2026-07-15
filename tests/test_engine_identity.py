"""Extended identity + conservative merge tests (whitepaper §06) — before code.

URL-v1 stays the base identity; the extended ids exist because one job appears
under many URLs. The merge policy is CONSERVATIVE: merge on canonical-URL
identity match, or on the high-threshold quadruple (company + title + city +
posting window); anything doubtful stays separate — wrongly merging two jobs
is worse than a duplicate. Same-run dedupe follows §8.1: first the normalized
URL, then the (company, title) pair, order preserved.
"""

from __future__ import annotations

from career.engine import identity
from career.engine.sources import DiscoveredJob


def _job(title: str = "Business Analyst", company: str = "Acme",
         url: str = "https://careers.acme.com/j/1", **kw: object) -> DiscoveredJob:
    defaults: dict[str, object] = {
        "title": title, "company": company, "url": url,
        "source": "searchapi_google_jobs", "family": "business_analyst",
    }
    defaults.update(kw)
    return DiscoveredJob(**defaults)  # type: ignore[arg-type]


# ── identity derivation ──────────────────────────────────────────────────────


def test_url_identity_is_the_career_core_authority() -> None:
    from career_core.identity import derive_canonical_job_identity

    job = _job(url="https://careers.acme.com/j/1?utm_source=x")
    ids = identity.derive_ids(job)
    assert ids.url_identity == derive_canonical_job_identity(job.url)
    assert ids.url_identity is not None
    assert ids.url_identity.startswith("joburl:v1:")


def test_tracking_params_do_not_change_identity() -> None:
    a = identity.derive_ids(_job(url="https://careers.acme.com/j/1"))
    b = identity.derive_ids(_job(url="https://careers.acme.com/j/1?utm_campaign=z"))
    assert a.url_identity == b.url_identity


def test_cross_source_fingerprint_normalizes_company_title_city() -> None:
    a = identity.derive_ids(_job(location="Riyadh, Saudi Arabia"))
    b = identity.derive_ids(
        _job(url="https://www.bayt.com/en/job/999/",
             title="  business analyst ", company="ACME",
             location="Riyadh", source="jobspy")
    )
    assert a.cross_source_fingerprint == b.cross_source_fingerprint  # same job
    c = identity.derive_ids(_job(title="Data Analyst"))
    assert c.cross_source_fingerprint != a.cross_source_fingerprint


def test_unusable_url_yields_no_identity() -> None:
    ids = identity.derive_ids(_job(url="not-a-url"))
    assert ids.url_identity is None  # honest — the pool insert will skip it


# ── conservative merge ───────────────────────────────────────────────────────


def test_same_canonical_identity_merges() -> None:
    a = _job(url="https://careers.acme.com/j/1?utm_source=google")
    b = _job(url="https://careers.acme.com/j/1", source="jobspy")
    assert identity.should_merge(
        identity.derive_ids(a), a, identity.derive_ids(b), b
    )


def test_quadruple_match_merges_across_sources() -> None:
    a = _job(url="https://careers.acme.com/j/1", location="Riyadh, Saudi Arabia")
    b = _job(url="https://www.bayt.com/en/job/42/", location="Riyadh",
             source="jobspy")
    assert identity.should_merge(
        identity.derive_ids(a), a, identity.derive_ids(b), b
    )


def test_doubt_stays_separate() -> None:
    a = _job(url="https://careers.acme.com/j/1", location="Riyadh")
    # same company, DIFFERENT title → separate
    b = _job(url="https://careers.acme.com/j/2", title="Senior Business Analyst",
             location="Riyadh")
    assert not identity.should_merge(
        identity.derive_ids(a), a, identity.derive_ids(b), b
    )
    # same title, different city → separate
    c = _job(url="https://careers.acme.com/j/3", location="Jeddah")
    assert not identity.should_merge(
        identity.derive_ids(a), a, identity.derive_ids(c), c
    )
    # missing location on one side → doubt → separate
    d = _job(url="https://careers.acme.com/j/4", location=None)
    assert not identity.should_merge(
        identity.derive_ids(a), a, identity.derive_ids(d), d
    )


# ── same-run dedupe (§8.1 semantics) ─────────────────────────────────────────


def test_same_run_dedupe_url_first_then_company_title() -> None:
    jobs = [
        _job(url="https://careers.acme.com/j/1"),
        _job(url="https://careers.acme.com/j/1?utm_source=x"),      # dup by URL
        _job(url="https://OTHER.example/xyz", title="Business Analyst",
             company="acme "),                                       # dup by pair
        _job(url="https://beta.example/j/9", title="PMO Lead", company="Beta"),
    ]
    deduped, removed = identity.dedupe_same_run(jobs)
    assert [j.url for j in deduped] == [
        "https://careers.acme.com/j/1", "https://beta.example/j/9",
    ]  # order preserved, first wins
    assert removed == 2


def test_repost_group_defaults_to_url_identity() -> None:
    job = _job()
    ids = identity.derive_ids(job)
    assert ids.repost_group_id == ids.url_identity
