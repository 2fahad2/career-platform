"""Founding seats: the counter behind «باقي N من M» must be the real one.

M is :data:`career.salla.seats.FOUNDING_SEATS_CAP`, and it MOVED — thirty
until the owner's decision of 2026-08-08, forty since. The caps written into
the arithmetic tests below are deliberately arbitrary and deliberately NOT the
constant: what they check is that the subtraction is right for any wave, which
is the property that survives the next decision. Exactly one test in this file
is about the constant's VALUE, and it is the last one.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.salla.seats import FOUNDING_SEATS_CAP, Seats, founding_seats, seats_line_ar


def _sub(session: Session, tenant_id: str, plan: str, status: str) -> None:
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t, :p, :s, :o,"
        " :a, 'SAR')"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "p": plan, "s": status,
         "o": f"O-{uuid.uuid4()}", "a": Decimal("149")})


def test_arithmetic_is_plain() -> None:
    s = Seats(cap=30, taken=7)
    assert s.remaining == 23 and not s.sold_out
    assert Seats(cap=30, taken=30).sold_out
    assert Seats(cap=30, taken=99).remaining == 0     # never negative


def test_only_paid_passes_take_a_seat(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """«التشخيص ما يحسب كرسي» — the funnel product must not consume one."""
    a, b = two_tenants
    _sub(owner_session, a, "basic", "ACTIVE")
    _sub(owner_session, b, "cv_analysis", "ACTIVE")
    owner_session.commit()
    assert founding_seats(owner_session).taken == 1


def test_refunds_and_cancellations_release_the_seat(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """«انقطعت أكثر؟ الكرسي ينفتح لغيرك»."""
    a, b = two_tenants
    _sub(owner_session, a, "professional", "REFUNDED")
    _sub(owner_session, b, "executive", "CANCELED")
    owner_session.commit()
    assert founding_seats(owner_session).taken == 0


def test_unclaimed_and_grace_still_hold_their_seat(
    owner_session: Session, clean_billing: None, two_tenants: tuple[str, str]
) -> None:
    """A paid-but-unactivated buyer already owns their seat — counting it free
    would sell the wave past its own size.

    The old wording here quoted «ما نبيع الكرسي رقم ٣١» as the thing broken.
    That sentence retired with the thirty-seat wave, and it was never the
    constraint anyway: the CAP is, and a cap that moves does not turn this
    test's subject into a different one.
    """
    a, b = two_tenants
    _sub(owner_session, a, "basic", "PAID_UNCLAIMED")
    _sub(owner_session, b, "basic", "GRACE")
    owner_session.commit()
    assert founding_seats(owner_session).taken == 2


def test_the_arabic_line_is_direction_pure() -> None:
    import re

    for seats in (Seats(cap=30, taken=4), Seats(cap=30, taken=30)):
        line = seats_line_ar(seats)
        assert not re.search(r"[A-Za-z]", line), line
        assert "\n" not in line


#: The wave, spelled the way a docstring spells it. Only so this test can
#: check that the paragraph recording the decision moved when the number did;
#: an unmapped size fails loudly rather than skipping the check.
_WAVE_IN_WORDS = {30: "thirty", 40: "forty", 50: "fifty", 100: "one hundred"}


def test_the_cap_is_the_approved_wave_and_says_where_that_was_approved(
) -> None:
    """The constant must equal the approved founding wave — and the approval
    must be findable, or the pin is «trust me» with an assert around it.

    WHAT THIS TEST IS FOR. It never protected the number thirty; it protected
    the AGREEMENT between the constant and the wave the owner approved. The
    wave moved to forty on 2026-08-08, so the pin moves with it. That is not
    weakening the test — the decision is the thing it tracks.

    WHY IT DOES NOT READ THE STORE COPY, which is the obvious way to make it
    check rather than trust. `docs/STORE-PAGES-AR.md`, `docs/PRODUCTS-SHEET.md`
    and `docs/WHITEPAPER.html` all still say ٣٠/٣١ and are stale ON PURPOSE:
    the pages, the terms and the guarantee are being rewritten in one pass and
    are not edited piecemeal. A test that read them today would pin the code to
    a number everybody already knows is wrong and would stay red until an
    unrelated copy rewrite lands. A red suite blocks every commit in the
    repository, so that is not honesty, it is a hostage — and the pressure it
    creates lands on whoever needs to ship at 02:00, which is how honest tests
    get deleted. The code-vs-sheet comparison is not lost: it lives in
    `tests/test_docs_truth.py`, which owns document truth and is where the copy
    rewrite must answer for itself.
    (Reported with this change, not fixed here: that comparison is one-sided —
    it only fails when the sheet sells MORE than the cap, so the sheet's ٢٥+٥
    now passes silently against a wave of forty.)

    So it reads the record that IS current: the paragraph in `salla/seats.py`
    that names the decision and its date. Bump the constant without moving that
    paragraph and this fails — which is exactly the moment somebody should be
    made to write down who decided, and when, for the next reader to check.

    Still missing, and reported rather than invented here: the decision has no
    line in `docs/CHANGELOG-v1.1.md`, which this repository's constitution puts
    AHEAD of code. When it gets one, this test should read the changelog and
    the docstring can go back to being prose.
    """
    from career.salla import seats as seats_module

    assert FOUNDING_SEATS_CAP == 40, (
        "the founding wave is forty since the owner's decision of 2026-08-08; "
        "if it moved again, move the record in salla/seats.py with it"
    )

    doc = (seats_module.__doc__ or "").lower()
    word = _WAVE_IN_WORDS.get(FOUNDING_SEATS_CAP)
    assert word is not None, (
        f"no word here for a wave of {FOUNDING_SEATS_CAP} — add one, and while "
        "you are there check the module docstring spells the new size out"
    )
    assert word in doc, (
        f"the cap is {FOUNDING_SEATS_CAP} and the paragraph that records the "
        "decision does not say so — the number is unverifiable again"
    )
    # `\s+` and not a space: the record is prose and prose wraps.
    assert re.search(r"owner's decision of\s+\d{4}-\d{2}-\d{2}", doc), (
        "the wave has no recorded approval and no date, so nobody downstream "
        "can check it against anything"
    )
