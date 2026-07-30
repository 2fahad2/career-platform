"""Founding seats: the counter behind «باقي N من ٣٠» must be the real one."""

from __future__ import annotations

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
    """A paid-but-unactivated buyer already owns their seat — overselling it
    would break «ما نبيع الكرسي رقم ٣١»."""
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


def test_cap_matches_the_approved_launch_wave() -> None:
    assert FOUNDING_SEATS_CAP == 30
