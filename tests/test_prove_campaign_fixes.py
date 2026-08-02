"""The three defects the execution campaign found that hit the FIRST paying
customer (2 August).

Every one of them was invisible to the existing suite because the suite fed
the code the shape it expected, not the shape the world sends.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from career.salla.activation_link import normalize_order_phone


def test_the_phone_shape_salla_really_returns_is_usable() -> None:
    """Salla splits the buyer's number: `mobile` carries the LOCAL part and
    the country code sits beside it in `mobile_code`. The repo's own captured
    live response is {"mobile": "555000111", "mobile_code": "+966"}. Reading
    `mobile` alone gave nine digits, which normalisation rightly refused — so
    order_phone_e164 was NULL, no welcome template was sent, and the buyer's
    reply could not claim the order they had just paid for."""
    assert normalize_order_phone("555000111") == "+966555000111"
    # and every shape that already worked keeps working
    assert normalize_order_phone("+966555000111") == "+966555000111"
    assert normalize_order_phone("0555000111") == "+966555000111"
    assert normalize_order_phone("00966555000111") == "+966555000111"
    assert normalize_order_phone("12345") is None


def test_the_client_joins_the_country_code_to_the_mobile() -> None:
    from career.salla.client import HttpSallaClient

    captured = {
        "data": {
            "id": 42, "status": {"slug": "completed"},
            "payment_method": "credit_card",
            "amounts": {"total": {"amount": "29.00", "currency": "SAR"}},
            "customer": {"id": 7, "mobile": "555000111", "mobile_code": "+966"},
        }
    }
    items = {"data": [{"product": {"id": "786318419"}}]}

    import httpx

    def _handler(request: httpx.Request) -> httpx.Response:
        body = items if "items" in request.url.path else captured
        return httpx.Response(200, json=body)

    client = HttpSallaClient("k", transport=httpx.MockTransport(_handler))
    order = client.get_order("42")

    assert order is not None
    assert normalize_order_phone(order.customer_phone) == "+966555000111"


def test_a_paid_order_for_an_unknown_product_is_never_silent(
    owner_session, clean_billing
):
    """It provisioned nothing, messaged nobody and alerted no one — the
    operator learned about it from the customer complaining, by which time the
    event was already marked ignored and unretryable. A cataloged product with
    missing PRICING fails loudly; this branch was the asymmetric one."""
    from career.salla.client import FakeSallaClient, SallaOrder
    from career.salla.provisioning import ProvisionStatus, provision_order
    from career.telegram.admin import FakeTelegramAdminClient

    order_id = f"ORD-{uuid.uuid4()}"
    order = SallaOrder(order_id, "paid", "prod_not_in_catalog",
                       Decimal("29.00"), "SAR", customer_phone="+966555000111")
    admin = FakeTelegramAdminClient()

    result = provision_order(
        owner_session, order_id,
        salla_client=FakeSallaClient({order_id: order}),
        product_catalog={"prod_pro": "professional"},
        expected_pricing={"prod_pro": (Decimal("279.00"), "SAR")},
        admin_client_hint=admin,
    )

    assert result.status is ProvisionStatus.UNKNOWN_PRODUCT
    assert admin.messages, "money arrived and nobody was told"
    assert any("غير معروف" in m for m in admin.messages)
    assert any("prod_not_in_catalog" in m for m in admin.messages)


def test_a_greeting_is_never_written_into_the_achievement_bank(
    owner_session, clean_billing
):
    """After «تأكيد الكل» a customer whose CV yielded no experience is asked
    «وش أبرز خبرة عملية عندك؟». Answering «السلام عليكم» used to store that
    as a CUSTOMER_CONFIRMED job title, satisfy the gap, and advance — and that
    title then fed every CV we generate (constant 5)."""
    from career.onboarding.achievement_render import normalize_ar
    from career.onboarding.orchestrator import _GREETINGS_NORMALIZED

    for greeting in ("السلام عليكم", "مرحبا", "مرحبًا", "هلا", "نكمل"):
        assert normalize_ar(greeting) in _GREETINGS_NORMALIZED, greeting
    # a real answer is still an answer
    assert normalize_ar("محلل أعمال") not in _GREETINGS_NORMALIZED
    assert normalize_ar("مهندس برمجيات") not in _GREETINGS_NORMALIZED
