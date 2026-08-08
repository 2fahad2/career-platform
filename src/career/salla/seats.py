"""Founding seats — an honest counter, not a marketing number.

The store page promises: «N كرسي مؤسس — والعدد الباقي مكتوب هنا بالصفحة،
محدّث أولًا بأول» and «كل ما انحجز كرسي، ننزّل العدد المكتوب فوق. اللي تشوفه
هو الصحيح».

N is :data:`FOUNDING_SEATS_CAP` and **N is forty**, per the owner's decision of
2026-08-08. It was thirty, and the store copy still says thirty — the product
pages, the terms and the guarantee are being rewritten in one deliberate pass
and are not edited piecemeal, so `docs/STORE-PAGES-AR.md` §الكرسي,
`docs/PRODUCTS-SHEET.md` and `docs/WHITEPAPER.html` still read ٣٠/٣١ today and
are listed for that pass. The code is the number that is true; the copy is the
number that is still being written. Nothing here quotes «ما نبيع الكرسي رقم
٣١» as a live constraint any more, because ٣١ was never the constraint — the
cap was, and the cap moved.

**This module reports. It does not enforce, and nothing else does either.**
That correction matters enough to lead with, because the previous docstring
asserted the opposite as fact — «Enforcement is Salla's, via product quantity
= 30. A store that sells out cannot oversell» — and that is not true as the
store is configured. Salla stock is PER PRODUCT, and the three passes are
three separate products (provisioning maps one product id per plan), each
given its own quantity in the store sheet. Per-product stock cannot enforce a
pool that PRODUCTS-SHEET §الكرسي declares shared across all three: following
the sheet literally, the per-product quantities can add to more than the cap,
and «we do not sell the seat past the wave» has nothing behind it but
arithmetic nobody performs.

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

WHAT WAS STILL MISSING, AND IS NOW HERE. Everything above says «our counter
is honest» — and it was, while saying nothing about the OTHER counter. Salla
holds one quantity per product and decrements it on every sale; we hold
:data:`FOUNDING_SEATS_CAP` and count rows. Two counters, one published
promise, and **nothing in the repository ever compared them**. So the
comparison is now an object, :func:`supply_verdict`, over an invariant sharper
than «the two quantities add up to the wave»:

    taken + (what the store will still sell) == cap

That holds at every moment of the wave, not just at configuration time,
because Salla decrements as we count up. And it catches the OPPOSITE drift for
free — a refund releases our seat and Salla does **not** hand the quantity
back, so «الكرسي ينفتح لغيرك» quietly stops being true one seat at a time, and
nothing else in the product would ever have noticed that either.

THE CAP MOVED, AND THE INVARIANT DID NOT BUDGE — which is the whole point of
having written it as an invariant. Read live and read-only on 2026-08-08 the
store is لمّاح ``quantity = 30`` and لمّاح+ ``quantity = 10``: forty sellable.
Against the old cap of thirty with one seat taken that was drift ``+11``.
Against the new cap of forty it is drift ``+1``, and the ``+1`` is REAL — not
an artifact to be tuned away. Forty more sellable on top of one already held
is forty-one founders on a forty-seat wave.

WHY THAT ONE SEAT IS THE INTERESTING PART. The seat is held on plan ``basic``
at 1.00 SAR — the retired pass, sold in the reviewer probe. ``basic`` is in
:data:`_SEAT_PLANS`, so it holds a seat and always should: a paid pass is a
paid pass and the human on the other end is a founder. But **no product in the
catalog maps to ``basic``** any more, so Salla never decremented anything for
it and never can. It is a seat that exists on our side of the comparison and
has no counterpart at all on the store's side.

Two wrong ways to handle that, both tempting. Drop ``basic`` from the count and
the arithmetic goes clean — by deleting a founder. Or report a bare ``+1`` and
tell the operator to type 39, which is TRUE and actionable and still leaves him
staring at ٣٠ + ١٠ = ٤٠ = the cap, unable to see why the panel wants 39; a
panel whose instruction he cannot derive is a panel he stops believing.

So the drift stays honest and gains a REASON.
:attr:`SupplyVerdict.seats_on_unsold_plans` names the seats held on passes the
store has no product for, and the operator's lines carry it whenever they ask
him to type a number. It is deliberately NOT a failure of its own
(:attr:`SupplyVerdict.ok` ignores it): it is a permanent, correct, accepted
offset, and a nightly alarm about a permanent accepted fact is exactly the
noise that trains a reader to stop reading. It is reported where it changes an
instruction, and silent where it does not.

Reading the store is a GET and nothing else (:func:`read_seat_supply`). The
quantities are Fahad's hand, always: this module reports the disagreement and
never resolves it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import Subscription
from career.salla import subscriptions as sub_states

#: The founding wave. Forty since 2026-08-08 (owner's decision); thirty before
#: that, and the store copy has not caught up yet — see the module docstring.
#:
#: STILL A MODULE CONSTANT, ON PURPOSE, now that it has changed once and the
#: obvious reflex is «make it configurable». Three reasons it should not be:
#:
#: * It is a PUBLISHED PROMISE, not a deployment parameter. An env var lets
#:   staging and production advertise different waves, and a promise whose
#:   value depends on which machine reads it is not a promise. It also guts
#:   `tests/test_docs_truth.py`, which compares the store sheet's quantities
#:   against this number: pointed at an env var it would be checking the test
#:   runner's environment rather than what the page says.
#: * Deriving it from the store — ``cap = taken + sellable`` — is worse still.
#:   That is the invariant :func:`supply_verdict` exists to CHECK, and a
#:   derived cap makes the check vacuously true. The two counters have to stay
#:   independent or there is nothing to compare.
#: * The cost of a constant is a code change, a review and a test run for a
#:   number the owner may revisit. That cost is the feature. The wave size is a
#:   strategy decision; it should leave a commit and re-run the suite, not be a
#:   dashboard field somebody edits at 2am.
#:
#: What the 30→40 change actually taught is not «configure it» but «say it
#: once». The fragility was the number DUPLICATED — in test fixtures, in
#: «25 + 5» prose, in a subtraction the operator was expected to do by hand.
#: :func:`founding_seats` takes ``cap`` as a keyword so tests can drive
#: arithmetic without the constant, and :attr:`SupplyVerdict.target_quantity`
#: is the one place the store-side half is computed.
FOUNDING_SEATS_CAP = 40

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


def holds_seat(
    session: Session, *, tenant_id: uuid.UUID, plan_code: str
) -> bool:
    """Does this customer still occupy a founding seat on THIS pass?

    Published as a function because it is the predicate behind two halves of
    one sentence — «انقطعت أكثر؟ الكرسي ينفتح لغيرك، وترجع بسعر يومها» — and
    only the first half had an implementation. :data:`_HOLDING_STATES` already
    decides when the seat opens; ``promises/price_lock`` must lapse the founder
    price on exactly the same event, and the way that guarantee rots is two
    modules each keeping their own idea of «still our customer».

    Asked per PLAN, not per tenant, because a lock is per (tenant, plan) and a
    customer can hold both passes: refunding لمّاح+ must not take the لمّاح
    price with it. Asked over ALL of that tenant's rows on the plan, so a
    reversal on one order of several leaves the lock alone — the relationship
    ends when the last live row does, not when the first one is reversed.
    """
    if plan_code not in _SEAT_PLANS:
        return False
    count = session.execute(
        select(func.count()).select_from(Subscription).where(
            Subscription.tenant_id == tenant_id,
            Subscription.plan_code == plan_code,
            Subscription.status.in_(sorted(_HOLDING_STATES)),
        )
    ).scalar_one()
    return int(count) > 0


#: Latin digits drag an Arabic line's direction — Fahad's client reorders it
#: and «باقي 23 من 40» arrives scrambled. Arabic-Indic digits are Arabic
#: script and stay inside the sentence, which is the rule
#: `tests/test_alert_direction_purity.py` polices everywhere the operator
#: reads. Numbers that are NOT counts — a product id — go on their own line
#: instead, because they are identifiers and must stay transcribable.
_AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _ar(value: object) -> str:
    return str(value).translate(_AR_DIGITS)


def seats_line_ar(seats: Seats) -> str:
    """One Arabic line for the watchtower. Direction-pure, digits only.

    The oversold case gets its own sentence. «اكتملت» beside a number larger
    than the cap reads like a rounding artifact; nothing in the payment path
    prevents it, so when it happens the operator must be told that the wave
    was exceeded and by how much — that is the only warning that exists."""
    if seats.oversold:
        return (
            f"🪑 كراسي التأسيس: تجاوزنا العدد المعلن بـ {_ar(seats.oversold)} — "
            f"محجوز {_ar(seats.taken)} من {_ar(seats.cap)}، "
            "راجع كميات المنتجات في المتجر"
        )
    if seats.sold_out:
        return f"🪑 كراسي التأسيس: اكتملت ({_ar(seats.taken)}/{_ar(seats.cap)})"
    return (
        f"🪑 كراسي التأسيس: باقي {_ar(seats.remaining)} "
        f"من {_ar(seats.cap)} (محجوز {_ar(seats.taken)})"
    )


# ── the OTHER counter: what the store will still sell ────────────────────────

#: The Salla Admin API root, spelled the same way `salla/client.py` spells it.
SALLA_ADMIN_BASE = "https://api.salla.dev/admin/v2"


@dataclass(frozen=True)
class SeatSupply:
    """One read of the storefront: how many more seats Salla will sell.

    ``unlimited`` and ``unreadable`` are separate fields and neither is folded
    into a number, because both are answers a total cannot carry. An unlimited
    seat product is not «a big quantity», it is «no bound exists»; a product we
    could not read is not «zero left», it is «we do not know», and a check that
    reported either as a clean total would be the quietest possible failure.
    """

    #: product_id → units the store will still sell
    by_product: dict[str, int] = field(default_factory=dict)
    #: seat products the store is selling with no stock limit at all
    unlimited: tuple[str, ...] = ()
    #: seat products the API would not answer for (404, auth, network)
    unreadable: tuple[str, ...] = ()
    #: Pass plans the catalog has a product for — the plans Salla is able to
    #: sell, and therefore the only plans it can ever decrement. Recorded
    #: because the reverse question has an answer nothing used to ask: a seat
    #: held on a plan MISSING from here (today ``basic``, the retired pass) can
    #: never have a counterpart in ``by_product``, so it shifts the invariant
    #: permanently and the operator has to be told why.
    offered_plans: tuple[str, ...] = ()

    @property
    def sellable(self) -> int:
        return sum(self.by_product.values())

    @property
    def complete(self) -> bool:
        return not self.unlimited and not self.unreadable


def read_seat_supply(
    *,
    api_key: str,
    product_catalog: Mapping[str, str],
    base_url: str = SALLA_ADMIN_BASE,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> SeatSupply:
    """GET the seat products' quantities. **Read-only — no other verb.**

    One request per seat product (``GET /products/{id}``) rather than a listing
    page: the listing paginates and a seat product that fell off page one would
    read as «not configured», which is the one answer that must never be
    invented. Two products today, so it is two requests once a day.

    ``product_catalog`` is the same ``product_id → plan_code`` map §09
    provisions from, filtered here to the pass plans — so a product that stops
    being a pass stops being counted without anybody editing this file, and
    the funnel product («التشخيص ما يحسب كرسي») is never counted at all.

    Failures are DATA, not exceptions: an unreadable product lands in
    ``unreadable`` so the verdict can say «I could not check» instead of
    «clean». The caller may still see httpx raise on a broken client
    construction, and its own boot-check wrapper swallows that.
    """
    seat_products = sorted(
        pid for pid, plan in product_catalog.items() if plan in _SEAT_PLANS
    )
    offered_plans = tuple(sorted({
        str(plan) for plan in product_catalog.values() if plan in _SEAT_PLANS
    }))
    by_product: dict[str, int] = {}
    unlimited: list[str] = []
    unreadable: list[str] = []
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
        transport=transport,
    ) as client:
        for product_id in seat_products:
            try:
                resp = client.get(f"/products/{product_id}")
            except httpx.HTTPError:
                unreadable.append(product_id)
                continue
            if resp.status_code != 200:
                unreadable.append(product_id)
                continue
            data = (resp.json() or {}).get("data") or {}
            if data.get("unlimited_quantity"):
                unlimited.append(product_id)
                continue
            quantity = data.get("quantity")
            if quantity is None:
                # No quantity AND not flagged unlimited: Salla is describing a
                # product we do not understand. Guessing zero would read as
                # «sold out» and guessing large as «oversold»; both are lies.
                unreadable.append(product_id)
                continue
            by_product[product_id] = max(int(quantity), 0)
    return SeatSupply(
        by_product=by_product,
        unlimited=tuple(unlimited),
        unreadable=tuple(unreadable),
        offered_plans=offered_plans,
    )


@dataclass(frozen=True)
class SupplyVerdict:
    """Whether the store and our counter still describe the same wave."""

    seats: Seats
    supply: SeatSupply

    @property
    def drift(self) -> int:
        """``taken + still-sellable − cap``. Zero is the only right answer.

        POSITIVE is the money defect: the store will sell seats past the
        published wave, and Constant 4 forbids the payment path from refusing
        the order once it is paid — so the only place it can be stopped is the
        dashboard, before somebody buys.

        NEGATIVE is the promise defect pointing the other way: seats we
        released are seats Salla will never re-offer, so «الكرسي ينفتح لغيرك»
        goes quietly false. It is not an emergency and it is not nothing.
        """
        return self.seats.taken + self.supply.sellable - self.seats.cap

    @property
    def seats_on_unsold_plans(self) -> dict[str, int]:
        """plan_code → seats held on a pass the store has no product for.

        The reverse of every other question in this module, and the one that
        makes today's drift explicable instead of merely true. ``read_seat_
        supply`` walks the catalog and asks Salla about each seat product; this
        walks the seats we actually HOLD and asks whether the store has
        anything that could ever have decremented for them.

        Today that is ``{"basic": 1}`` — the retired 149 pass, sold once in the
        reviewer probe at 1.00 SAR, with no product id mapped to it any more.
        That seat is real and stays counted: a paid pass is a paid pass, and
        removing the founder to tidy the arithmetic would be the exact
        dishonesty this module was written against. But no Salla quantity was
        ever taken for it and none ever can be, so it moves the store-side
        target down by one **permanently**, and an operator reading
        ٣٠ + ١٠ = ٤٠ against a cap of ٤٠ cannot possibly derive that on his own.

        Empty when :attr:`SeatSupply.offered_plans` is empty, because that means
        we could not establish what the store sells at all — «no catalog» is
        not «every plan is unsellable», and inventing the loudest reading of a
        missing input is how a check earns its own dismissal.
        """
        if not self.supply.offered_plans:
            return {}
        offered = set(self.supply.offered_plans)
        return {
            plan: count
            for plan, count in sorted(self.seats.by_plan.items())
            if plan not in offered and count > 0
        }

    @property
    def ok(self) -> bool:
        """Deliberately blind to :attr:`seats_on_unsold_plans`.

        A seat on a retired pass is not a fault to be repaired — it is a
        permanent, correct offset that :attr:`target_quantity` already absorbs.
        Letting it hold ``ok`` False forever would put an unfixable line in the
        nightly channel every night, and the module's own rule is that a
        channel which cries about a fact nobody can act on stops being read.
        It explains a number the operator is asked to type; it is not itself a
        number he can change.
        """
        return self.drift == 0 and self.supply.complete

    @property
    def target_quantity(self) -> int:
        """What the seat products must add up to right now, for the operator
        to type. Derived, so nobody has to redo the subtraction at 2am.

        ``cap - taken`` already carries the retired-pass offset for free:
        ``basic`` is counted in ``taken`` like any other held seat, so the
        target it produces (39 today, not 40) is right without a special case.
        The special case is only needed to EXPLAIN it, never to compute it."""
        return max(0, self.seats.cap - self.seats.taken)


def supply_verdict(seats: Seats, supply: SeatSupply) -> SupplyVerdict:
    return SupplyVerdict(seats=seats, supply=supply)


def supply_lines_ar(verdict: SupplyVerdict) -> list[str]:
    """The operator's lines, or none at all when the two counters agree.

    Silence on a clean check is deliberate: this runs every night beside a
    boot check the operator already reads, and a nightly «all good» is how a
    channel stops being read. Every line is direction-pure — counts in
    Arabic-Indic digits inside the sentence, product ids alone on their own
    line because they are Latin identifiers he may have to type into Salla.
    """
    if verdict.ok:
        return []
    lines: list[str] = ["🪑 كراسي التأسيس — المتجر وعدّادنا ما يتفقان"]
    supply = verdict.supply
    if verdict.drift > 0:
        lines.append(
            f"المتجر يقدر يبيع {_ar(verdict.drift)} كرسيًا فوق العدد المعلن — "
            f"محجوز {_ar(verdict.seats.taken)} ومعروض للبيع "
            f"{_ar(supply.sellable)} والمعلن {_ar(verdict.seats.cap)}"
        )
        lines.append(
            "اضبط مجموع كميات منتجات الاشتراك على "
            f"{_ar(verdict.target_quantity)} بالضبط من لوحة سلة"
        )
    elif verdict.drift < 0:
        lines.append(
            f"ناقص {_ar(-verdict.drift)} كرسيًا عن العدد المعلن — "
            f"محجوز {_ar(verdict.seats.taken)} ومعروض للبيع "
            f"{_ar(supply.sellable)} والمعلن {_ar(verdict.seats.cap)}"
        )
        lines.append(
            "كرسي انفتح عندنا ولا رجّعته سلة للبيع — "
            f"ارفع مجموع الكميات إلى {_ar(verdict.target_quantity)}"
        )
    # The reason the target is not simply the cap. It rides with the drift
    # lines and only with them: it is an explanation of a number he is being
    # asked to type, not an alarm of its own (see `SupplyVerdict.ok`). The plan
    # code is a Latin identifier, so it takes its own line under the identifier
    # rule — the same treatment product ids get below.
    if verdict.drift != 0:
        for plan, count in verdict.seats_on_unsold_plans.items():
            lines.append(
                f"منها {_ar(count)} على باقة ما عاد لها منتج في المتجر — "
                "محجوزة عندنا وسلة ما خصمت لها ولا تقدر:"
            )
            lines.append(plan)
    for product_id in supply.unlimited:
        lines.append("منتج اشتراك بكمية غير محدودة — لا سقف عليه إطلاقًا:")
        lines.append(product_id)
    for product_id in supply.unreadable:
        lines.append("ما قدرنا نقرأ كمية هذا المنتج من سلة:")
        lines.append(product_id)
    return lines
