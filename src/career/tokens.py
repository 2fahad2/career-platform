"""Activation-token authority — one place defines how tokens are minted and
hashed, so the value stored at provisioning (Salla, C3) and the value verified
at activation (WhatsApp, C4) can never disagree (single-authority).

The raw token is shown to the customer exactly once (in the WhatsApp deep link);
only its SHA-256 hash is ever stored.
"""

from __future__ import annotations

import hashlib
import secrets

_TOKEN_BYTES = 32


def new_activation_token() -> str:
    """A fresh URL-safe token (~43 chars of [A-Za-z0-9_-])."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
