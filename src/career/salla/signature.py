"""Salla webhook signature verification (whitepaper §09).

Salla signs the raw request body with the store's webhook secret using
HMAC-SHA256 (hex). We verify with a constant-time comparison. The secret is
injected (never hardcoded, never logged — §15.13); the exact header value is
confirmed against Salla when the app is created (C3 manual step).
"""

from __future__ import annotations

import hashlib
import hmac


def compute_signature(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, header_signature: str | None, secret: str) -> bool:
    """Constant-time check that ``header_signature`` matches HMAC-SHA256(body).
    Missing signature or empty secret → False (fail closed)."""
    if not header_signature or not secret:
        return False
    expected = compute_signature(raw_body, secret)
    return hmac.compare_digest(expected, header_signature.strip())
