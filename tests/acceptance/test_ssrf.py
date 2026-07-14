"""Acceptance tests — SSRF guard predicates (LEGACY §6, MR-2). Fail-closed.

Pure checks only (no network): named-host resolution is exercised through an
injected resolver. The real fetch flow lands in C6 and must call these on BOTH
the initial URL and the final post-redirect URL.
"""

from __future__ import annotations

import ipaddress

from career_core.ssrf import (
    DEFAULT_JD_UNSAFE_DOMAINS,
    LEGACY_JD_UNSAFE_DOMAINS,
    host_resolves_public_only,
    ip_is_internal,
    is_public_url,
    is_unsafe_domain,
)


class TestIpClassification:
    def test_internal_addresses(self) -> None:
        for ip in ("127.0.0.1", "10.0.0.1", "172.16.5.5", "192.168.1.1",
                   "169.254.169.254", "::1", "224.0.0.1", "0.0.0.0"):  # noqa: S104
            assert ip_is_internal(ipaddress.ip_address(ip)), ip

    def test_ipv4_mapped_ipv6_unwrapped(self) -> None:
        # ::ffff:127.0.0.1 must not slip through as a "public" IPv6.
        assert ip_is_internal(ipaddress.ip_address("::ffff:127.0.0.1"))
        assert ip_is_internal(ipaddress.ip_address("::ffff:10.0.0.1"))

    def test_public_addresses(self) -> None:
        for ip in ("8.8.8.8", "1.1.1.1", "142.250.180.14"):
            assert not ip_is_internal(ipaddress.ip_address(ip)), ip


class TestHostResolution:
    def test_ip_literal_checked_directly(self) -> None:
        assert host_resolves_public_only("8.8.8.8")
        assert not host_resolves_public_only("127.0.0.1")
        assert not host_resolves_public_only("169.254.169.254")

    def test_empty_host_fails_closed(self) -> None:
        assert not host_resolves_public_only("")
        assert not host_resolves_public_only(None)

    def test_named_host_all_addresses_public(self) -> None:
        resolver = lambda h: [("f", "t", "p", "c", ("93.184.216.34", 0))]  # noqa: E731
        assert host_resolves_public_only("example.com", resolver=resolver)

    def test_named_host_any_internal_rejects(self) -> None:
        # DNS answer mixing public and private → reject (rebinding defence).
        resolver = lambda h: [  # noqa: E731
            ("f", "t", "p", "c", ("93.184.216.34", 0)),
            ("f", "t", "p", "c", ("10.0.0.5", 0)),
        ]
        assert not host_resolves_public_only("evil.example", resolver=resolver)

    def test_resolution_failure_fails_closed(self) -> None:
        def resolver(_h):  # noqa: ANN001
            raise OSError("dns down")
        assert not host_resolves_public_only("nope.example", resolver=resolver)

    def test_empty_resolution_fails_closed(self) -> None:
        assert not host_resolves_public_only("empty.example", resolver=lambda h: [])

    def test_unparseable_address_fails_closed(self) -> None:
        resolver = lambda h: [("f", "t", "p", "c", ("not-an-ip", 0))]  # noqa: E731
        assert not host_resolves_public_only("weird.example", resolver=resolver)


class TestUrlGuard:
    def test_metadata_url_rejected(self) -> None:
        assert not is_public_url("http://169.254.169.254/latest/meta-data/")

    def test_loopback_rejected(self) -> None:
        assert not is_public_url("http://127.0.0.1:8080/x")

    def test_public_ip_url_accepted(self) -> None:
        assert is_public_url("https://8.8.8.8/job")

    def test_non_http_rejected(self) -> None:
        assert not is_public_url("ftp://8.8.8.8/file")
        assert not is_public_url("file:///etc/passwd")

    def test_hostless_rejected(self) -> None:
        assert not is_public_url("https:///nohost")
        assert not is_public_url("")


class TestUnsafeDomains:
    def test_legacy_blocklist_includes_linkedin(self) -> None:
        assert "linkedin.com" in LEGACY_JD_UNSAFE_DOMAINS
        assert is_unsafe_domain("https://www.linkedin.com/jobs/1", LEGACY_JD_UNSAFE_DOMAINS)

    def test_default_blocklist_excludes_linkedin_per_d7(self) -> None:
        # DEVIATIONS D7: LinkedIn allowed (no-login only) pending the C6 audit.
        assert "linkedin.com" not in DEFAULT_JD_UNSAFE_DOMAINS
        assert not is_unsafe_domain("https://www.linkedin.com/jobs/1", DEFAULT_JD_UNSAFE_DOMAINS)

    def test_aggregators_blocked_in_both(self) -> None:
        for dom in ("jooble.org", "bebee.com", "jobleads.com", "theirstack.com"):
            assert dom in DEFAULT_JD_UNSAFE_DOMAINS
            assert is_unsafe_domain(f"https://{dom}/x", DEFAULT_JD_UNSAFE_DOMAINS)

    def test_subdomain_matches(self) -> None:
        assert is_unsafe_domain("https://jobs.jooble.org/j/9", DEFAULT_JD_UNSAFE_DOMAINS)

    def test_safe_domain_passes(self) -> None:
        assert not is_unsafe_domain("https://careers.aramco.com/j/1", DEFAULT_JD_UNSAFE_DOMAINS)
