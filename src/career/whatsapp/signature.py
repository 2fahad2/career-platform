"""Meta (WhatsApp Cloud API) webhook verification (whatsapp §08).

- POST bodies are signed: ``X-Hub-Signature-256: sha256=<hmac_hex>`` over the raw
  body with the app secret. Verified with a constant-time compare, fail-closed.
- GET subscription handshake: Meta calls with hub.mode/hub.verify_token/
  hub.challenge; we echo the challenge only if the verify token matches.
"""

from __future__ import annotations

import hashlib
import hmac

_PREFIX = "sha256="


def compute_meta_signature(raw_body: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"{_PREFIX}{digest}"


def verify_meta_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not app_secret or not header.startswith(_PREFIX):
        return False
    expected = compute_meta_signature(raw_body, app_secret)
    return hmac.compare_digest(expected, header.strip())


def verify_challenge(
    *, mode: str | None, token: str | None, challenge: str | None, expected_token: str
) -> str | None:
    """Return the challenge to echo if the subscription handshake is valid."""
    if mode == "subscribe" and expected_token and token == expected_token:
        return challenge
    return None
