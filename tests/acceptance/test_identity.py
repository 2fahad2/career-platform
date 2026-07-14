"""Acceptance tests — URL-v1 job identity (LEGACY §3, Verification Record).

The identity is a pure function of the normalized URL; same-run dedupe and the
delivered-suppression key deliberately reuse the SAME normalization so they can
never diverge (LEGACY §8.1, bug P4-DUP-2).
"""

from __future__ import annotations

import hashlib

from career_core.identity import (
    CANONICAL_IDENTITY_PREFIX,
    IDENTITY_VERSION,
    canonical_identity_safe_stem,
    derive_canonical_job_identity,
    parse_canonical_job_identity,
)
from career_core.urltools import (
    delivered_key,
    normalize_job_url_v1,
    strip_tracking_params,
)


class TestNormalization:
    def test_strips_utm_and_tracking_params(self) -> None:
        url = "https://example.com/job/123?utm_source=x&utm_medium=y&gclid=z&id=9"
        assert strip_tracking_params(url) == "https://example.com/job/123?id=9"

    def test_tracking_params_case_insensitive(self) -> None:
        url = "https://example.com/j?UTM_Source=a&Ref=b&keep=1"
        assert strip_tracking_params(url) == "https://example.com/j?keep=1"

    def test_google_jobs_apply_and_fbclid_stripped(self) -> None:
        url = "https://example.com/j?google_jobs_apply=1&fbclid=abc&utm_id=7"
        assert strip_tracking_params(url) == "https://example.com/j"

    def test_normalize_trims_lowercases_and_drops_trailing_slash(self) -> None:
        assert (
            normalize_job_url_v1("  HTTPS://Example.COM/Careers/Job/123/  ")
            == "https://example.com/careers/job/123"
        )

    def test_normalize_empty_and_none(self) -> None:
        assert normalize_job_url_v1("") == ""
        assert normalize_job_url_v1(None) == ""
        assert normalize_job_url_v1("   ") == ""

    def test_normalize_single_trailing_slash_only(self) -> None:
        # rstrip("/") removes trailing slashes; root URL keeps scheme+host
        assert normalize_job_url_v1("https://example.com/") == "https://example.com"

    def test_keeps_blank_query_values(self) -> None:
        url = "https://example.com/j?keep=&utm_source=x"
        assert strip_tracking_params(url) == "https://example.com/j?keep="


class TestIdentityDerivation:
    def test_identity_shape_and_hash_input(self) -> None:
        raw = "HTTPS://Example.com/Job/42/?utm_source=li"
        normalized = "https://example.com/job/42"
        expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        identity = derive_canonical_job_identity(raw)
        assert identity == f"joburl:v1:{expected}"
        assert identity is not None and len(identity.split(":")[-1]) == 64

    def test_identity_deterministic_across_tracking_variants(self) -> None:
        a = derive_canonical_job_identity("https://x.co/j/1?utm_source=a")
        b = derive_canonical_job_identity("https://x.co/j/1/?gclid=zzz")
        assert a == b is not None

    def test_rejects_non_http_schemes(self) -> None:
        assert derive_canonical_job_identity("ftp://example.com/job") is None
        assert derive_canonical_job_identity("javascript:alert(1)") is None
        assert derive_canonical_job_identity("file:///etc/passwd") is None

    def test_rejects_hostless_and_empty(self) -> None:
        assert derive_canonical_job_identity("") is None
        assert derive_canonical_job_identity(None) is None
        assert derive_canonical_job_identity("https:///path-only") is None
        assert derive_canonical_job_identity("not a url") is None

    def test_constants(self) -> None:
        assert IDENTITY_VERSION == "joburl-v1"
        assert CANONICAL_IDENTITY_PREFIX == "joburl:v1:"


class TestIdentityParsing:
    def test_parse_valid(self) -> None:
        hexpart = "a" * 64
        assert parse_canonical_job_identity(f"joburl:v1:{hexpart}") == hexpart

    def test_parse_rejects_wrong_prefix(self) -> None:
        assert parse_canonical_job_identity("jobid:v1:" + "a" * 64) is None

    def test_parse_rejects_bad_hex(self) -> None:
        assert parse_canonical_job_identity("joburl:v1:" + "z" * 64) is None
        assert parse_canonical_job_identity("joburl:v1:" + "a" * 63) is None
        assert parse_canonical_job_identity("joburl:v1:" + "A" * 64) is None  # lowercase only

    def test_parse_rejects_non_string(self) -> None:
        assert parse_canonical_job_identity(None) is None
        assert parse_canonical_job_identity(123) is None

    def test_safe_stem_is_path_safe(self) -> None:
        hexpart = "b" * 64
        stem = canonical_identity_safe_stem(f"joburl:v1:{hexpart}")
        assert stem == f"joburl-{hexpart}"
        assert "/" not in stem and "\\" not in stem and ".." not in stem

    def test_safe_stem_rejects_invalid(self) -> None:
        assert canonical_identity_safe_stem("garbage") is None


class TestDeliveredKeyReusesSameIdentity:
    """P4-DUP-2: cross-day suppression key MUST be byte-identical to same-run
    dedupe normalization — both are normalize_job_url_v1."""

    def test_delivered_key_prefers_apply_link(self) -> None:
        job = {"apply_link": "HTTPS://X.co/J/1/?utm_source=a", "url": "https://other.co/2"}
        assert delivered_key(job) == "https://x.co/j/1"

    def test_delivered_key_falls_back_to_url(self) -> None:
        job = {"url": "https://X.co/J/2/?gclid=g"}
        assert delivered_key(job) == "https://x.co/j/2"

    def test_delivered_key_none_when_no_link(self) -> None:
        assert delivered_key({}) is None
        assert delivered_key({"apply_link": "", "url": ""}) is None

    def test_delivered_key_equals_normalize(self) -> None:
        raw = "https://Example.com/Job/9/?utm_campaign=c"
        assert delivered_key({"url": raw}) == normalize_job_url_v1(raw)
