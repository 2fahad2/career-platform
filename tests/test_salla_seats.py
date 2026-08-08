"""The founding wave has TWO counters, and until today nothing compared them.

`tests/test_founding_seats.py` proves ours is honest. This file is about the
other one — the per-product ``quantity`` a human types into the Salla
dashboard — and about the promise that binds them: «كل ما انحجز كرسي، ننزّل
العدد المكتوب فوق … وما نبيع فوق الموجة المعلنة».

Read live and read-only on 2026-08-08, the store is configured لمّاح
``quantity = 30`` and لمّاح+ ``quantity = 10``. Forty. The sheet that
configures it (`docs/STORE-PAGES-AR.md` §إعدادات سلة) says 25 + 5, our cap
says forty since the owner's decision of the same day, and every one of those
three numbers was written down somewhere nobody executes. So the checks below
are written against the INVARIANT rather than against any of the numbers:

    taken + (what the store will still sell) == cap

THE CAP MOVED WHILE THIS FILE WAS ALIVE, thirty to forty, and that is the best
evidence the invariant was the right thing to write: two tests needed their
subject restated and none needed its logic changed. Where a case here quotes a
bare ``cap=30`` it is an ARBITRARY wave chosen to exercise arithmetic, never
the constant — the tests that are about the live configuration say
``FOUNDING_SEATS_CAP`` and mean it.

The second half of the file is about the other sentence in the same block —
«انقطعت أكثر؟ الكرسي ينفتح لغيرك، وترجع بسعر يومها» — which has two
consequences and, today, one implementation. The seat opens. The founder price
does not lapse. See the xfail block at the bottom, which is written against
the patch and will FAIL the day the patch lands, which is how it removes
itself.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.salla.seats import (
    FOUNDING_SEATS_CAP,
    Seats,
    founding_seats,
    holds_seat,
    read_seat_supply,
    seats_line_ar,
    supply_lines_ar,
    supply_verdict,
)

NOW = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)

#: The live catalog, as `.env.staging` carries it on 2026-08-08.
CATALOG = {
    "471675272": "cv_analysis",
    "1778012297": "professional",
    "1252550546": "executive",
}


def _sub(session: Session, tenant_id: str, plan: str, status: str) -> None:
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t, :p, :s, :o,"
        " :a, 'SAR')"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "p": plan, "s": status,
         "o": f"O-{uuid.uuid4()}", "a": Decimal("199")})


def _store(quantities: dict[str, object]) -> httpx.MockTransport:
    """A fake storefront. Keys are product ids; a value of None means the
    product answers 404, and a dict is a raw payload."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "this module may only ever read Salla"
        product_id = request.url.path.rsplit("/", 1)[-1]
        if product_id not in quantities:
            return httpx.Response(404, json={"data": None})
        value = quantities[product_id]
        if value is None:
            return httpx.Response(500, json={})
        payload = value if isinstance(value, dict) else {
            "id": int(product_id), "quantity": value,
            "unlimited_quantity": False,
        }
        return httpx.Response(200, json={"data": payload})
    return httpx.MockTransport(handler)


# ── holds_seat: one predicate for both halves of one sentence ───────────────


def test_holds_seat_answers_per_plan_not_per_customer(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """A customer on both passes who reverses one keeps the other's seat —
    and therefore must keep the other's price lock."""
    a, _ = two_tenants
    _sub(owner_session, a, "professional", "ACTIVE")
    _sub(owner_session, a, "executive", "REFUNDED")
    owner_session.commit()
    tenant = uuid.UUID(a)
    assert holds_seat(owner_session, tenant_id=tenant, plan_code="professional")
    assert not holds_seat(owner_session, tenant_id=tenant, plan_code="executive")


def test_holds_seat_survives_a_reversal_of_one_order_among_several(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """The precision that keeps a goodwill refund from destroying a live
    customer's founder price: the relationship ends with the LAST live row."""
    a, _ = two_tenants
    _sub(owner_session, a, "professional", "REFUNDED")   # last month's order
    _sub(owner_session, a, "professional", "ACTIVE")     # the current one
    owner_session.commit()
    assert holds_seat(
        owner_session, tenant_id=uuid.UUID(a), plan_code="professional")


def test_holds_seat_refuses_the_funnel_product(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """«التقييم ما يحسب كرسي» — and it has no lock and no renewal either."""
    a, _ = two_tenants
    _sub(owner_session, a, "cv_analysis", "ACTIVE")
    owner_session.commit()
    assert not holds_seat(
        owner_session, tenant_id=uuid.UUID(a), plan_code="cv_analysis")


def test_holds_seat_and_the_counter_cannot_disagree(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """Both read `_HOLDING_STATES`; this pins that they keep doing so."""
    a, b = two_tenants
    _sub(owner_session, a, "professional", "SUSPENDED")
    _sub(owner_session, b, "professional", "CANCELED")
    owner_session.commit()
    seats = founding_seats(owner_session)
    assert seats.by_plan.get("professional") == 1
    assert holds_seat(
        owner_session, tenant_id=uuid.UUID(a), plan_code="professional")
    assert not holds_seat(
        owner_session, tenant_id=uuid.UUID(b), plan_code="professional")


# ── the store's quantity, read back ─────────────────────────────────────────


def test_the_reader_only_ever_issues_a_GET() -> None:
    """The whole module's licence: `products.read_write` is granted, and this
    code must never use the write half. Asserted in the transport, so a POST
    added here fails rather than changing a live product."""
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({"1778012297": 25, "1252550546": 5}),
    )
    assert supply.by_product == {"1778012297": 25, "1252550546": 5}
    assert supply.sellable == 30


def test_the_funnel_product_is_never_read_as_a_seat() -> None:
    """`cv_analysis` has unlimited stock by design; counting it would make
    every check report an infinite oversell."""
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({"1778012297": 25, "1252550546": 5, "471675272": 999}),
    )
    assert "471675272" not in supply.by_product


def test_an_unreadable_product_is_not_reported_as_zero() -> None:
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({"1778012297": 25, "1252550546": None}),
    )
    assert supply.unreadable == ("1252550546",)
    assert not supply.complete
    assert not supply_verdict(Seats(cap=30, taken=5), supply).ok


def test_an_unlimited_seat_product_is_not_a_big_number() -> None:
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({
            "1778012297": {"id": 1778012297, "quantity": None,
                           "unlimited_quantity": True},
            "1252550546": 5,
        }),
    )
    assert supply.unlimited == ("1778012297",)
    assert supply.sellable == 5           # NOT folded into the total
    verdict = supply_verdict(Seats(cap=30, taken=25), supply)
    assert verdict.drift == 0             # the arithmetic says «fine»
    assert not verdict.ok                 # and the verdict still says «no»


# ── the invariant this file exists for ──────────────────────────────────────


def test_the_wave_grew_to_forty_and_the_live_store_STILL_oversells_it() -> None:
    """THE DEFECT, in the exact numbers read live and read-only on 2026-08-08 —
    and the reason nobody may read this test's green as a bug being fixed.

    This test used to be called «the live store configuration oversells the
    published wave» and asserted drift ``+11``. Nothing about the store
    changed. The store is still لمّاح ``30`` and لمّاح+ ``10``; the seat is
    still one live ``basic`` subscription (both re-read on 2026-08-08, the
    store over ``GET /admin/v2/products``, the seat over the staging rows).
    What changed is the PROMISE: the owner moved the wave from thirty to
    forty, and ten of those eleven oversold seats became seats we are entitled
    to sell. A resolution by decision, not by repair — so the name had to stop
    saying «oversells the published wave» as though somebody had gone and
    fixed the quantities, and had to keep saying that the store oversells,
    because it does.

    The remaining ``+1`` is REAL and is the interesting half. One founder
    holds a ``basic`` seat, ``basic`` is a retired pass with no product in the
    catalog, so Salla never decremented for it and never can: forty sellable
    on top of one held is forty-one founders on a forty-seat wave. Constant 4
    still forbids the payment path from refusing the forty-first buyer once he
    has paid, so the Salla dashboard is still the only place it can be
    stopped, and the number the operator must type is still not the cap.
    """
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({"1778012297": 30, "1252550546": 10}),
    )
    # by_plan carries the live shape too: the one seat is on the retired pass.
    live_seats = Seats(cap=FOUNDING_SEATS_CAP, taken=1, by_plan={"basic": 1})
    verdict = supply_verdict(live_seats, supply)

    assert supply.sellable == 40
    assert verdict.drift == 1
    assert not verdict.ok
    assert verdict.target_quantity == 39

    # the operator is not asked to type 39 without being told why it is not 40
    assert verdict.seats_on_unsold_plans == {"basic": 1}
    lines = supply_lines_ar(verdict)
    assert any("ما عاد لها منتج" in line for line in lines)
    assert "basic" in lines

    # WHAT MOVED, asserted rather than asserted-about: the very same store
    # read, against the wave as it stood before the decision, is the same
    # eleven-seat oversell this test was written for. The store did not change.
    assert supply_verdict(
        Seats(cap=30, taken=1, by_plan={"basic": 1}), supply).drift == 11


def test_a_cold_start_configured_to_the_wave_passes_the_check() -> None:
    """The property the sheet test was really carrying: quantities that add up
    to the wave, with nothing sold yet, are clean and say nothing.

    Derived from :data:`FOUNDING_SEATS_CAP` instead of quoting a number, so the
    next decision cannot make it wrong — the previous version of this test
    hard-coded «25 + 5» from a document and went red the day the wave moved,
    which is the failure mode this file exists to notice, not to reproduce.
    """
    lammah = FOUNDING_SEATS_CAP - 5
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({"1778012297": lammah, "1252550546": 5}),
    )
    verdict = supply_verdict(Seats(cap=FOUNDING_SEATS_CAP, taken=0), supply)
    assert supply.sellable == FOUNDING_SEATS_CAP
    assert verdict.ok and supply_lines_ar(verdict) == []


def test_the_sheet_still_configures_the_wave_we_no_longer_sell() -> None:
    """And the same check, driven by the DOCUMENT's own numbers, which now
    fail it — the finding has flipped ends.

    Before the decision the sheet was right and the store was wrong, and this
    test's ancestor said so by passing. The sheet has not been touched: it
    still instructs ٢٥ + ٥ = ٣٠, and the store copy, the terms and the
    guarantee are being rewritten in ONE deliberate pass by the owner, so it
    is stale on purpose and not for a test to edit around. Following it today
    would configure a thirty-seat wave for a forty-seat promise: ten seats
    sold to nobody, and «الكرسي ينفتح لغيرك» quietly false by ten.

    Kept reading the file rather than hard-coding ٣٠ so the link survives, and
    written so it FAILS the day the sheet is rewritten to the new wave — that
    is not an accident and not a trap to route around. Whoever performs the
    rewrite should delete this test: `test_a_cold_start_configured_to_the_wave
    _passes_the_check` above already carries the invariant, and the document's
    agreement with the cap is `tests/test_docs_truth.py`'s job.
    """
    import pathlib

    text = pathlib.Path("docs/STORE-PAGES-AR.md").read_text(encoding="utf-8")
    quantities = [
        int(m.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
        for m in re.findall(r"\|\s*كمية «لمّاح\+?»\s*\|\s*\*\*([٠-٩\d]+)\*\*", text)
    ]
    assert len(quantities) == 2, "both pass quantities must be in the table"
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({"1778012297": quantities[0],
                          "1252550546": quantities[1]}),
    )
    verdict = supply_verdict(Seats(cap=FOUNDING_SEATS_CAP, taken=0), supply)

    assert verdict.drift == sum(quantities) - FOUNDING_SEATS_CAP
    assert verdict.drift < 0, (
        "the store sheet now agrees with the wave — good; delete this test, "
        "the cold-start invariant above already covers the arithmetic"
    )
    assert not verdict.ok
    # nothing is sold, so what he must type IS the whole wave
    assert verdict.target_quantity == FOUNDING_SEATS_CAP
    assert any("ارفع مجموع الكميات" in line for line in supply_lines_ar(verdict))


def test_the_invariant_holds_as_the_wave_sells_not_only_at_setup() -> None:
    """Salla decrements as we count up, so the check stays true mid-wave —
    that is why it is `taken + sellable == cap` and not `quantities == 30`."""
    for sold in range(0, 31):
        supply = read_seat_supply(
            api_key="x", product_catalog=CATALOG,
            transport=_store({"1778012297": max(25 - sold, 0),
                              "1252550546": max(5 - max(sold - 25, 0), 0)}),
        )
        verdict = supply_verdict(Seats(cap=30, taken=sold), supply)
        assert verdict.ok, (sold, supply.sellable)


def test_a_released_seat_that_salla_never_reoffers_is_reported_too() -> None:
    """«انقطعت أكثر؟ الكرسي ينفتح لغيرك» — a refund frees our seat and Salla
    keeps the decrement, so the reopened seat is unsellable and the page's
    promise goes quietly false. Negative drift, and it is said out loud."""
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({"1778012297": 5, "1252550546": 0}),
    )
    verdict = supply_verdict(Seats(cap=30, taken=24), supply)   # one refunded
    assert verdict.drift == -1
    assert not verdict.ok
    assert verdict.target_quantity == 6


# ── the operator reads these on a phone that reorders mixed lines ───────────


def test_every_operator_line_is_direction_pure() -> None:
    """Counts inline in Arabic-Indic digits, product ids alone on their line."""
    supply = read_seat_supply(
        api_key="x", product_catalog=CATALOG,
        transport=_store({
            "1778012297": {"id": 1778012297, "quantity": None,
                           "unlimited_quantity": True},
            "1252550546": None,
        }),
    )
    lines = supply_lines_ar(supply_verdict(Seats(cap=30, taken=1), supply))
    assert lines
    arabic = re.compile("[؀-ۿ]")
    for line in lines:
        assert "\n" not in line
        if arabic.search(line):
            assert not re.search(r"[A-Za-z0-9]", line), line


def test_the_seats_line_carries_no_latin_digits() -> None:
    """It is Arabic prose with numbers in it, and it goes to a phone that
    reorders any line mixing the two scripts."""
    for seats in (Seats(cap=30, taken=4), Seats(cap=30, taken=30),
                  Seats(cap=30, taken=41)):
        line = seats_line_ar(seats)
        assert not re.search(r"[A-Za-z0-9]", line), line


# ── the second half of the same sentence, now implemented ──────────────────
#
# `promises/price_lock.lapse_for_terminal`, called from the single place every
# terminal transition passes through (`salla/subscriptions.apply_order_lifecycle`).
# The xfail(strict) marker that used to stand here is gone because the defect
# is: strict=True would now fail the suite as XPASS, which is exactly how that
# marker was written to force its own removal.


def test_a_reversal_lapses_the_founder_price_with_the_seat(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """«انقطعت أكثر؟ الكرسي ينفتح لغيرك، وترجع بسعر يومها» — one sentence,
    two consequences, and for a long time only the first one happened.

    The permanent version of the hole is the buyer who never activated: his
    row carries no ``current_period_end``, so ``price_lock._continuous``
    returns True forever (no periods ⇒ nothing has lapsed) and the lock can
    never age out on its own. He charges back, keeps nothing, owes nothing —
    and keeps a standing right to the founding price for as long as the
    product exists.
    """
    from career.db.models import Subscription
    from career.promises import price_lock
    from career.salla import subscriptions as sub_states

    a, _ = two_tenants
    tenant = uuid.UUID(a)
    sub_id = uuid.uuid4()
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t,"
        " 'professional', 'PAID_UNCLAIMED', :o, 199, 'SAR')"),
        {"i": str(sub_id), "t": a, "o": f"O-{uuid.uuid4()}"})
    owner_session.commit()

    price_lock.capture(
        owner_session, tenant_id=tenant, plan_code="professional",
        amount=Decimal("199.00"), currency="SAR",
        subscription_id=sub_id, now=NOW,
    )
    owner_session.commit()
    assert price_lock.active_lock(
        owner_session, tenant_id=tenant, plan_code="professional") is not None

    sub = owner_session.get(Subscription, sub_id)
    assert sub is not None
    sub_states.apply_order_lifecycle(
        owner_session, sub, "order.chargeback", salla_order_id=sub.salla_order_id)
    owner_session.commit()

    # the seat opened, exactly as the sentence promises …
    assert not holds_seat(
        owner_session, tenant_id=tenant, plan_code="professional")
    assert founding_seats(owner_session).by_plan.get("professional", 0) == 0

    # … and the price must have gone with it. It does not.
    assert price_lock.active_lock(
        owner_session, tenant_id=tenant, plan_code="professional") is None, (
        "a charged-back customer still holds a live founder price lock"
    )


def test_the_lock_of_a_never_activated_buyer_cannot_age_out_on_its_own(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """Why the missing lapse is PERMANENT and not a seven-day window.

    ``_continuous`` measures from ``current_period_end``, and a buyer who paid
    but never activated has none — so «no periods ⇒ continuous» holds a year
    later just as well as it holds today. This one passes now and must keep
    passing after the patch: it is the reason the patch cannot be «wait for
    the continuity check to catch it».
    """
    from career.promises import price_lock

    a, _ = two_tenants
    tenant = uuid.UUID(a)
    sub_id = uuid.uuid4()
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t,"
        " 'professional', 'CHARGEBACK', :o, 199, 'SAR')"),
        {"i": str(sub_id), "t": a, "o": f"O-{uuid.uuid4()}"})
    owner_session.commit()
    price_lock.capture(
        owner_session, tenant_id=tenant, plan_code="professional",
        amount=Decimal("199.00"), currency="SAR",
        subscription_id=sub_id, now=NOW,
    )
    owner_session.commit()

    assert price_lock._continuous(
        owner_session, tenant_id=tenant, now=NOW + timedelta(days=365))
