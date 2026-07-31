"""Founding seats — an honest counter, not a marketing number.

The store page promises: «٣٠ كرسي مؤسس — والعدد الباقي مكتوب هنا بالصفحة،
محدّث أولًا بأول» and «كل ما انحجز كرسي، ننزّل العدد المكتوب فوق. اللي تشوفه
هو الصحيح، وما نبيع الكرسي رقم ٣١».

Two mechanisms keep that promise, deliberately separate:

* **Enforcement is Salla's**, via product quantity = 30. A store that sells
  out cannot oversell, with no code of ours in the payment path — the safest
  possible place for the hard limit.
* **Reporting is ours**, right here, computed from real subscription rows so
  the operator's watchtower shows the true count rather than a number someone
  remembered to edit.

A seat is a *paid pass*, so the funnel product never consumes one (the store
copy says exactly that). Cancelled and refunded subscriptions release their
seat, matching «انقطعت أكثر؟ الكرسي ينفتح لغيرك».
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import Subscription
from career.salla import subscriptions as sub_states

#: The alpha wave, per the approved launch strategy.
FOUNDING_SEATS_CAP = 30

#: Pass plans only — «التشخيص ما يحسب كرسي».
_SEAT_PLANS: frozenset[str] = frozenset({"basic", "professional", "executive"})

#: A seat is held while the subscription is live or recoverable; a refund,
#: cancellation or chargeback frees it.
_HOLDING_STATES: frozenset[str] = frozenset({
    sub_states.PAID_UNCLAIMED,
    sub_states.ONBOARDING,
    sub_states.ACTIVE,
    sub_states.GRACE,
    sub_states.PAUSED,
})


@dataclass(frozen=True)
class Seats:
    cap: int
    taken: int

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.taken)

    @property
    def sold_out(self) -> bool:
        return self.remaining == 0


def founding_seats(session: Session, *, cap: int = FOUNDING_SEATS_CAP) -> Seats:
    """The real count, from subscription rows. Runs as owner (spans tenants).

    Counted per CUSTOMER, not per row: since renewals (§16) one person holds a
    subscription row per order, and counting rows would have burned a second
    founding seat every time somebody paid again."""
    taken = int(session.execute(
        select(func.count(func.distinct(Subscription.tenant_id))).where(
            Subscription.plan_code.in_(sorted(_SEAT_PLANS)),
            Subscription.status.in_(sorted(_HOLDING_STATES)),
        )
    ).scalar_one())
    return Seats(cap=cap, taken=taken)


def seats_line_ar(seats: Seats) -> str:
    """One Arabic line for the watchtower. Direction-pure, digits only."""
    if seats.sold_out:
        return f"🪑 كراسي التأسيس: اكتملت ({seats.taken}/{seats.cap})"
    return (
        f"🪑 كراسي التأسيس: باقي {seats.remaining} "
        f"من {seats.cap} (محجوز {seats.taken})"
    )
