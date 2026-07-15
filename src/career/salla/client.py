"""Salla API client — the boundary the provisioning worker re-verifies through.

The worker never trusts the webhook payload's amounts/status; it re-fetches the
order from Salla and acts on that (whitepaper §09). The client is an injectable
Protocol so tests use a fake with no network; HttpSallaClient talks to the
Salla Admin API (wired live 2026-07-15 against a real demo-store order).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import httpx

# --- Honest paid-mapping (§15.4: provision only on confirmed payment) --------
#
# Salla has no explicit is_paid flag on the order; payment is implied by the
# status slug *and* the payment method (verified live 2026-07-15):
#   - Online gateways (mada/credit card/…): a successful charge moves the order
#     out of payment_pending — under_review and later slugs mean money captured.
#   - Manual methods (bank transfer, COD): under_review only means "receipt
#     uploaded, merchant must verify" — NOT paid. They count as paid only once
#     the merchant advances the order (in_progress and beyond). COD is disabled
#     for our products anyway (whitepaper §09).
# Anything unrecognized maps to "pending" — fail closed, never provision.
#   - "closed" is Salla's slug for the standard "مكتمل" end state (verified
#     live 2026-07-15: marking the order completed produced slug=closed).
_PAID_ANY_METHOD = frozenset(
    {"in_progress", "completed", "closed", "delivering", "delivered", "shipped"}
)
_PAID_UNLESS_MANUAL = frozenset({"under_review"})
_MANUAL_METHODS = frozenset({"bank", "cod", "cash", "waiting"})
_CANCELED = frozenset({"canceled", "cancelled"})
_REFUNDED = frozenset({"restored", "restoring"})


def _map_status(slug: str, payment_method: str) -> str:
    if slug in _PAID_ANY_METHOD:
        return "paid"
    if slug in _PAID_UNLESS_MANUAL and payment_method not in _MANUAL_METHODS:
        return "paid"
    if slug in _CANCELED:
        return "canceled"
    if slug in _REFUNDED:
        return "refunded"
    return "pending"


@dataclass(frozen=True)
class SallaOrder:
    order_id: str
    status: str  # e.g. "paid", "pending", "canceled", "refunded"
    product_id: str
    amount: Decimal
    currency: str
    customer_phone: str | None = None
    raw: dict[str, object] = field(default_factory=dict)


class SallaClient(Protocol):
    def get_order(self, order_id: str) -> SallaOrder | None: ...


class FakeSallaClient:
    """In-memory client for tests. Seed orders, then the worker reads them as if
    from Salla."""

    def __init__(self, orders: dict[str, SallaOrder] | None = None) -> None:
        self._orders = dict(orders or {})

    def set_order(self, order: SallaOrder) -> None:
        self._orders[order.order_id] = order

    def get_order(self, order_id: str) -> SallaOrder | None:
        return self._orders.get(order_id)


class HttpSallaClient:
    """Salla Admin API implementation.

    Two calls per order (the detail endpoint returns items=null; items live on
    their own endpoint): GET /orders/{id} then GET /orders/items?order_id={id}.
    A custom transport is injectable so tests exercise the exact JSON shapes
    captured from the live API without any network.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.salla.dev/admin/v2",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def get_order(self, order_id: str) -> SallaOrder | None:
        resp = self._client.get(f"/orders/{order_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json().get("data") or {}

        status_slug = str((data.get("status") or {}).get("slug") or "")
        payment_method = str(data.get("payment_method") or "")
        total = (data.get("amounts") or {}).get("total") or {}
        amount = Decimal(str(total.get("amount") or "0"))
        currency = str(total.get("currency") or data.get("currency") or "SAR")
        phone = (data.get("customer") or {}).get("mobile")

        items_resp = self._client.get("/orders/items", params={"order_id": order_id})
        items_resp.raise_for_status()
        items = items_resp.json().get("data") or []
        first = items[0] if items else {}
        product = first.get("product") or {}
        product_id = str(product.get("id") or first.get("product_id") or "")

        return SallaOrder(
            order_id=str(data.get("id") or order_id),
            status=_map_status(status_slug, payment_method),
            product_id=product_id,
            amount=amount,
            currency=currency,
            customer_phone=str(phone) if phone else None,
            raw={"status_slug": status_slug, "payment_method": payment_method},
        )
