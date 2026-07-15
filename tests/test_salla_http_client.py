"""HttpSallaClient against captured live API shapes (httpx.MockTransport).

The JSON mirrors real responses from api.salla.dev (demo store, 2026-07-15):
GET /orders/{id} returns items=null; items come from GET /orders/items.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx

from career.salla.client import HttpSallaClient, _map_status


def _order_body(slug: str, method: str = "mada") -> dict[str, object]:
    return {
        "status": 200,
        "success": True,
        "data": {
            "id": 1590390068,
            "reference_id": 272476561,
            "status": {"id": 1, "name": "x", "slug": slug},
            "payment_method": method,
            "currency": "SAR",
            "amounts": {"total": {"amount": 1, "currency": "SAR"}},
            "customer": {"id": 7, "mobile": "555000111", "mobile_code": "+966"},
            "items": None,
        },
    }


_ITEMS_BODY = {
    "status": 200,
    "success": True,
    "data": [{"id": 5, "name": "اشتراك تجريبي", "product": {"id": 786318419}}],
}


def _client(slug: str, method: str = "mada") -> HttpSallaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orders/items"):
            return httpx.Response(200, json=_ITEMS_BODY)
        return httpx.Response(200, json=_order_body(slug, method))

    return HttpSallaClient("k", transport=httpx.MockTransport(handler))


def test_online_under_review_is_paid_and_fields_mapped() -> None:
    order = _client("under_review", "mada").get_order("1590390068")
    assert order is not None
    assert order.status == "paid"
    assert order.product_id == "786318419"
    assert order.amount == Decimal("1")
    assert order.currency == "SAR"
    assert order.customer_phone == "555000111"


def test_bank_transfer_under_review_is_not_paid() -> None:
    order = _client("under_review", "bank").get_order("1590390068")
    assert order is not None
    assert order.status == "pending"  # manual method: receipt not yet verified


def test_404_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": 404, "success": False})

    client = HttpSallaClient("k", transport=httpx.MockTransport(handler))
    assert client.get_order("999") is None


def test_status_mapping_fail_closed() -> None:
    assert _map_status("in_progress", "bank") == "paid"  # merchant advanced it
    assert _map_status("completed", "mada") == "paid"
    assert _map_status("closed", "bank") == "paid"  # مكتمل — live slug 2026-07-15
    assert _map_status("payment_pending", "mada") == "pending"
    assert _map_status("canceled", "mada") == "canceled"
    assert _map_status("restored", "mada") == "refunded"
    assert _map_status("weird_new_slug", "mada") == "pending"  # unknown → pending
    assert json.dumps(_ITEMS_BODY)  # keep the captured shape used


def test_order_id_extracted_from_activity_wrapper() -> None:
    """order.status.updated wraps the order (live shape): data.id is the
    activity id; the order id must come from data.order.id."""
    from career.salla.webhook import extract_salla_order_id

    activity = {"event": "order.status.updated",
                "data": {"id": 3187825903901094400, "type": "activity",
                         "order": {"id": 1590390068, "reference_id": 272476561}}}
    assert extract_salla_order_id(activity) == "1590390068"
    plain = {"event": "order.created", "data": {"id": 42}}
    assert extract_salla_order_id(plain) == "42"
