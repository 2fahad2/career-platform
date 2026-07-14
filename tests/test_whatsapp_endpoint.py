"""WhatsApp endpoints (TestClient) — GET challenge + POST signature/dedupe.

Requires WHATSAPP_APP_SECRET / WHATSAPP_VERIFY_TOKEN in the environment to match
the values used here.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from career.main import app
from career.whatsapp.signature import compute_meta_signature

APP_SECRET = "wa_app_secret_123"
VERIFY_TOKEN = "wa_verify_123"

pytestmark = pytest.mark.skipif(
    os.environ.get("WHATSAPP_APP_SECRET") != APP_SECRET
    or os.environ.get("WHATSAPP_VERIFY_TOKEN") != VERIFY_TOKEN,
    reason="WHATSAPP_APP_SECRET/WHATSAPP_VERIFY_TOKEN must match the test values",
)


def _body(wamid: str) -> bytes:
    return (
        b'{"entry":[{"changes":[{"value":{"messaging_product":"whatsapp",'
        b'"messages":[{"id":"' + wamid.encode() + b'","from":"+966500000000",'
        b'"type":"text","text":{"body":"hi"}}]}}]}]}'
    )


def test_get_challenge_echoed_on_match() -> None:
    with TestClient(app) as client:
        resp = client.get("/webhooks/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "424242",
        })
    assert resp.status_code == 200
    assert resp.text == "424242"


def test_get_challenge_forbidden_on_mismatch() -> None:
    with TestClient(app) as client:
        resp = client.get("/webhooks/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1",
        })
    assert resp.status_code == 403


def test_post_valid_signature_accepted(clean_billing: None) -> None:
    body = _body(f"wamid-{uuid.uuid4()}")
    sig = compute_meta_signature(body, APP_SECRET)
    with TestClient(app) as client:
        resp = client.post("/webhooks/whatsapp", content=body,
                           headers={"X-Hub-Signature-256": sig})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_post_bad_signature_401(clean_billing: None) -> None:
    body = _body(f"wamid-{uuid.uuid4()}")
    with TestClient(app) as client:
        resp = client.post("/webhooks/whatsapp", content=body,
                           headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert resp.status_code == 401


def test_post_duplicate_deduped(clean_billing: None) -> None:
    body = _body(f"wamid-{uuid.uuid4()}")
    sig = compute_meta_signature(body, APP_SECRET)
    headers = {"X-Hub-Signature-256": sig}
    with TestClient(app) as client:
        first = client.post("/webhooks/whatsapp", content=body, headers=headers)
        second = client.post("/webhooks/whatsapp", content=body, headers=headers)
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
