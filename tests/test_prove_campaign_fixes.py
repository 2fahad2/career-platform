"""The three defects the execution campaign found that hit the FIRST paying
customer (2 August).

Every one of them was invisible to the existing suite because the suite fed
the code the shape it expected, not the shape the world sends.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import text as sql_text

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


def test_a_delivered_day_can_never_be_undelivered() -> None:
    """Re-running the nightly the same Riyadh day is a normal recovery action
    and it used to be destructive: a second pass that found nothing rewrote a
    DELIVERED day as NO_MATCHES and told a customer who HAD received his jobs
    that we found none. The ledger only moves up now."""
    from career.cv.close import _outranks

    assert not _outranks("NO_MATCHES", "DELIVERED")
    assert not _outranks("CV_GENERATION_FAILED", "DELIVERED")
    assert not _outranks("WHATSAPP_FAILED", "PARTIAL_DELIVERY")
    # an equal or better outcome still refreshes
    assert _outranks("DELIVERED", "DELIVERED")
    assert _outranks("DELIVERED", "NO_MATCHES")
    assert _outranks("PARTIAL_DELIVERY", "WHATSAPP_FAILED")


def test_the_thank_you_never_mixes_directions_and_never_breaks() -> None:
    """Names come off the CV header and are usually Latin; a mixed line
    scrambles in the customer's client. And with no name the old text read
    «تسلم يا 🙏» — a broken sentence at the one moment we thank them."""
    from career.onboarding.enrichment import ack_thanks

    named = ack_thanks("Fahad Almulhim")
    assert "Fahad Almulhim" in named
    for line in named.splitlines():
        has_arabic = any("؀" <= ch <= "ۿ" for ch in line)
        has_latin = any(ch.isascii() and ch.isalpha() for ch in line)
        assert not (has_arabic and has_latin), line

    for empty in (None, "", "   "):
        plain = ack_thanks(empty)
        assert "يا" not in plain.split("\n")[0]
        assert plain.startswith("تسلم")


def test_a_disputed_account_is_not_promised_a_service_it_will_not_get() -> None:
    from career.salla.renewal import (
        RENEWED_CUSTOMER_AR,
        RENEWED_UNDER_REVIEW_AR,
    )

    assert "بنكمل عادي" in RENEWED_CUSTOMER_AR
    assert "بنكمل عادي" not in RENEWED_UNDER_REVIEW_AR
    assert "مراجعة" in RENEWED_UNDER_REVIEW_AR
    assert "دعم" in RENEWED_UNDER_REVIEW_AR


def test_a_tenant_code_collision_is_retried_not_fatal(owner_session, clean_billing):
    """max(code)+1 is a read-then-write. Two overlapping provisioning passes
    read the same maximum and try the same code; the loser used to raise
    UniqueViolation into the poison guard, which marked the paid webhook
    'failed' FOREVER — the buyer paid and got nothing recoverable.

    Simulated by handing the allocator a code that is already taken on its
    first attempt, exactly as the losing pass would have computed it."""
    import career.salla.provisioning as prov

    taken = f"TEN-Z{uuid.uuid4().int % 10_000:04d}"
    owner_session.execute(sql_text(
        "INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(uuid.uuid4()), "c": taken})
    owner_session.commit()

    calls = {"n": 0}
    real = prov._next_tenant_code

    def _collide_once(session):  # noqa: ANN001
        calls["n"] += 1
        return taken if calls["n"] == 1 else real(session)

    prov._next_tenant_code = _collide_once
    try:
        tenant = prov._create_tenant(owner_session)
        owner_session.commit()
    finally:
        prov._next_tenant_code = real

    assert calls["n"] >= 2, "the collision must trigger a retry"
    assert tenant.code != taken
    # and the session is still usable — the failed INSERT rolled back alone
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM tenants WHERE code = :c"),
        {"c": tenant.code}).scalar_one() == 1

    owner_session.execute(sql_text("DELETE FROM tenants WHERE code IN (:a, :b)"),
                          {"a": taken, "b": tenant.code})
    owner_session.commit()


def test_stray_chatter_at_the_cards_leaves_no_orphan_rows(
    owner_session, clean_billing, tmp_path
):
    """Every unmatched message at the path/policy card used to insert a fresh
    assessment and a fresh policy draft — a chatty customer left one orphan
    per message, forever, each with a bumped version number."""
    from career.db.models import SearchPolicy
    from career.onboarding import orchestrator as orch

    tid = uuid.uuid4()
    owner_session.execute(sql_text(
        "INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tid), "c": f"TEN-S{uuid.uuid4().int % 100_000:05d}"})
    owner_session.execute(sql_text(
        "INSERT INTO search_policies (id, tenant_id, version, status,"
        " approved_paths, cities) VALUES (:i, :t, 1, 'draft',"
        " '{}'::jsonb, '{}'::jsonb)"),
        {"i": str(uuid.uuid4()), "t": str(tid)})
    owner_session.commit()
    policy_id = owner_session.execute(sql_text(
        "SELECT id FROM search_policies WHERE tenant_id = :t"),
        {"t": str(tid)}).scalar_one()
    try:
        # the journey is already showing THIS draft
        found = orch._existing(owner_session, SearchPolicy, str(policy_id), tid)
        assert found is not None, "the card in front of the customer is reused"

        # a row belonging to someone else never resolves through context
        assert orch._existing(
            owner_session, SearchPolicy, str(policy_id), uuid.uuid4()) is None
        # and garbage in the context is simply ignored
        assert orch._existing(
            owner_session, SearchPolicy, "not-a-uuid", tid) is None
    finally:
        owner_session.rollback()
        owner_session.execute(sql_text(
            "DELETE FROM search_policies WHERE tenant_id = :t"), {"t": str(tid)})
        owner_session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                              {"t": str(tid)})
        owner_session.commit()


def test_no_test_order_describes_a_shape_salla_never_sends() -> None:
    """The structural guard behind the phone defect.

    A real Salla order ALWAYS carries the buyer's mobile — zero-touch
    activation, the welcome template and renewal matching all key on it. Test
    orders that omitted it were describing a shape the world never sends, and
    that unrealism is precisely how the defect survived for weeks: the suite
    was green while no real customer could have been activated.

    A new test order without a phone fails here rather than in production.
    """
    import pathlib
    import re

    offenders: list[str] = []
    for path in sorted(pathlib.Path("tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"SallaOrder\(", text):
            # look at the call and the two lines that follow it
            window = text[match.start(): match.start() + 400]
            call = window.split("\n\n")[0]
            if "customer_phone" not in call:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path}:{line}")
    assert offenders == [], (
        "these test orders carry no buyer mobile, a shape Salla never sends: "
        + ", ".join(offenders)
    )


def test_courtesy_is_recognised_by_shape_not_by_a_list() -> None:
    """The first guard was exact membership on thirteen phrases, so it caught
    «السلام عليكم» and missed «السلام عليكم ورحمة الله وبركاته», «صباح الخير»,
    «هلا والله», «شكرا» and «وعليكم السلام» — every one of which was then
    written into the achievement bank as a job title and fed into every CV.

    A greeting is not a list to enumerate; it is a message made ENTIRELY of
    courtesy words. And the rule must stay conservative in the other
    direction: any real content makes it an answer."""
    from career.onboarding.orchestrator import _is_courtesy_only as courtesy

    for greeting in ("السلام عليكم ورحمة الله وبركاته", "صباح الخير",
                     "مساء الخير", "هلا والله", "كيف الحال", "شكرا",
                     "وعليكم السلام", "مرحبًا", "نكمل", "hi", "تمام"):
        assert courtesy(greeting), greeting

    for answer in ("محلل أعمال", "مدير فرع في بنك الرياض", "مهندس برمجيات",
                   "هلا، شتغلت مدير فرع", "شكرا اشتغلت محاسب"):
        assert not courtesy(answer), answer


def test_only_a_real_delivery_is_protected_from_being_overwritten() -> None:
    """The first monotonic ledger ranked all eight states and broke the very
    constant it served: NO_MATCHES outranked every failure, so a re-run whose
    CV generation failed still read «no opportunities today» — money spent,
    failure discarded. LEDGER_FAILED, ranked lowest, could never be recorded
    at all, though §15.3 and §15.12 both lean on it."""
    from career.cv.close import _outranks

    # a delivery cannot be undone
    assert not _outranks("NO_MATCHES", "DELIVERED")
    assert not _outranks("CV_GENERATION_FAILED", "PARTIAL_DELIVERY")
    # but every honest failure can still be recorded over a non-delivery
    assert _outranks("CV_GENERATION_FAILED", "NO_MATCHES")
    assert _outranks("LEDGER_FAILED", "DELIVERED") is False
    assert _outranks("LEDGER_FAILED", "NO_MATCHES")
    assert _outranks("WHATSAPP_FAILED", "DISCOVERY_FAILED")
    # and a recovery run that finally lands is always allowed
    assert _outranks("DELIVERED", "WHATSAPP_FAILED")
    assert _outranks("PARTIAL_DELIVERY", "DELIVERED")
