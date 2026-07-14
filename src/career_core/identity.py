"""URL-v1 job identity — the single identity authority (LEGACY §3).

identity = joburl:v1:<64-lowercase-hex sha256(normalized_url)>

Pure function of the normalized URL: no randomness, no timestamps, no
title/company/id fallback, no network. Validators must recompute this
independently — a caller-attested identity never authorizes a match.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from career_core.urltools import normalize_job_url_v1

IDENTITY_VERSION = "joburl-v1"
CANONICAL_IDENTITY_PREFIX = "joburl:v1:"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def derive_canonical_job_identity(raw_url: object) -> str | None:
    normalized = normalize_job_url_v1(raw_url)
    if not normalized:
        return None
    try:
        parsed = urlparse(normalized)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{CANONICAL_IDENTITY_PREFIX}{digest}"


def parse_canonical_job_identity(identity: object) -> str | None:
    if not isinstance(identity, str):
        return None
    if not identity.startswith(CANONICAL_IDENTITY_PREFIX):
        return None
    hexpart = identity[len(CANONICAL_IDENTITY_PREFIX):]
    return hexpart if _HEX64_RE.match(hexpart) else None


def canonical_identity_safe_stem(identity: object) -> str | None:
    """Path-safe filename stem: hex-only, cannot contain '/', '\\', '..', query."""
    hexpart = parse_canonical_job_identity(identity)
    return f"joburl-{hexpart}" if hexpart else None
