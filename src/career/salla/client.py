"""Salla API client — the boundary the provisioning worker re-verifies through.

The worker never trusts the webhook payload's amounts/status; it re-fetches the
order from Salla and acts on that (whitepaper §09). The client is an injectable
Protocol so tests use a fake with no network, and the HTTP implementation is
wired when the API key arrives (C3 manual step).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


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
    """HTTP implementation — structurally ready; wired when SALLA_API_KEY is set.

    Kept minimal on purpose: the tested provisioning logic depends only on the
    SallaClient protocol, so this can be fleshed out and integration-tested once
    real credentials exist without touching the worker.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.salla.dev/admin/v2") -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def get_order(self, order_id: str) -> SallaOrder | None:  # pragma: no cover
        raise NotImplementedError(
            "HttpSallaClient.get_order is wired in C3 once SALLA_API_KEY is provided"
        )
