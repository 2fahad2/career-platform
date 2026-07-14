"""Meta webhook signature + subscription-challenge verification (pure)."""

from __future__ import annotations

from career.whatsapp.signature import (
    compute_meta_signature,
    verify_challenge,
    verify_meta_signature,
)

SECRET = "wa_app_secret_123"
BODY = b'{"entry":[{"changes":[{"value":{"messages":[]}}]}]}'


def test_signature_roundtrip() -> None:
    sig = compute_meta_signature(BODY, SECRET)
    assert sig.startswith("sha256=")
    assert verify_meta_signature(BODY, sig, SECRET) is True


def test_signature_rejects_tamper_and_missing() -> None:
    sig = compute_meta_signature(BODY, SECRET)
    assert verify_meta_signature(BODY + b"x", sig, SECRET) is False
    assert verify_meta_signature(BODY, "sha256=deadbeef", SECRET) is False
    assert verify_meta_signature(BODY, None, SECRET) is False
    assert verify_meta_signature(BODY, "no-prefix", SECRET) is False
    assert verify_meta_signature(BODY, sig, "") is False


def test_challenge_echoed_on_match() -> None:
    assert verify_challenge(
        mode="subscribe", token="vt", challenge="12345", expected_token="vt"
    ) == "12345"


def test_challenge_rejected_on_mismatch() -> None:
    assert verify_challenge(
        mode="subscribe", token="wrong", challenge="1", expected_token="vt"
    ) is None
    assert verify_challenge(
        mode="unsub", token="vt", challenge="1", expected_token="vt"
    ) is None
    assert verify_challenge(
        mode="subscribe", token="", challenge="1", expected_token=""
    ) is None
