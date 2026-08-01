"""Founding seats — an honest counter, not a marketing number.

The store page promises: «٣٠ كرسي مؤسس — والعدد الباقي مكتوب هنا بالصفحة،
محدّث أولًا بأول» and «كل ما انحجز كرسي، ننزّل العدد المكتوب فوق. اللي تشوفه
هو الصحيح، وما نبيع الكرسي رقم ٣١».

**This module reports. It does not enforce, and nothing else does either.**
That correction matters enough to lead with, because the previous docstring
asserted the opposite as fact — «Enforcement is Salla's, via product quantity
= 30. A store that sells out cannot oversell» — and that is not true as the
store is configured. Salla stock is PER PRODUCT, and the three passes are
three separate products (provisioning maps one product id per plan), each
given its own quantity in the store sheet. Per-product stock cannot enforce a
pool that PRODUCTS-SHEET §الكرسي declares shared across all three: following
the sheet literally, more than thirty founding seats are sellable, and the
sentence «ما نبيع الكرسي رقم ٣١» has nothing behind it but arithmetic nobody
performs.

Keeping no cap in the payment path is still the right call and is NOT the
defect: rejecting an order Salla has already taken money for would break
Constant 4 (a subscription exists whenever payment = paid) and strand a paid
customer with neither service nor refund. The honest fix for the hard limit
lives in the store configuration — one shared quantity, or a single pass
product with variants — and belongs to the owner. What belongs here is a
count that is true, and a line that says so out loud when it is exceeded.

So: **reporting is ours**, computed from real subscription rows so the
operator's watchtower shows the true count rather than a number someone
remembered to edit, and shouts when the pool has been oversold.

A seat is a *paid pass*, so the funnel product never consumes one (the store
copy says exactly that). Cancelled, refunded and expired subscriptions
release their seat, matching «انقطعت أكثر؟ الكرسي ينفتح لغيرك».
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
#:
#: SUSPENDED is held, and its absence was a real under-count: a renewal on a
#: disputed account is parked SUSPENDED (renewal.RenewalTarget.new_status)
#: and an operator can suspend anyone — a PAID customer awaiting a human, in
#: other words, who was not occupying a seat as far as this counter knew. It
#: reported a free seat that was not free, in the one direction that oversells
#: the wave.
#:
#: PAUSED is held and stays held: a paused period is still running (§05), and
#: since the lifecycle sweep now retires a paused row at its period end, the
#: seat is released on schedule instead of never.
_HOLDING_STATES: frozenset[str] = frozenset({
    sub_states.PAID_UNCLAIMED,
    sub_states.ONBOARDING,
    sub_states.ACTIVE,
    sub_states.GRACE,
    sub_states.PAUSED,
    sub_states.SUSPENDED,
})


@dataclass(frozen=True)
class Seats:
    cap: int
    taken: int
    #: plan_code → customers holding a seat on that pass. The store gives each
    #: pass its own Salla quantity, so reconciling «what Salla thinks it has
    #: left» against «who actually holds a seat» needs the breakdown, not just
    #: the total.
    by_plan: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.taken)

    @property
    def sold_out(self) -> bool:
        return self.remaining == 0

    @property
    def oversold(self) -> int:
        """Seats sold beyond the published wave. Should be zero; is not
        structurally prevented anywhere, so it is measured."""
        return max(0, self.taken - self.cap)


def founding_seats(session: Session, *, cap: int = FOUNDING_SEATS_CAP) -> Seats:
    """The real count, from subscription rows. Runs as owner (spans tenants).

    Counted per CUSTOMER, not per row: since renewals (§16) one person holds a
    subscription row per order, and counting rows would have burned a second
    founding seat every time somebody paid again. A customer who holds rows on
    two different passes is likewise one seat in the total, and appears under
    each pass in the breakdown."""
    rows = session.execute(
        select(
            Subscription.plan_code,
            func.count(func.distinct(Subscription.tenant_id)),
        ).where(
            Subscription.plan_code.in_(sorted(_SEAT_PLANS)),
            Subscription.status.in_(sorted(_HOLDING_STATES)),
        ).group_by(Subscription.plan_code)
    ).all()
    by_plan = {str(plan): int(n) for plan, n in rows}
    taken = int(session.execute(
        select(func.count(func.distinct(Subscription.tenant_id))).where(
            Subscription.plan_code.in_(sorted(_SEAT_PLANS)),
            Subscription.status.in_(sorted(_HOLDING_STATES)),
        )
    ).scalar_one())
    return Seats(cap=cap, taken=taken, by_plan=by_plan)


def seats_line_ar(seats: Seats) -> str:
    """One Arabic line for the watchtower. Direction-pure, digits only.

    The oversold case gets its own sentence. «اكتملت» beside a number larger
    than the cap reads like a rounding artifact; nothing in the payment path
    prevents it, so when it happens the operator must be told that the wave
    was exceeded and by how much — that is the only warning that exists."""
    if seats.oversold:
        return (
            f"🪑 كراسي التأسيس: تجاوزنا العدد المعلن بـ {seats.oversold} — "
            f"محجوز {seats.taken} من {seats.cap}، راجع كميات المنتجات في المتجر"
        )
    if seats.sold_out:
        return f"🪑 كراسي التأسيس: اكتملت ({seats.taken}/{seats.cap})"
    return (
        f"🪑 كراسي التأسيس: باقي {seats.remaining} "
        f"من {seats.cap} (محجوز {seats.taken})"
    )
