"""SSRF guard predicates (LEGACY §6, MR-2) — fail-closed.

Pure checks: IP classification, host resolution policy (resolver injectable so
tests never touch DNS), URL public-ness, and the unsafe-domain blocklists.
The enrichment fetcher (C6) must apply these to BOTH the initial URL and the
final post-redirect URL — one policy governs the whole fetch.

DEVIATIONS D7: linkedin.com is excluded from the DEFAULT blocklist (allowed
without login, pending the C6 source-ToS audit); the full legacy list is kept
as LEGACY_JD_UNSAFE_DOMAINS for reference and tests.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlparse

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_AddrInfo = tuple[Any, ...]

LEGACY_JD_UNSAFE_DOMAINS = frozenset({
    "jooble.org", "bebee.com", "jobleads.com", "theirstack.com",
    "linkedin.com", "nationalpostdoc.org", "founditgulf.com",
})
# D7: LinkedIn allowed (no-login only) pending the C6 audit.
DEFAULT_JD_UNSAFE_DOMAINS = frozenset(LEGACY_JD_UNSAFE_DOMAINS - {"linkedin.com"})

Resolver = Callable[[str], Sequence[_AddrInfo]]


def ip_is_internal(ip: _IPAddress) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)  # unwrap ::ffff:127.0.0.1
    if mapped is not None:
        ip = mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _default_resolver(hostname: str) -> Sequence[_AddrInfo]:
    return socket.getaddrinfo(hostname, None)


def host_resolves_public_only(
    hostname: str | None, *, resolver: Resolver = _default_resolver
) -> bool:
    """True only if the host is a public IP literal, or a name whose EVERY
    resolved address is public. Any failure → False (fail closed)."""
    if not hostname:
        return False
    try:  # direct IP literal
        return not ip_is_internal(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    try:  # named host → resolve ALL addresses
        infos = resolver(hostname)
    except Exception:
        return False  # resolution failure → fail closed
    if not infos:
        return False
    for info in infos:
        addr = info[4][0] if info[4] else ""
        try:
            if ip_is_internal(ipaddress.ip_address(addr)):
                return False
        except ValueError:
            return False  # unparseable → fail closed
    return True  # only if EVERY address is public


def is_public_url(url: str, *, resolver: Resolver = _default_resolver) -> bool:
    """Valid http(s) URL whose host passes the public-only resolution policy."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return host_resolves_public_only(parsed.hostname, resolver=resolver)


def is_unsafe_domain(url: str, blocked: frozenset[str] = DEFAULT_JD_UNSAFE_DOMAINS) -> bool:
    """True if the URL's host is (a subdomain of) any blocked domain."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True  # unparseable → treat as unsafe (fail closed)
    if not host:
        return True
    return any(host == d or host.endswith("." + d) for d in blocked)
