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


class SallaApiError(Exception):
    """A call to Salla did not answer usefully.

    ``retryable`` is the load-bearing field: it separates «this order is bad»
    from «we could not ask». Everything that reached the worker before was one
    undifferentiated Exception, so a dead access token — a credential problem,
    fixed in ninety seconds by a human — looked exactly like a poisoned
    payload and got the same treatment: the paid order's webhook marked
    ``failed`` forever, no tenant, no subscription, no activation, and an
    alert pointing the operator at the payload instead of the credential.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class SallaAuthError(SallaApiError):
    """401/403 — the token is rejected. Retryable BY DEFINITION: the order is
    perfectly valid and must wait, untouched, until the credential is
    renewed."""

    def __init__(self, message: str = "salla rejected our credentials") -> None:
        super().__init__(message, retryable=True)


class SallaUnavailable(SallaApiError):
    """Rate limit, 5xx, timeout, connection failure — Salla's side or the
    network. The order is fine; ask again later."""

    def __init__(self, message: str = "salla unavailable") -> None:
        super().__init__(message, retryable=True)


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

    def _get(self, url: str, **kwargs: object) -> httpx.Response:
        """One place where an HTTP outcome becomes a typed failure.

        404 stays soft (handled by the caller as «no such order»). Everything
        else that is not our fault — credentials, throttling, Salla's own 5xx,
        a timeout, a dropped connection — raises a RETRYABLE error, because
        the money already changed hands and the order must survive to be
        provisioned once we can ask again.
        """
        try:
            resp = self._client.get(url, **kwargs)  # type: ignore[arg-type]
        except httpx.TimeoutException as exc:
            raise SallaUnavailable(f"timeout calling salla: {url}") from exc
        except httpx.TransportError as exc:
            raise SallaUnavailable(f"transport error calling salla: {url}") from exc
        if resp.status_code in (401, 403):
            raise SallaAuthError(
                f"salla rejected our credentials ({resp.status_code})"
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise SallaUnavailable(f"salla returned {resp.status_code}")
        return resp

    def get_order(self, order_id: str) -> SallaOrder | None:
        resp = self._get(f"/orders/{order_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json().get("data") or {}

        status_slug = str((data.get("status") or {}).get("slug") or "")
        payment_method = str(data.get("payment_method") or "")
        total = (data.get("amounts") or {}).get("total") or {}
        amount = Decimal(str(total.get("amount") or "0"))
        currency = str(total.get("currency") or data.get("currency") or "SAR")
        # Salla splits the buyer's number: `mobile` is the LOCAL part and the
        # country code lives beside it in `mobile_code`. The repo's own
        # captured live response is {"mobile": "555000111", "mobile_code":
        # "+966"} — reading `mobile` alone produced a nine-digit string that
        # normalize_order_phone rightly refused, so order_phone_e164 was NULL:
        # no welcome template, and the buyer's reply could not claim their own
        # paid order. Join them when the code is there.
        customer = data.get("customer") or {}
        phone = customer.get("mobile")
        code = str(customer.get("mobile_code") or "").strip()
        if phone and code:
            phone = f"{code}{str(phone).lstrip('0')}"

        items_resp = self._get("/orders/items", params={"order_id": order_id})
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
