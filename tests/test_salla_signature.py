"""Salla webhook signature verification (pure)."""

from __future__ import annotations

from career.salla.signature import compute_signature, verify_signature

SECRET = "test_secret_123"
BODY = b'{"data":{"id":"ORD-1"},"event":"order.payment.updated"}'


def test_roundtrip_valid() -> None:
    sig = compute_signature(BODY, SECRET)
    assert verify_signature(BODY, sig, SECRET) is True


def test_wrong_signature_rejected() -> None:
    assert verify_signature(BODY, "deadbeef", SECRET) is False


def test_tampered_body_rejected() -> None:
    sig = compute_signature(BODY, SECRET)
    assert verify_signature(BODY + b"x", sig, SECRET) is False


def test_wrong_secret_rejected() -> None:
    sig = compute_signature(BODY, SECRET)
    assert verify_signature(BODY, sig, "other_secret") is False


def test_missing_signature_or_secret_fails_closed() -> None:
    assert verify_signature(BODY, None, SECRET) is False
    assert verify_signature(BODY, "", SECRET) is False
    assert verify_signature(BODY, compute_signature(BODY, SECRET), "") is False
