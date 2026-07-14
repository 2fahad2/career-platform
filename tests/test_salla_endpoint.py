"""POST /webhooks/salla endpoint (TestClient) — fast 200 / 401 behaviors.

Requires SALLA_WEBHOOK_SECRET in the environment (set to 'test_secret_123' by
the test run) so the app's configured secret matches the signatures here.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from career.main import app
from career.salla.signature import compute_signature

SECRET = "test_secret_123"

pytestmark = pytest.mark.skipif(
    os.environ.get("SALLA_WEBHOOK_SECRET") != SECRET,
    reason="SALLA_WEBHOOK_SECRET must equal the test secret for endpoint tests",
)


def _body(order_id: str, event: str = "order.payment.updated") -> bytes:
    return json.dumps({"event": event, "data": {"id": order_id}}).encode("utf-8")


def test_valid_webhook_returns_200_accepted(clean_billing: None) -> None:
    body = _body(f"ORD-{uuid.uuid4()}")
    sig = compute_signature(body, SECRET)
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/salla", content=body,
            headers={"X-Salla-Event": "order.payment.updated", "X-Salla-Signature": sig},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_bad_signature_returns_401(clean_billing: None) -> None:
    body = _body(f"ORD-{uuid.uuid4()}")
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/salla", content=body,
            headers={"X-Salla-Event": "order.payment.updated", "X-Salla-Signature": "forged"},
        )
    assert resp.status_code == 401
    assert resp.json()["status"] == "invalid_signature"


def test_duplicate_returns_200_duplicate(clean_billing: None) -> None:
    body = _body(f"ORD-{uuid.uuid4()}")
    sig = compute_signature(body, SECRET)
    headers = {"X-Salla-Event": "order.payment.updated", "X-Salla-Signature": sig}
    with TestClient(app) as client:
        first = client.post("/webhooks/salla", content=body, headers=headers)
        second = client.post("/webhooks/salla", content=body, headers=headers)
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"


def test_unknown_event_type_ignored(clean_billing: None) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/salla", content=b"{}",
            headers={"X-Salla-Event": "cart.updated", "X-Salla-Signature": "x"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
