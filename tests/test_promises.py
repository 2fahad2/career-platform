"""The three things the store SELLS — proven, not assumed.

Every test below is a sentence from a Salla product page that had no code
behind it: the 72-hour start guarantee («ما وصلتك أول فرصة خلال ٧٢ ساعة من
تفعيل اشتراكك؟ استرداد كامل أو تمديد الاشتراك — أنت تختار»), the لمّاح+ career
session («جلسة مسار واحدة متى طلبتها»), and the founding-seat price lock
(«سعره اليوم مقفول له … ما دام تجديده مستمر»).

The price-lock cases matter most and are the reason this file exists at all:
before `price_locks`, raising the store price turned every founder renewal
into `AMOUNT_MISMATCH` — a TERMINAL webhook status nothing re-reads — so the
founder paid and received nothing. Half the tests here are therefore the
narrowness of the relaxation: an amount that is neither today's price nor
that tenant's locked one must still fail closed, and it must still fail
closed for a stranger, for a tampered figure, and for a lock whose continuity
lapsed.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.models import CareerSession, DeliveryGuarantee
from career.promises import career_session, guarantee
from career.salla import subscriptions as sub_states
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import ProvisionStatus, provision_order
from career.telegram import console
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
ADMIN = "666"
#: The schedules in this system are written in Riyadh time — the delivery run,
#: the guarantee window the customer counts, and the operator's own day.
_RIYADH = ZoneInfo("Asia/Riyadh")

_CATALOG = {"prod_pro": "professional", "prod_plus": "executive",
            "prod_cv": "cv_analysis"}
#: The approved sheet: تقييم لمّاح ٢٩ · لمّاح ١٩٩ · لمّاح+ ٤٤٩.
_PRICING = {"prod_pro": (Decimal("199.00"), "SAR"),
            "prod_plus": (Decimal("449.00"), "SAR"),
            "prod_cv": (Decimal("29.00"), "SAR")}
#: The same store after a price rise — the event the founder lock exists for.
_PRICING_RAISED = {"prod_pro": (Decimal("249.00"), "SAR"),
                   "prod_plus": (Decimal("549.00"), "SAR"),
                   "prod_cv": (Decimal("29.00"), "SAR")}


class FakeProbes:
    def collect(self) -> dict[str, Any]:
        return {}

    def error_lines(self) -> list[str]:
        return []


def _phone() -> str:
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _buy(
    session: Session, *, product: str, phone: str | None = None,
    pricing: dict[str, tuple[Decimal, str]] | None = None,
    amount: Decimal | None = None, now: datetime = NOW,
    admin: FakeTelegramAdminClient | None = None,
):
    """One paid order through the real webhook-side entry point."""
    order_id = f"ORD-{uuid.uuid4()}"
    charged = amount if amount is not None else _PRICING[product][0]
    order = SallaOrder(order_id, "paid", product, charged, "SAR",
                       customer_phone=phone)
    return provision_order(
        session, order_id, salla_client=FakeSallaClient({order_id: order}),
        product_catalog=_CATALOG, expected_pricing=pricing or _PRICING,
        now=now, admin_client_hint=admin,
    ), order_id


def _customer(
    session: Session, *, product: str = "prod_pro", activated_at: datetime = NOW,
    period_days: int = 30,
) -> tuple[str, uuid.UUID, uuid.UUID]:
    """A real customer: paid, activated by token, journey ACTIVE, period live.

    The journey row is written here rather than driven through the whole C5
    conversation because the ONE column that matters to these tests is
    ``completed_at`` — the §05 anchor `policy.activate` stamps, and the
    timestamp the 72-hour guarantee is measured from.
    """
    phone = _phone()
    result, _ = _buy(session, product=product, phone=phone)
    activate(session, token=result.activation_token, from_phone=phone,
             display_name=None, now=activated_at,
             whatsapp_client=FakeWhatsAppClient(),
             admin_client=FakeTelegramAdminClient())
    tenant_id = uuid.UUID(str(result.tenant_id))
    sub_id = uuid.UUID(str(result.subscription_id))
    session.execute(sql_text(
        "UPDATE subscriptions SET status = 'ACTIVE', current_period_start = :s,"
        " current_period_end = :e WHERE id = :id"),
        {"s": activated_at, "e": activated_at + timedelta(days=period_days),
         "id": str(sub_id)})
    session.execute(sql_text(
        "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
        " state, state_entered_at, completed_at, context)"
        " VALUES (:id, :t, :s, 'ACTIVE', :at, :at, '{}')"),
        {"id": str(uuid.uuid4()), "t": str(tenant_id), "s": str(sub_id),
         "at": activated_at})
    session.commit()
    return phone, tenant_id, sub_id


def _day_state(
    session: Session, *, tenant_id: uuid.UUID, state: str, delivered: int,
    at: datetime, run_date: Any = None,
) -> None:
    session.execute(sql_text(
        "INSERT INTO tenant_day_states (id, tenant_id, run_date, state,"
        " counts, recorded_at) VALUES (:id, :t, :d, :s, :c, :at)"),
        {"id": str(uuid.uuid4()), "t": str(tenant_id),
         "d": (run_date or at.date()), "s": state,
         "c": f'{{"delivered": {delivered}, "failed_sends": 0}}', "at": at})
    session.commit()


def _code(session: Session, tenant_id: uuid.UUID) -> str:
    return session.execute(sql_text(
        "SELECT code FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
    ).scalar_one()


def _cbq(data: str, message_id: int = 10) -> dict[str, Any]:
    return {"callback_query": {
        "id": "cbq1", "from": {"id": int(ADMIN)}, "data": data,
        "message": {"message_id": message_id},
    }}


def _tap(session: Session, data: str, *, now: datetime = NOW) -> str:
    """One operator tap, returning the text they end up reading."""
    outcomes = console.handle_update(
        session, _cbq(data), admin_chat_id=ADMIN, probes=FakeProbes(), now=now,
    )
    return outcomes[-1].text


def _confirm(session: Session, code: str, action: str, *, now: datetime = NOW) -> str:
    """The full double-confirm path: the card's button, then «تأكيد نهائي»."""
    card = _tap(session, f"v1|act|{code}|{action}", now=now)
    assert "تأكيد" in card
    nonce = [n for n, (a, c, _e) in console._pending_actions.items()
             if a == action and c == code][-1]
    return _tap(session, f"v1|confirm|{nonce}", now=now)


# ── PROMISE 1 — «ضمان البداية»: أول فرصة خلال ٧٢ ساعة من التفعيل ────────────


def test_no_first_opportunity_in_72h_breaches_and_pages_the_operator(
    owner_session: Session, clean_billing: None,
) -> None:
    """«ما وصلتك أول فرصة خلال ٧٢ ساعة من تفعيل اشتراكك؟» — until now nothing
    asked that question, so a broken guarantee was invisible unless the
    customer complained."""
    _p, tenant_id, _s = _customer(owner_session)
    admin = FakeTelegramAdminClient()

    counts = guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73), admin_client=admin,
    )
    owner_session.commit()

    assert counts["breached"] == 1
    row = owner_session.execute(
        sql_text("SELECT status, breached_at, alerted_at FROM"
                 " delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).one()
    assert row[0] == guarantee.BREACHED
    assert row[1] is not None and row[2] is not None
    assert len(admin.messages) == 1
    assert _code(owner_session, tenant_id) in admin.messages[0]
    # §15.13: the alert names a TEN code and nothing else about the human
    assert "+9665" not in admin.messages[0]


def test_the_breach_is_paged_exactly_once(
    owner_session: Session, clean_billing: None,
) -> None:
    """A nightly sweep that re-pages every night is a sweep the operator
    learns to ignore — which is the same as not alerting at all."""
    _p, _t, _s = _customer(owner_session)
    admin = FakeTelegramAdminClient()
    for _ in range(3):
        guarantee.sweep_delivery_guarantee(
            owner_session, now=NOW + timedelta(hours=80), admin_client=admin,
        )
        owner_session.commit()
    assert len(admin.messages) == 1


# ── the CADENCE of the measurement, which is part of the promise ────────────
#
# The detector was correct from the day it was written and its clock was read
# once a day, at 11:00 Riyadh, because its only caller was the nightly
# delivery run. A promise the store writes in HOURS cannot be measured in
# DAYS: a guarantee that ran out at 12:05 sat undetected — unrecorded,
# unalerted, un-remedied — until 11:00 the following morning. The tests below
# are about the schedule and not about the detection: they drive the sweep at
# the cadence the system actually runs it at, and measure how late the answer
# is.


def _nightly_run_time() -> tuple[int, int]:
    """The hour the delivery run fires, read from the unit rather than typed.

    Same technique as `test_docs_truth`'s guard on `escalate_overdue`: the
    number that makes this test mean something lives in
    `ops/systemd/career-engine-nightly.timer`, and a test carrying its own
    private copy of it stops describing the system the day Fahad moves the
    run again (he has moved it once already — 04:30 to 11:00, 2 August).
    """
    unit = _REPO / "ops/systemd/career-engine-nightly.timer"
    fires = re.search(r"OnCalendar=\S+\s+(\d{2}):(\d{2}):\d{2}\s+(\S+)",
                      unit.read_text(encoding="utf-8"))
    assert fires is not None, f"{unit} no longer states an OnCalendar time"
    assert fires.group(3) == "Asia/Riyadh", (
        "the nightly timer no longer fires on Riyadh time — this test's "
        "arithmetic about «five minutes after the run» is written in it"
    )
    return int(fires.group(1)), int(fires.group(2))


def test_a_breach_that_lands_after_the_nightly_is_caught_within_the_hour(
    owner_session: Session, clean_billing: None,
) -> None:
    """THE TIMING DEFECT, in the only terms that matter: how late are we?

    A customer activates so that his seventy-two hours run out FIVE MINUTES
    after the delivery run has already swept and gone. With the nightly as the
    only caller — which is what it was until 2026-08-07 — the next look at his
    clock is at the same hour tomorrow, so the promise is broken at 11:05 and
    measured at 11:00 the next day: nearly twenty-three hours of a product
    knowing nothing about the sentence it leads with.

    The sweeps below are not invented for the test. The first is the nightly,
    at the hour its own unit file names; the rest are the hourly caller, one
    `SWEEP_INTERVAL_SECONDS` apart. The assertion is the promise: the answer
    is late by at most one sweep interval, and that interval is at most an
    hour.
    """
    interval = timedelta(seconds=guarantee.SWEEP_INTERVAL_SECONDS)
    hour, minute = _nightly_run_time()
    tonight = datetime(2026, 7, 19, hour, minute, tzinfo=_RIYADH)
    deadline = tonight + timedelta(minutes=5)
    _p, tenant_id, _s = _customer(
        owner_session,
        activated_at=deadline - timedelta(hours=guarantee.GUARANTEE_HOURS),
    )
    admin = FakeTelegramAdminClient()

    # the nightly runs five minutes early and honestly finds nothing yet
    counts = guarantee.sweep_and_commit(
        owner_session, now=tonight, admin_client=admin)
    assert counts["watching"] == 1 and counts["breached"] == 0
    assert admin.messages == []

    caught_at: datetime | None = None
    for step in range(1, 25):                      # a full day of hourly runs
        now = tonight + step * interval
        if guarantee.sweep_and_commit(
            owner_session, now=now, admin_client=admin
        )["breached"]:
            caught_at = now
            break

    assert caught_at is not None, (
        "a day of hourly sweeps never noticed a broken guarantee"
    )
    lateness = caught_at - deadline
    assert lateness <= timedelta(hours=1), (
        f"the breach was measured {lateness} after it became true. The store "
        "writes this promise in HOURS; a measurement coarser than an hour is "
        "coarser than the operator's own reaction time, and the cadence this "
        f"replaced ({(tonight + timedelta(days=1)) - deadline}) is the defect"
    )
    assert lateness <= interval, (
        f"…and it is the sweep interval ({interval}) that bounds it"
    )
    # the number this replaced, stated so the fix cannot silently rot back
    # into it: the next nightly is the following day at the same hour.
    assert (tonight + timedelta(days=1)) - deadline > timedelta(hours=23)

    status, breached_at, alerted_at = owner_session.execute(sql_text(
        "SELECT status, breached_at, alerted_at FROM delivery_guarantees"
        " WHERE tenant_id = :t"), {"t": str(tenant_id)}).one()
    assert status == guarantee.BREACHED
    # the RECORD is late by the same small amount, not by a day — this is the
    # timestamp the operator reads beside a decision about money
    assert breached_at - deadline <= interval
    assert alerted_at is not None
    assert len([m for m in admin.messages if "🛡️" in m]) == 1


def test_the_nightly_backstop_cannot_re_apply_what_the_hourly_sweep_did(
    owner_session: Session, clean_billing: None,
) -> None:
    """Two callers, one breach: the second must find nothing left to do.

    The hourly sweep is the schedule and the nightly is the backstop, so every
    breach is now looked at by two different processes about twenty-five times
    a day. Nothing may be counted twice, stamped twice, re-described twice or
    — the one the operator would actually feel — paged twice.

    The nightly half is driven through the REAL path (`sweep_promises`), not
    through the sweep directly: the guard has to hold for the caller that
    exists, including its own commit.
    """
    from career.cv.daily_run import DailyDeps, sweep_promises

    _p, tenant_id, _s = _customer(owner_session)
    hourly = FakeTelegramAdminClient()
    breach_time = NOW + timedelta(hours=73)
    guarantee.sweep_and_commit(
        owner_session, now=breach_time, admin_client=hourly)

    row = owner_session.execute(sql_text(
        "SELECT breached_at, alerted_at, facts FROM delivery_guarantees"
        " WHERE tenant_id = :t"), {"t": str(tenant_id)}).one()
    assert len([m for m in hourly.messages if "🛡️" in m]) == 1

    nightly = FakeTelegramAdminClient()
    deps = DailyDeps(storage=None, whatsapp_client=FakeWhatsAppClient(),
                     admin_client=nightly, llm=None)
    counts = sweep_promises(
        owner_session, deps=deps, now=breach_time + timedelta(hours=20))

    assert counts["breached"] == 0 and counts["alerted"] == 0
    assert [m for m in nightly.messages if "🛡️" in m] == []
    after = owner_session.execute(sql_text(
        "SELECT breached_at, alerted_at, facts FROM delivery_guarantees"
        " WHERE tenant_id = :t"), {"t": str(tenant_id)}).one()
    assert after == row          # same stamps, same frozen facts

    # and the remedy — the only half that moves money — is still applicable
    # exactly once, by the operator's hand and never by a sweep
    assert guarantee.apply_remedy(
        owner_session, tenant_id=tenant_id, remedy=guarantee.REMEDY_EXTENSION,
        now=breach_time + timedelta(hours=21),
    ).outcome == "applied"
    assert guarantee.apply_remedy(
        owner_session, tenant_id=tenant_id, remedy=guarantee.REMEDY_EXTENSION,
        now=breach_time + timedelta(hours=22),
    ).outcome == "already_settled"
    owner_session.commit()


def test_two_sweeps_in_flight_at_once_page_one_breach_exactly_once(
    owner_engine: Engine, owner_session: Session, clean_billing: None,
) -> None:
    """The race the second caller created, in two real transactions.

    Every «page once» guard in the sweep is a read followed by a write inside
    ONE transaction — `alerted_at is None`, then `alerted_at = now`. With a
    single nightly caller that was airtight. With an hourly sweep in the
    worker AND the nightly backstop, two passes can read the same WATCHING row
    in the same millisecond, and both would page: one customer, two identical
    breach alerts, on the channel whose entire value is that a message on it
    means something new happened.

    NO THREADS, deliberately — `pg_try_advisory_xact_lock` never waits, so the
    interleaving is built by hand and there is no timing window to lose.
    `lock_timeout` is the safety valve and not the subject: without the lock
    the second pass would block on the first one's uncommitted UPDATE, and a
    hang is a rotten way for a suite to report a defect.
    """
    _p, tenant_id, _s = _customer(owner_session)
    breach_time = NOW + timedelta(hours=73)
    first, second = FakeTelegramAdminClient(), FakeTelegramAdminClient()

    other = Session(owner_engine)
    try:
        # A — the hourly worker sweep. Reads, breaches, pages, uncommitted.
        counts = guarantee.sweep_delivery_guarantee(
            other, now=breach_time, admin_client=first)
        assert counts["alerted"] == 1

        # B — the nightly backstop, a millisecond later, in its own process.
        owner_session.execute(sql_text("SET LOCAL lock_timeout = '3s'"))
        assert guarantee.sweep_delivery_guarantee(
            owner_session, now=breach_time, admin_client=second,
        ) == {"watching": 0, "met": 0, "breached": 0, "alerted": 0}
        assert second.messages == [], "one breach, two pages"

        other.commit()
    finally:
        other.rollback()
        other.close()
    owner_session.rollback()

    # …and once A has committed, B's next pass genuinely has nothing to do:
    # the skip deferred no work, it declined to duplicate it. Not «zero
    # because it was locked out» this time — zero because the row is already
    # breached, already stamped and already paged.
    assert guarantee.sweep_and_commit(
        owner_session, now=breach_time + timedelta(hours=1),
        admin_client=second,
    ) == {"watching": 0, "met": 0, "breached": 0, "alerted": 0}
    assert second.messages == []
    assert len([m for m in first.messages if "🛡️" in m]) == 1


def test_the_operator_is_told_that_nobody_has_told_the_customer(
    owner_session: Session, clean_billing: None,
) -> None:
    """«الكشف والعلاج حيّان، وإبلاغ العميل معدوم» (STORE-PAGES §٨).

    We detect the broken promise, we page the operator, we can extend the
    subscription — and the person whose promise we broke is never told. The
    store page's answer is «راسلنا على واتساب»: the customer must notice, or
    he simply keeps a promise we know we broke. Wiring a real send is Fahad's
    decision (five questions, in `breach_notice_ar`'s docstring); until he
    makes it, the alert has to say the gap out loud and hand over the words,
    because this one survived being written into DEVIATIONS and STORE-PAGES
    twice without anybody scheduling it.
    """
    _p, tenant_id, sub_id = _customer(owner_session)
    admin = FakeTelegramAdminClient()
    guarantee.sweep_and_commit(
        owner_session, now=NOW + timedelta(hours=73), admin_client=admin)
    alert = admin.messages[0]

    assert "والعميل ما أُبلغ" in alert
    for line in guarantee.breach_notice_ar().splitlines():
        assert line in alert, "the ready text is not on the operator's screen"

    # …but NOT for an account whose money has already gone back: that text
    # offers «نرجّع لك المبلغ كاملًا», and a second refund is exactly what the
    # frozen `subscription_status` fact exists to prevent.
    _p2, _t2, sub2 = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'REFUNDED' WHERE id = :id"),
        {"id": str(sub2)})
    owner_session.commit()
    refunded = FakeTelegramAdminClient()
    guarantee.sweep_and_commit(
        owner_session, now=NOW + timedelta(hours=73), admin_client=refunded)
    assert guarantee.GUARANTEE_BREACH_CUSTOMER_AR not in refunded.messages[0]
    assert "راجع حالته قبل أي تعويض" in refunded.messages[0]


def test_the_customer_notice_still_has_nothing_that_sends_it() -> None:
    """A tripwire on the gap above, so it cannot be closed by accident.

    `GUARANTEE_BREACH_CUSTOMER_AR` had ZERO callers in the whole repository —
    tests included — for as long as it existed, and its own comment said so
    while three documents recorded it as «مبنيّ وغير موصول». It now has one:
    the operator's alert, which PRINTS it for him to copy. Nothing sends it.

    So the day a real sender appears, this test fails — on purpose. It is the
    one place that knows the five product questions in `breach_notice_ar`'s
    docstring are unanswered (trigger, channel and the 24h window, content,
    the customer's reply, the silenced customer). Answer them, then change
    this test to name the sender.
    """
    users: set[str] = set()
    for root in ("src", "scripts"):
        for path in sorted((_REPO / root).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if ("breach_notice_ar" in source
                    or "GUARANTEE_BREACH_CUSTOMER_AR" in source):
                users.add(str(path.relative_to(_REPO)))
    assert users == {"src/career/promises/guarantee.py"}, (
        f"the breach notice is now referenced by {sorted(users)}. If that is "
        "a real send to the customer, the five questions in "
        "breach_notice_ar's docstring are the decision it needs first — and "
        "this test is where the answer gets written down"
    )


def test_a_first_delivery_inside_the_window_meets_the_guarantee(
    owner_session: Session, clean_billing: None,
) -> None:
    _p, tenant_id, _s = _customer(owner_session)
    _day_state(owner_session, tenant_id=tenant_id, state="DELIVERED",
               delivered=2, at=NOW + timedelta(hours=20))
    admin = FakeTelegramAdminClient()

    counts = guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=99), admin_client=admin,
    )
    owner_session.commit()

    assert counts == {"watching": 0, "met": 1, "breached": 0, "alerted": 0}
    assert admin.messages == []
    row = owner_session.execute(
        sql_text("SELECT status FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    assert row == guarantee.MET


def test_a_partial_day_that_delivered_nothing_is_not_a_first_opportunity(
    owner_session: Session, clean_billing: None,
) -> None:
    """§15.12: «تسليم جزئي» with an empty delivered list is a failure wearing
    an amber word. Counting it would let the guarantee be «met» by a day on
    which the customer received nothing at all."""
    _p, tenant_id, _s = _customer(owner_session)
    _day_state(owner_session, tenant_id=tenant_id, state="PARTIAL_DELIVERY",
               delivered=0, at=NOW + timedelta(hours=20))

    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()

    assert owner_session.execute(
        sql_text("SELECT status FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == guarantee.BREACHED


def test_a_late_delivery_is_recorded_but_never_un_breaks_the_promise(
    owner_session: Session, clean_billing: None,
) -> None:
    _p, tenant_id, _s = _customer(owner_session)
    _day_state(owner_session, tenant_id=tenant_id, state="DELIVERED",
               delivered=1, at=NOW + timedelta(hours=90))

    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=100),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()

    status, first = owner_session.execute(
        sql_text("SELECT status, first_delivery_at FROM delivery_guarantees"
                 " WHERE tenant_id = :t"), {"t": str(tenant_id)}).one()
    assert status == guarantee.BREACHED
    assert first is not None          # the operator still learns it started


def test_the_breach_carries_the_facts_the_owner_judges_with(
    owner_session: Session, clean_billing: None,
) -> None:
    """«لا فرص مطابقة» for three days is a different conversation from three
    days of WhatsApp failures, and only one of them is our fault."""
    _p, tenant_id, _s = _customer(owner_session)
    _day_state(owner_session, tenant_id=tenant_id, state="NO_MATCHES",
               delivered=0, at=NOW + timedelta(hours=20))
    _day_state(owner_session, tenant_id=tenant_id, state="NO_MATCHES",
               delivered=0, at=NOW + timedelta(hours=44))

    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()

    row = owner_session.execute(
        sql_text("SELECT facts FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    assert row["day_states"] == {"NO_MATCHES": 2}
    assert row["paused"] is False and row["opted_out"] is False


def test_a_pause_the_customer_lifted_is_still_a_fact_about_the_window(
    owner_session: Session, clean_billing: None,
) -> None:
    """«وكان اشتراكه موقوفًا مؤقتًا بطلبه» is past tense, and the code used to
    answer it in the present: `subscription.status == PAUSED` read at SWEEP
    time. A customer who paused on day one and resumed on day two therefore
    read as never having paused — and «he asked us to stop delivering» is the
    single fact most likely to change whether the operator refunds."""
    _p, tenant_id, sub_id = _customer(owner_session)
    for at, to_status in ((NOW + timedelta(hours=2), "PAUSED"),
                          (NOW + timedelta(hours=30), "ACTIVE")):
        owner_session.execute(sql_text(
            "INSERT INTO subscription_events (id, tenant_id, subscription_id,"
            " event_type, to_status, details, created_at)"
            " VALUES (:id, :t, :s, 'customer_pause', :st, '{}', :at)"),
            {"id": str(uuid.uuid4()), "t": str(tenant_id), "s": str(sub_id),
             "st": to_status, "at": at})
    owner_session.commit()

    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()

    facts = owner_session.execute(
        sql_text("SELECT facts FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    assert facts["paused"] is True          # resumed long before the sweep


def test_an_opt_out_weeks_later_never_explains_an_old_breach(
    owner_session: Session, clean_billing: None,
) -> None:
    """`opt_out_at is not None` answers «have they EVER», and the facts packet
    is read beside a decision to move money. A customer who silenced us on day
    forty had that carried back onto a breach from day two as if it were the
    reason — and, because the facts were recomputed on every sweep, it grew
    into the record of a window that had been over for a month."""
    _p, tenant_id, _s = _customer(owner_session)
    admin = FakeTelegramAdminClient()
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73), admin_client=admin)
    owner_session.commit()

    owner_session.execute(sql_text(
        "UPDATE customer_channels SET opt_out_at = :at WHERE tenant_id = :t"),
        {"at": NOW + timedelta(days=40), "t": str(tenant_id)})
    owner_session.commit()
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(days=41), admin_client=admin)
    owner_session.commit()

    facts = owner_session.execute(
        sql_text("SELECT facts FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    assert facts["opted_out"] is False       # it happened outside the window
    assert len(admin.messages) == 1          # and it did not re-page anybody


def _inbound(
    session: Session, *, tenant_id: uuid.UUID, classification: str,
    at: datetime,
) -> None:
    """One «إيقاف» or «استئناف» exactly as `whatsapp.worker` records it."""
    channel_id = session.execute(sql_text(
        "SELECT id FROM customer_channels WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    session.execute(sql_text(
        "INSERT INTO inbound_messages (id, tenant_id, channel_id,"
        " wa_message_id, message_type, text_body, classification, payload,"
        " received_at) VALUES (:id, :t, :c, :w, 'text', NULL, :k, '{}', :at)"),
        {"id": str(uuid.uuid4()), "t": str(tenant_id), "c": str(channel_id),
         "w": f"wamid.{uuid.uuid4()}", "k": classification, "at": at})
    session.commit()


def test_a_stop_inside_the_window_outlives_the_resume_that_cleared_it(
    owner_session: Session, clean_billing: None,
) -> None:
    """The `paused` snapshot bug, still living in `opted_out`.

    `customer_channels.opt_out_at` remembers ONE flip: `whatsapp/worker` sets
    it on «إيقاف» and puts it back to NULL on «استئناف». So a customer who
    silenced us at hour one and came back at hour seventy-four read as
    ``opted_out: False`` at the sweep — for a window he was silent through,
    which is the single fact most likely to explain the breach and the one the
    operator needs before he moves money. The stop and the resume both carry a
    TIMESTAMP in `inbound_messages`; that is what the window is read from now.
    """
    _p, tenant_id, _s = _customer(owner_session)
    _inbound(owner_session, tenant_id=tenant_id, classification="stop",
             at=NOW + timedelta(hours=1))
    _inbound(owner_session, tenant_id=tenant_id, classification="resume",
             at=NOW + timedelta(hours=74))
    owner_session.execute(sql_text(          # exactly what the resume does
        "UPDATE customer_channels SET opt_out_at = NULL WHERE tenant_id = :t"),
        {"t": str(tenant_id)})
    owner_session.commit()

    admin = FakeTelegramAdminClient()
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=80), admin_client=admin)
    owner_session.commit()

    facts = owner_session.execute(
        sql_text("SELECT facts FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    assert facts["opted_out"] is True
    assert "وكان موقفًا للرسائل داخل المهلة" in admin.messages[0]


def test_a_silence_from_before_the_window_is_not_dressed_up_as_one_inside_it(
    owner_session: Session, clean_billing: None,
) -> None:
    """A column with no lower bound answers a question nobody asked.

    A buyer who replies, silences us during onboarding, and only activates
    days later carries an `opt_out_at` from BEFORE his guarantee window ever
    opened — and the alert reported it as «وكان موقفًا للرسائل داخل المهلة»,
    a sentence about the window that nothing inside the window supports. The
    fact is real and the operator still gets it; it just gets its own true
    sentence instead of borrowing another one's.
    """
    _p, tenant_id, _s = _customer(owner_session)
    _inbound(owner_session, tenant_id=tenant_id, classification="stop",
             at=NOW - timedelta(days=1))
    owner_session.execute(sql_text(
        "UPDATE customer_channels SET opt_out_at = :at WHERE tenant_id = :t"),
        {"at": NOW - timedelta(days=1), "t": str(tenant_id)})
    owner_session.commit()

    admin = FakeTelegramAdminClient()
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73), admin_client=admin)
    owner_session.commit()

    facts = owner_session.execute(
        sql_text("SELECT facts FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    assert facts["opted_out"] is False               # nothing happened inside
    assert facts["opted_out_before_window"] is True  # and this is why
    assert "وكان موقفًا للرسائل داخل المهلة" not in admin.messages[0]
    assert "ودخل المهلة وهو موقف للرسائل من قبلها" in admin.messages[0]


def test_a_refunded_customer_is_not_offered_a_refund_a_second_time(
    owner_session: Session, clean_billing: None,
) -> None:
    """The breach is still recorded — it really did break — but «الخيار
    للعميل: استرداد كامل» printed over an account whose money has already gone
    back is an invitation to refund the same order twice."""
    _p, tenant_id, sub_id = _customer(owner_session)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'REFUNDED' WHERE id = :id"),
        {"id": str(sub_id)})
    owner_session.commit()
    admin = FakeTelegramAdminClient()

    counts = guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73), admin_client=admin)
    owner_session.commit()

    assert counts["breached"] == 1           # the record stays honest
    assert "منتهٍ ماليًا" in admin.messages[0]
    assert "استرداد كامل أو تمديد المدة" not in admin.messages[0]


def test_the_breach_screen_reads_the_money_status_live_not_frozen(
    owner_session: Session, clean_billing: None,
) -> None:
    """The facts are frozen because the window is over — but the status of the
    money is not a fact about the window. Frozen, it says what the account was
    the night it broke, and the screen that prints it sits days later beside
    the buttons that move money."""
    _p, tenant_id, sub_id = _customer(owner_session)
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73),
        admin_client=FakeTelegramAdminClient())
    owner_session.commit()

    owner_session.execute(sql_text(          # refunded the next morning
        "UPDATE subscriptions SET status = 'REFUNDED' WHERE id = :id"),
        {"id": str(sub_id)})
    owner_session.commit()

    row = [b for b in guarantee.open_breaches(owner_session)
           if b.tenant_id == tenant_id][0]
    assert row.facts["subscription_status"] == "REFUNDED"      # today
    assert row.facts["subscription_status_at_breach"] == "ACTIVE"   # that night
    assert "راجع حالته قبل أي تعويض" in "\n".join(
        console._fact_lines_ar(row.facts))


def test_a_funnel_only_customer_has_no_start_guarantee(
    owner_session: Session, clean_billing: None,
) -> None:
    """«تقييم لمّاح» is a one-shot report with its own refund clause (§٥) —
    no period, no daily search, nothing to guarantee the start of."""
    _p, tenant_id, _s = _customer(owner_session, product="prod_cv")
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=99),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()
    assert owner_session.execute(
        sql_text("SELECT count(*) FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == 0


def test_extension_adds_the_lost_days_and_leaves_a_subscription_event(
    owner_session: Session, clean_billing: None,
) -> None:
    """«نمدد لك المدة» — by the days that went by with nothing delivered,
    which is the store's own principle: «ما نحسب عليك يوم ما اشتغلنا فيه»."""
    _p, tenant_id, sub_id = _customer(owner_session)
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()
    before = owner_session.execute(sql_text(
        "SELECT current_period_end FROM subscriptions WHERE id = :i"),
        {"i": str(sub_id)}).scalar_one()

    result = guarantee.apply_remedy(
        owner_session, tenant_id=tenant_id,
        remedy=guarantee.REMEDY_EXTENSION, now=NOW + timedelta(hours=73),
    )
    owner_session.commit()

    assert result.outcome == "applied" and result.days == 3
    after = owner_session.execute(sql_text(
        "SELECT current_period_end FROM subscriptions WHERE id = :i"),
        {"i": str(sub_id)}).scalar_one()
    assert (after - before).days == 3
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM subscription_events WHERE subscription_id = :i"
        " AND event_type = 'guarantee_extension'"), {"i": str(sub_id)}
    ).scalar_one() == 1
    assert owner_session.execute(sql_text(
        "SELECT status, remedy FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).one() == (guarantee.SETTLED, "extension")


def test_refund_records_the_choice_and_moves_no_money(
    owner_session: Session, clean_billing: None,
) -> None:
    """The money returns «بنفس وسيلة دفعك عبر سلة» — from Salla, by the
    owner's hand. Flipping the subscription here would race the
    `order.refunded` webhook and could switch off a customer mid-remedy."""
    _p, tenant_id, sub_id = _customer(owner_session)
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()

    result = guarantee.apply_remedy(
        owner_session, tenant_id=tenant_id, remedy=guarantee.REMEDY_REFUND,
        now=NOW + timedelta(hours=73),
    )
    owner_session.commit()

    assert result.outcome == "applied" and result.days == 0
    status, end = owner_session.execute(sql_text(
        "SELECT status, current_period_end FROM subscriptions WHERE id = :i"),
        {"i": str(sub_id)}).one()
    assert status == sub_states.ACTIVE          # untouched — Salla decides
    assert end == NOW + timedelta(days=30)
    assert owner_session.execute(sql_text(
        "SELECT remedy FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == "refund"


def test_a_settled_guarantee_cannot_be_remedied_twice(
    owner_session: Session, clean_billing: None,
) -> None:
    _p, tenant_id, _s = _customer(owner_session)
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73),
        admin_client=FakeTelegramAdminClient(),
    )
    guarantee.apply_remedy(owner_session, tenant_id=tenant_id,
                           remedy=guarantee.REMEDY_WAIVED, now=NOW)
    owner_session.commit()
    again = guarantee.apply_remedy(
        owner_session, tenant_id=tenant_id, remedy=guarantee.REMEDY_EXTENSION,
        now=NOW,
    )
    assert again.outcome == "already_settled"


def test_the_operator_can_see_and_settle_a_breach_from_the_console(
    owner_session: Session, clean_billing: None,
) -> None:
    """The whole promise, end to end, through the only surface the owner
    uses: the breach appears on «الوعود المستحقة», the card offers the two
    remedies the page offers, and the tap says what actually changed."""
    _p, tenant_id, sub_id = _customer(owner_session)
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=73),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()
    code = _code(owner_session, tenant_id)

    screen = _tap(owner_session, "v1|promises", now=NOW + timedelta(hours=73))
    assert code in screen and "ضمانات مكسورة" in screen

    card = _tap(owner_session, f"v1|tenant|{code}", now=NOW + timedelta(hours=73))
    assert "انكسر" in card

    answer = _confirm(owner_session, code, "g_extend",
                      now=NOW + timedelta(hours=73))
    assert "مددنا" in answer
    assert owner_session.execute(sql_text(
        "SELECT status FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == guarantee.SETTLED


# ── PROMISE 2 — لمّاح+: «جلسة مسار واحدة متى طلبتها» ────────────────────────


def test_only_lammah_plus_carries_a_career_session(
    owner_session: Session, clean_billing: None,
) -> None:
    _p, tenant_id, _s = _customer(owner_session, product="prod_pro")
    assert career_session.entitled(owner_session, tenant_id) is False
    result = career_session.request_session(
        owner_session, tenant_id=tenant_id, now=NOW,
    )
    assert result.outcome == "not_entitled"
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM career_sessions WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == 0


def test_a_request_is_recorded_and_one_per_period_is_enforced(
    owner_session: Session, clean_billing: None,
) -> None:
    """«واحدة متى طلبتها» — one per subscription period, and the second ask
    is answered honestly instead of quietly opening a second row."""
    _p, tenant_id, sub_id = _customer(owner_session, product="prod_plus")

    first = career_session.request_session(
        owner_session, tenant_id=tenant_id, now=NOW, source="customer",
    )
    owner_session.commit()
    assert first.outcome == "created"

    second = career_session.request_session(
        owner_session, tenant_id=tenant_id, now=NOW + timedelta(hours=1),
    )
    assert second.outcome == "already_open"
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM career_sessions WHERE subscription_id = :s"),
        {"s": str(sub_id)}).scalar_one() == 1


def test_the_catalog_refuses_a_second_live_session_for_one_period(
    owner_session: Session, clean_billing: None,
) -> None:
    """The rule lives in a unique index, not in everybody remembering to
    check — a caller that forgets is refused by the database."""
    from sqlalchemy.exc import IntegrityError

    _p, tenant_id, sub_id = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()
    owner_session.add(CareerSession(
        id=uuid.uuid4(), tenant_id=tenant_id, subscription_id=sub_id,
        status=career_session.REQUESTED, source="operator", requested_at=NOW,
    ))
    try:
        owner_session.commit()
        raise AssertionError("a second live session must not be insertable")
    except IntegrityError:
        owner_session.rollback()


def test_an_unanswered_request_escalates_into_a_ticket_exactly_once(
    owner_session: Session, clean_billing: None,
) -> None:
    """لمّاح+ promises «والرد خلال ٢٤ ساعة». The failure mode of a human
    promise is not refusal, it is silence — so silence is what escalates."""
    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()
    admin = FakeTelegramAdminClient()

    early = career_session.escalate_overdue(
        owner_session, now=NOW + timedelta(hours=23), admin_client=admin,
    )
    owner_session.commit()
    assert early["escalated"] == 0 and admin.messages == []

    for _ in range(2):
        career_session.escalate_overdue(
            owner_session, now=NOW + timedelta(hours=25), admin_client=admin,
        )
        owner_session.commit()

    assert len(admin.messages) == 1
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM support_events WHERE tenant_id = :t AND kind = :k"),
        {"t": str(tenant_id), "k": career_session.OVERDUE_TICKET_KIND}
    ).scalar_one() == 1


# ── the CADENCE of «الرد خلال ٢٤ ساعة», which is most of the promise ────────
#
# The same defect as the guarantee's, on a clock a third as long, which makes
# the arithmetic worse rather than merely similar. `escalate_overdue` rode the
# 11:00 delivery run and nothing else, so a request that crossed twenty-four
# hours at 12:05 was ticketed at 11:00 the NEXT morning: a promise 24 hours
# long, measured up to ~23 hours late — very nearly measured after it had
# already expired. The tests below are about the SCHEDULE and not the
# detection: they drive the sweep at the cadence the system actually runs it
# at, and measure how late the answer is.


def test_a_request_crossing_the_sla_after_the_nightly_waits_a_day_or_an_hour(
    owner_session: Session, clean_billing: None,
) -> None:
    """THE TIMING DEFECT, and its fix, in one body — because the number only
    means something next to the number it replaced.

    Two identical لمّاح+ customers ask for their session at the identical
    instant, chosen so the twenty-four hours run out FIVE MINUTES after the
    delivery run has already swept and gone. One is then swept the way this
    function was swept until 2026-08-07 — once a day, at the hour the unit
    file names, through the same call `cv.daily_run.sweep_promises` makes. The
    other is swept hourly.

    The first waits nearly a full extra day for a ticket about a promise that
    only lasted a day. The second is ticketed inside the hour. The assertion
    is the comparison, so the fix cannot rot back into the defect without this
    test saying which one it became.
    """
    interval = timedelta(seconds=career_session.SLA_SWEEP_INTERVAL_SECONDS)
    hour, minute = _nightly_run_time()
    tonight = datetime(2026, 7, 19, hour, minute, tzinfo=_RIYADH)
    expires = tonight + timedelta(minutes=5)
    requested_at = expires - timedelta(
        hours=career_session.RESPONSE_SLA_HOURS)

    def _asked() -> uuid.UUID:
        _p, tenant_id, _s = _customer(
            owner_session, product="prod_plus",
            activated_at=requested_at - timedelta(hours=1),
        )
        assert career_session.request_session(
            owner_session, tenant_id=tenant_id, now=requested_at,
        ).outcome == "created"
        owner_session.commit()
        return tenant_id

    nightly_only, hourly = _asked(), _asked()

    def _ticketed_at(tenant_id: uuid.UUID, step: timedelta) -> datetime | None:
        """Sweep on `step` for a full day; when did THIS customer get a
        ticket? Every pass goes through the real entry point, commit and all.
        """
        admin = FakeTelegramAdminClient()
        seen = 0
        for n in range(1, int(timedelta(days=1) / step) + 1):
            now = tonight + n * step
            career_session.escalate_overdue_and_commit(
                owner_session, now=now, admin_client=admin)
            found = owner_session.execute(sql_text(
                "SELECT escalated_at FROM career_sessions WHERE tenant_id = :t"),
                {"t": str(tenant_id)}).scalar_one()
            seen += 1
            if found is not None:
                assert seen >= 1
                return now
        return None

    # the nightly runs five minutes early and honestly finds nothing yet
    admin = FakeTelegramAdminClient()
    assert career_session.escalate_overdue_and_commit(
        owner_session, now=tonight, admin_client=admin)["escalated"] == 0
    assert admin.messages == []

    caught_hourly = _ticketed_at(hourly, interval)
    assert caught_hourly is not None, (
        "a day of hourly sweeps never noticed an unanswered لمّاح+ request"
    )
    lateness = caught_hourly - expires
    assert lateness <= timedelta(hours=1), (
        f"the broken SLA was noticed {lateness} after it became true. The "
        "store writes this promise in HOURS and it is only twenty-four of "
        "them long; a measurement coarser than an hour is coarser than the "
        "operator's own reaction time"
    )
    assert lateness <= interval, (
        f"…and it is the sweep interval ({interval}) that bounds it"
    )

    caught_nightly = _ticketed_at(nightly_only, timedelta(days=1))
    assert caught_nightly is not None
    was_late_by = caught_nightly - expires
    assert was_late_by > timedelta(hours=23), (
        "the once-a-day cadence is supposed to be the DEFECT this test names"
    )
    # …and that is 96% of the promise spent not knowing, against 4%.
    assert was_late_by > timedelta(
        hours=career_session.RESPONSE_SLA_HOURS) * 0.95
    assert lateness < timedelta(
        hours=career_session.RESPONSE_SLA_HOURS) * 0.05

    # the RECORD is late by the small amount, not by a day: this stamp and the
    # ticket under it are what the operator's queue sorts by.
    stamped = owner_session.execute(sql_text(
        "SELECT escalated_at FROM career_sessions WHERE tenant_id = :t"),
        {"t": str(hourly)}).scalar_one()
    assert stamped - expires <= interval
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM support_events WHERE tenant_id = :t AND kind = :k"),
        {"t": str(hourly), "k": career_session.OVERDUE_TICKET_KIND}
    ).scalar_one() == 1


def test_twenty_four_sweeps_a_day_raise_one_ticket_and_one_page(
    owner_session: Session, clean_billing: None,
) -> None:
    """The per-day assumption, hunted for and not found.

    Going from one pass a night to twenty-five a day is only safe if nothing
    in `escalate_overdue` counts nights. Nothing does: the select takes
    ``REQUESTED`` rows with ``escalated_at IS NULL``, both branches stamp it,
    and no path anywhere clears it — a one-way edge, not a «once tonight»
    marker. `_is_overdue` compares flat hours with no date arithmetic, and the
    returned counters describe the PASS. This drives a whole day of hourly
    passes over one overdue request and asserts the operator's side of that:
    one ticket, one page, one stamp — and the stamp is the FIRST pass that saw
    it, never refreshed by the twenty-three that followed.
    """
    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()

    admin = FakeTelegramAdminClient()
    interval = timedelta(seconds=career_session.SLA_SWEEP_INTERVAL_SECONDS)
    first_stamp: datetime | None = None
    # from six hours before the SLA expires to eighteen hours after it
    start = NOW + timedelta(hours=career_session.RESPONSE_SLA_HOURS - 6)
    for step in range(24):
        career_session.escalate_overdue_and_commit(
            owner_session, now=start + step * interval, admin_client=admin)
        stamp = owner_session.execute(sql_text(
            "SELECT escalated_at FROM career_sessions WHERE tenant_id = :t"),
            {"t": str(tenant_id)}).scalar_one()
        if stamp is not None and first_stamp is None:
            first_stamp = stamp

    assert first_stamp is not None
    # not stamped early — the six passes before the deadline saw an unbroken
    # promise and left it alone
    assert first_stamp >= NOW + timedelta(
        hours=career_session.RESPONSE_SLA_HOURS)
    assert owner_session.execute(sql_text(
        "SELECT escalated_at FROM career_sessions WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == first_stamp
    assert len([m for m in admin.messages if "📅" in m]) == 1
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM support_events WHERE tenant_id = :t AND kind = :k"),
        {"t": str(tenant_id), "k": career_session.OVERDUE_TICKET_KIND}
    ).scalar_one() == 1


def test_the_nightly_backstop_cannot_re_escalate_what_the_hourly_sweep_did(
    owner_session: Session, clean_billing: None,
) -> None:
    """Two callers, one broken promise: the second must find nothing to do.

    The hourly sweep is the schedule and the nightly is the backstop, kept for
    the hour the worker is not there. The nightly half is driven through the
    REAL path (`cv.daily_run.sweep_promises`) rather than through
    `escalate_overdue` directly, because the guard has to hold for the caller
    that actually exists — including its own commit, and including the fact
    that it sweeps the 72-hour guarantee out of the same session first.
    """
    from career.cv.daily_run import DailyDeps, sweep_promises

    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()

    hourly = FakeTelegramAdminClient()
    overdue_at = NOW + timedelta(hours=25)
    assert career_session.escalate_overdue_and_commit(
        owner_session, now=overdue_at, admin_client=hourly)["escalated"] == 1
    before = owner_session.execute(sql_text(
        "SELECT escalated_at FROM career_sessions WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    assert len([m for m in hourly.messages if "📅" in m]) == 1

    nightly = FakeTelegramAdminClient()
    deps = DailyDeps(storage=None, whatsapp_client=FakeWhatsAppClient(),
                     admin_client=nightly, llm=None)
    counts = sweep_promises(
        owner_session, deps=deps, now=overdue_at + timedelta(hours=20))

    assert counts["escalated"] == 0 and counts["unticketed"] == 0
    assert [m for m in nightly.messages if "📅" in m] == []
    assert owner_session.execute(sql_text(
        "SELECT escalated_at FROM career_sessions WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == before
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM support_events WHERE tenant_id = :t AND kind = :k"),
        {"t": str(tenant_id), "k": career_session.OVERDUE_TICKET_KIND}
    ).scalar_one() == 1


def test_two_sla_sweeps_in_flight_at_once_ticket_one_request_exactly_once(
    owner_engine: Engine, owner_session: Session, clean_billing: None,
) -> None:
    """The race the second caller created, in two real transactions.

    «Escalated once» is a read followed by a write inside ONE transaction —
    ``escalated_at IS NULL``, then ``escalated_at = now`` — with a
    `support_events` INSERT and a page in between. With a single nightly
    caller that was airtight. With an hourly sweep in the worker AND the
    nightly backstop, two passes can read the same unstamped row in the same
    millisecond: one customer, two tickets on the operator's queue, two
    identical pages on the channel whose entire value is that a message on it
    means something new happened.

    NO THREADS, deliberately — `pg_try_advisory_xact_lock` never waits, so the
    interleaving is built by hand and there is no timing window to lose.
    `lock_timeout` is the safety valve and not the subject: without the
    advisory lock the second pass would block on the first one's uncommitted
    UPDATE of `career_sessions`, and a hang is a rotten way for a suite to
    report a defect.

    And the key is the SESSION SLA's own, never the guarantee's: the two
    passes run off the same two schedules and must be able to overlap freely,
    because a 72-hour sweep standing a 24-hour sweep down would be one promise
    silencing another it shares no row with.
    """
    assert (career_session._SLA_SWEEP_LOCK_KEY
            != guarantee._SWEEP_LOCK_KEY), (
        "the two promise sweeps share an advisory lock key. They run from the "
        "same two schedules over different tables, so a shared key lets each "
        "one stand the other down for a reason that has nothing to do with it"
    )

    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()
    overdue_at = NOW + timedelta(hours=25)
    first, second = FakeTelegramAdminClient(), FakeTelegramAdminClient()

    other = Session(owner_engine)
    try:
        # A — the hourly worker sweep. Reads, tickets, pages, uncommitted.
        assert career_session.escalate_overdue(
            other, now=overdue_at, admin_client=first)["escalated"] == 1
        assert len([m for m in first.messages if "📅" in m]) == 1

        # B — the nightly backstop, a millisecond later, in its own process.
        owner_session.execute(sql_text("SET LOCAL lock_timeout = '3s'"))
        assert career_session.escalate_overdue(
            owner_session, now=overdue_at, admin_client=second,
        ) == {"escalated": 0, "unticketed": 0}
        assert second.messages == [], "one broken promise, two pages"

        other.commit()
    finally:
        other.rollback()
        other.close()
    owner_session.rollback()

    # …and once A has committed, B's next pass genuinely has nothing to do:
    # the stand-down deferred no work, it declined to duplicate it.
    assert career_session.escalate_overdue_and_commit(
        owner_session, now=overdue_at + timedelta(hours=1),
        admin_client=second,
    ) == {"escalated": 0, "unticketed": 0}
    assert second.messages == []
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM support_events WHERE tenant_id = :t AND kind = :k"),
        {"t": str(tenant_id), "k": career_session.OVERDUE_TICKET_KIND}
    ).scalar_one() == 1


def test_scheduling_answers_the_customer_and_stops_the_clock(
    owner_session: Session, clean_billing: None,
) -> None:
    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    career_session.mark_scheduled(
        owner_session, tenant_id=tenant_id, now=NOW + timedelta(hours=2),
    )
    owner_session.commit()

    counts = career_session.escalate_overdue(
        owner_session, now=NOW + timedelta(hours=40),
        admin_client=FakeTelegramAdminClient(),
    )
    assert counts["escalated"] == 0
    rows = career_session.open_sessions(
        owner_session, now=NOW + timedelta(hours=40)
    )
    mine = [r for r in rows if r.tenant_id == tenant_id]
    assert mine and mine[0].status == career_session.SCHEDULED
    assert mine[0].overdue is False


def test_a_completed_session_prices_itself_for_the_refund_page(
    owner_session: Session, clean_billing: None,
) -> None:
    """«تُخصم قيمة الخدمات البشرية اللي استلمتها فعليًا (جلسة المسار ١٥٠
    ريالًا)» — a number that used to come from memory."""
    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    career_session.mark_completed(
        owner_session, tenant_id=tenant_id, now=NOW + timedelta(days=1),
    )
    owner_session.commit()

    assert career_session.refund_deduction_sar(
        owner_session, tenant_id=tenant_id
    ) == Decimal("150")
    # and the entitlement is spent for this period
    assert career_session.request_session(
        owner_session, tenant_id=tenant_id, now=NOW + timedelta(days=2),
    ).outcome == "already_used"


def test_the_operator_runs_the_whole_session_loop_from_the_card(
    owner_session: Session, clean_billing: None,
) -> None:
    """The request arrives as a sentence typed to a human («تكتب لي أنا
    مباشرة على نفس المحادثة»), so the button is what turns it into a record
    with a clock on it — and each step answers with what the ledger says."""
    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    code = _code(owner_session, tenant_id)

    assert "سجّلنا طلب جلسة المسار" in _confirm(owner_session, code, "cs_request")
    assert "اتفقتم على موعد" in _confirm(owner_session, code, "cs_scheduled")
    assert "الجلسة تمت" in _confirm(owner_session, code, "cs_completed")

    row = owner_session.execute(sql_text(
        "SELECT status, source, scheduled_at, completed_at FROM career_sessions"
        " WHERE tenant_id = :t"), {"t": str(tenant_id)}).one()
    assert row[0] == career_session.COMPLETED and row[1] == "operator"
    assert row[2] is not None and row[3] is not None


def test_the_pending_session_is_visible_with_its_age(
    owner_session: Session, clean_billing: None,
) -> None:
    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()

    screen = _tap(owner_session, "v1|promises", now=NOW + timedelta(hours=30))
    assert _code(owner_session, tenant_id) in screen
    assert "جلسات مسار معلّقة" in screen
    assert "منذ" in screen and "تجاوز مهلة الرد" in screen


def test_the_refund_deduction_is_scoped_to_the_order_being_refunded(
    owner_session: Session, clean_billing: None,
) -> None:
    """«تُخصم قيمة الخدمات البشرية اللي استلمتها فعليًا» is scoped to the thing
    being refunded, and this counted the customer's whole life: a لمّاح+
    customer in their second period who took the session they paid for each
    time had 300 SAR deducted from a 449 SAR refund. Every one of those
    sessions was already paid for by the period it belonged to."""
    _p, tenant_id, first_sub = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    career_session.mark_completed(
        owner_session, tenant_id=tenant_id, now=NOW + timedelta(days=1))
    owner_session.commit()

    # the period rolls: §16 gives the renewal its own subscription row
    later = NOW + timedelta(days=31)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED' WHERE id = :id"),
        {"id": str(first_sub)})
    renewed = uuid.uuid4()
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, salla_order_id, plan_code,"
        " status, amount_sar, currency, current_period_start,"
        " current_period_end) VALUES (:id, :t, :o, 'executive', 'ACTIVE',"
        " 449.00, 'SAR', :s, :e)"),
        {"id": str(renewed), "t": str(tenant_id), "o": f"ORD-{uuid.uuid4()}",
         "s": later, "e": later + timedelta(days=30)})
    owner_session.commit()

    # the new period grants its own session — «واحدة» is per period
    assert career_session.request_session(
        owner_session, tenant_id=tenant_id, now=later).outcome == "created"
    career_session.mark_completed(
        owner_session, tenant_id=tenant_id, now=later + timedelta(days=1))
    owner_session.commit()

    # two sessions really were received, and the refund of ONE order deducts one
    assert career_session.completed_sessions_count(
        owner_session, tenant_id=tenant_id) == 2
    assert career_session.refund_deduction_sar(
        owner_session, tenant_id=tenant_id) == Decimal("150")
    assert career_session.refund_deduction_sar(
        owner_session, tenant_id=tenant_id, subscription_id=first_sub,
    ) == Decimal("150")


def test_a_renewal_never_hides_a_session_nobody_answered(
    owner_session: Session, clean_billing: None,
) -> None:
    """An entitlement expires with its period; an unanswered promise does not.
    `current_session` is keyed on subscription_id, so a customer who asked, was
    never answered and then RENEWED vanished from their own card at the exact
    moment they paid us again — and the card is what the operator opens when
    that customer writes to him.

    AUDIT 2026-08-06 — and this test asserted only the READ side, which is why
    the gate stayed green over a card that contradicted its own button. The
    read fell back across periods; every write still resolved through
    `_open_or_used(subscription_id=current.id)`, so the card paged the operator
    about an overdue REQUESTED session and the confirm button answered «لا يوجد
    طلب جلسة لهذا العميل — لم نغيّر شيئًا» in the same screen. Both sides are
    asserted here now."""
    _p, tenant_id, first_sub = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()

    later = NOW + timedelta(days=31)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED' WHERE id = :id"),
        {"id": str(first_sub)})
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, salla_order_id, plan_code,"
        " status, amount_sar, currency, current_period_start,"
        " current_period_end) VALUES (:id, :t, :o, 'executive', 'ACTIVE',"
        " 449.00, 'SAR', :s, :e)"),
        {"id": str(uuid.uuid4()), "t": str(tenant_id),
         "o": f"ORD-{uuid.uuid4()}", "s": later, "e": later + timedelta(days=30)})
    owner_session.commit()

    # the read side: the card still pages him about it
    still_owed = career_session.current_session(owner_session, tenant_id=tenant_id)
    assert still_owed is not None
    assert still_owed.status == career_session.REQUESTED
    assert still_owed.subscription_id == first_sub

    # the write side, on the same screen: the button UNDER that card must
    # answer the row the card just named, not «لا يوجد طلب جلسة لهذا العميل»
    code = _code(owner_session, tenant_id)
    reply = _confirm(owner_session, code, "cs_scheduled",
                     now=later + timedelta(hours=1))
    assert "لا يوجد طلب جلسة" not in reply
    assert "اتفقتم على موعد" in reply

    # …and «سجّل طلبًا» must not open a SECOND promise behind the one the
    # card is already showing
    asked_again = career_session.request_session(
        owner_session, tenant_id=tenant_id, now=later,
    )
    # rolled back BEFORE the assertion on purpose: a failing assert would
    # otherwise leave this session holding uncommitted rows, and the
    # `clean_billing` teardown deletes those tenants from a DIFFERENT session
    # — the suite would hang on the lock instead of reporting the failure.
    owner_session.rollback()
    assert asked_again.outcome == "already_open"

    rows = owner_session.execute(sql_text(
        "SELECT status, subscription_id FROM career_sessions WHERE"
        " tenant_id = :t"), {"t": str(tenant_id)}).all()
    assert len(rows) == 1                      # one promise, not two
    assert rows[0][0] == career_session.SCHEDULED
    assert str(rows[0][1]) == str(first_sub)   # the row the operator saw


def test_a_deleted_customers_open_session_is_announced_once_not_nightly(
    owner_session: Session, clean_billing: None,
) -> None:
    """`career_sessions` is RETAINED by a §12 deletion and `customer_channels`
    is not, so «a لمّاح+ customer without a channel cannot have asked» was not
    the impossible row it was written as. Unstamped, it wrote the same ERROR
    line every night forever — and the operator's feed harvests ERROR lines,
    so a permanent one is camouflage for the next real failure."""
    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()
    owner_session.execute(sql_text(
        "DELETE FROM customer_channels WHERE tenant_id = :t"),
        {"t": str(tenant_id)})
    owner_session.commit()

    admin = FakeTelegramAdminClient()
    first = career_session.escalate_overdue(
        owner_session, now=NOW + timedelta(hours=30), admin_client=admin)
    owner_session.commit()
    second = career_session.escalate_overdue(
        owner_session, now=NOW + timedelta(hours=54), admin_client=admin)
    owner_session.commit()

    assert first["unticketed"] == 1 and first["escalated"] == 0
    assert second["unticketed"] == 0          # noticed once, not every night
    assert len(admin.messages) == 1
    assert "ولا نقدر نفتح له تذكرة" in admin.messages[0]
    assert _code(owner_session, tenant_id) in admin.messages[0]


# ── PROMISE 3 — «سعره اليوم مقفول له … ما دام تجديده مستمر» ─────────────────


def test_the_first_purchase_captures_the_price_and_currency(
    owner_session: Session, clean_billing: None,
) -> None:
    _p, tenant_id, sub_id = _customer(owner_session)
    lock = owner_session.execute(sql_text(
        "SELECT plan_code, amount_sar, currency, lapsed_at FROM price_locks"
        " WHERE tenant_id = :t"), {"t": str(tenant_id)}).one()
    assert lock[0] == "professional"
    assert Decimal(lock[1]) == Decimal("199.00")
    assert lock[2] == "SAR" and lock[3] is None


def test_the_analysis_product_never_takes_a_lock(
    owner_session: Session, clean_billing: None,
) -> None:
    """«الكرسي يُحسب على اشتراكَي لمّاح ولمّاح+. التقييم ما يحسب كرسي»."""
    _p, tenant_id, _s = _customer(owner_session, product="prod_cv")
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM price_locks WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == 0


def test_a_founder_renews_at_the_locked_price_after_a_rise(
    owner_session: Session, clean_billing: None,
) -> None:
    """The promise, and the outage its absence was: with the store now at
    249, this same order used to end in AMOUNT_MISMATCH — a TERMINAL webhook
    status nothing re-reads — so the founder paid and received nothing."""
    phone, tenant_id, _s = _customer(owner_session)
    admin = FakeTelegramAdminClient()

    result, _order = _buy(
        owner_session, product="prod_pro", phone=phone,
        pricing=_PRICING_RAISED, amount=Decimal("199.00"),
        now=NOW + timedelta(days=29), admin=admin,
    )

    assert result.status is ProvisionStatus.RENEWED
    assert uuid.UUID(str(result.tenant_id)) == tenant_id
    # and the operator is told, because a renewal below today's price is
    # exactly the shape of a mispriced order
    assert any("مقفول" in m for m in admin.messages)


def test_a_stranger_paying_the_old_price_still_fails_closed(
    owner_session: Session, clean_billing: None,
) -> None:
    """The lock belongs to a tenant, never to an amount. Somebody else's
    locked price is not a discount code."""
    _phone_a, _t, _s = _customer(owner_session)
    stranger = _phone()
    admin = FakeTelegramAdminClient()

    result, _order = _buy(
        owner_session, product="prod_pro", phone=stranger,
        pricing=_PRICING_RAISED, amount=Decimal("199.00"),
        now=NOW + timedelta(days=1), admin=admin,
    )

    assert result.status is ProvisionStatus.AMOUNT_MISMATCH
    assert any("لا تطابق المتوقع" in m for m in admin.messages)


def test_an_arbitrary_amount_from_the_locked_customer_still_fails_closed(
    owner_session: Session, clean_billing: None,
) -> None:
    """The relaxation admits ONE exact amount. A tampered figure — even a
    plausible one, even from the right phone — is the case the guard exists
    for."""
    phone, _t, _s = _customer(owner_session)
    result, _order = _buy(
        owner_session, product="prod_pro", phone=phone,
        pricing=_PRICING_RAISED, amount=Decimal("150.00"),
        now=NOW + timedelta(days=29),
    )
    assert result.status is ProvisionStatus.AMOUNT_MISMATCH


def test_a_lock_never_admits_more_than_todays_price(
    owner_session: Session, clean_billing: None,
) -> None:
    """A lock protects against a RISE. If the store gets cheaper, honouring
    the old higher amount would take more money than the page asks for
    today — nothing promises that."""
    phone, tenant_id, _s = _customer(owner_session)
    cheaper = {"prod_pro": (Decimal("99.00"), "SAR")}
    result, _order = _buy(
        owner_session, product="prod_pro", phone=phone, pricing=cheaper,
        amount=Decimal("199.00"), now=NOW + timedelta(days=29),
    )
    assert result.status is ProvisionStatus.AMOUNT_MISMATCH


def test_renewing_inside_the_published_seven_days_keeps_the_lock(
    owner_session: Session, clean_billing: None,
) -> None:
    """«تجدد قبل نهاية اشتراكك أو خلال ٧ أيام بعده» — six days after the
    period ended is still «تجديد مستمر»."""
    phone, tenant_id, _s = _customer(owner_session)
    result, _order = _buy(
        owner_session, product="prod_pro", phone=phone,
        pricing=_PRICING_RAISED, amount=Decimal("199.00"),
        now=NOW + timedelta(days=36),
    )
    assert result.status is ProvisionStatus.RENEWED


def test_a_longer_gap_lapses_the_lock_and_the_order_fails_closed(
    owner_session: Session, clean_billing: None,
) -> None:
    """«انقطعت أكثر؟ الكرسي ينفتح لغيرك، وترجع بسعر يومها» — and the operator
    is told the real reason instead of being sent to fix a price that is
    already correct."""
    phone, tenant_id, _s = _customer(owner_session)
    admin = FakeTelegramAdminClient()

    result, _order = _buy(
        owner_session, product="prod_pro", phone=phone,
        pricing=_PRICING_RAISED, amount=Decimal("199.00"),
        now=NOW + timedelta(days=38), admin=admin,
    )

    assert result.status is ProvisionStatus.AMOUNT_MISMATCH
    assert any("سقط قفل سعره" in m for m in admin.messages)
    lapsed = owner_session.execute(sql_text(
        "SELECT lapsed_at FROM price_locks WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    assert lapsed is not None


def test_a_lapsed_founder_comes_back_at_todays_price(
    owner_session: Session, clean_billing: None,
) -> None:
    """The other half of «وترجع بسعر يومها»: they are still welcome, and the
    lapse must not lock them out of buying at the CURRENT price."""
    phone, tenant_id, _s = _customer(owner_session)
    _lapse, _o = _buy(
        owner_session, product="prod_pro", phone=phone,
        pricing=_PRICING_RAISED, amount=Decimal("199.00"),
        now=NOW + timedelta(days=38),
    )
    result, _order = _buy(
        owner_session, product="prod_pro", phone=phone,
        pricing=_PRICING_RAISED, amount=Decimal("249.00"),
        now=NOW + timedelta(days=39),
    )
    assert result.status is ProvisionStatus.RENEWED


def test_an_upgrade_locks_the_new_pass_at_what_they_actually_paid(
    owner_session: Session, clean_billing: None,
) -> None:
    """The promise is about the price of the thing they bought, so a customer
    moving to لمّاح+ gets a lock for لمّاح+ — at 449, not at 199."""
    phone, tenant_id, _s = _customer(owner_session)
    _result, _order = _buy(
        owner_session, product="prod_plus", phone=phone,
        amount=Decimal("449.00"), now=NOW + timedelta(days=10),
    )
    locks = {
        plan: Decimal(amount) for plan, amount in owner_session.execute(
            sql_text("SELECT plan_code, amount_sar FROM price_locks"
                     " WHERE tenant_id = :t"), {"t": str(tenant_id)}).all()
    }
    assert locks == {"professional": Decimal("199.00"),
                     "executive": Decimal("449.00")}


def test_the_card_shows_the_founder_their_locked_price(
    owner_session: Session, clean_billing: None,
) -> None:
    _p, tenant_id, _s = _customer(owner_session)
    card = _tap(owner_session, f"v1|tenant|{_code(owner_session, tenant_id)}")
    assert "سعره مقفول" in card and "199.00 SAR" in card


def test_a_price_lock_is_never_captured_from_an_unverified_amount(
    owner_session: Session, clean_billing: None,
) -> None:
    """Capture happens only after the triple match has agreed the amount is
    the store's real price — that ordering is the whole basis for trusting
    the lock later."""
    admin = FakeTelegramAdminClient()
    result, _order = _buy(
        owner_session, product="prod_pro", phone=_phone(),
        amount=Decimal("1.00"), admin=admin,
    )
    assert result.status is ProvisionStatus.AMOUNT_MISMATCH
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM price_locks WHERE amount_sar = 1.00")
    ).scalar_one() == 0


# ── the price-test window: the store is knowingly cheap for a few days ──────
#
# Fahad proves the Salla payment gateway with real money by dropping the THREE
# REAL products to 1.00 SAR, letting a handful of people he knows buy, and
# restoring the prices. Every automatic check agrees that configuration is
# correct, because it is: the store, the pricing map and the §09 triple match
# are all reading the same decision. The one thing none of them can see is
# that the decision is a rehearsal — so `capture` would record 1.00 SAR as
# those buyers' FOUNDING price, idempotently and forever.
#
# `SALLA_PRICE_TEST_UNTIL` is the operator's declaration that this is what is
# happening. The tests below are its two halves: while it is open no lock is
# captured and the refusal is written down, and the moment it lapses the
# promise works exactly as it did before the variable existed.

#: The same three real products with the test price on them — what the store
#: and `SALLA_PRODUCT_PRICING` BOTH say during the window.
_PRICING_TEST = {"prod_pro": (Decimal("1.00"), "SAR"),
                 "prod_plus": (Decimal("1.00"), "SAR"),
                 "prod_cv": (Decimal("1.00"), "SAR")}


@contextmanager
def _window(value: str) -> Any:
    """Open (or close) the price-test window for the code under test."""
    from career.config import get_settings

    previous = os.environ.get("SALLA_PRICE_TEST_UNTIL")
    os.environ["SALLA_PRICE_TEST_UNTIL"] = value
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SALLA_PRICE_TEST_UNTIL", None)
        else:
            os.environ["SALLA_PRICE_TEST_UNTIL"] = previous
        get_settings.cache_clear()


def _events(session: Session, tenant_id: uuid.UUID, kind: str) -> list[Any]:
    return session.execute(sql_text(
        "SELECT details FROM subscription_events WHERE tenant_id = :t"
        " AND event_type = :k"), {"t": str(tenant_id), "k": kind}).all()


def test_the_test_window_stops_the_lock_and_says_so(
    owner_session: Session, clean_billing: None,
) -> None:
    """THE GUARD. A one-riyal purchase of the REAL 199 product provisions
    normally — the money is real and the buyer must be served — and records no
    founding price at all.

    Without this, that row is permanent: capture is idempotent per (tenant,
    plan), so the same tester paying the real 199 later changes nothing, and
    the 1.00 lock becomes both a standing authorisation to buy the pass for one
    riyal and the reason his next real payment dies TERMINAL.
    """
    with _window("2026-07-16"):                 # NOW is the 15th in Riyadh
        result, _order = _buy(
            owner_session, product="prod_pro", phone=_phone(),
            pricing=_PRICING_TEST, amount=Decimal("1.00"),
        )

    assert result.status is ProvisionStatus.PROVISIONED
    tenant_id = uuid.UUID(str(result.tenant_id))
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM price_locks WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == 0
    # the assertion the older test makes about the whole table now holds by
    # construction rather than by the amount happening to be refused
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM price_locks WHERE amount_sar = 1.00")
    ).scalar_one() == 0
    # …and it is NOT a silent no-op: a silent one is how the original defect
    # hid, since a wrong lock and a missing lock look identical from outside.
    refusals = _events(owner_session, tenant_id, "price_lock_refused")
    assert len(refusals) == 1
    assert refusals[0][0]["amount"] == "1.00"
    assert refusals[0][0]["window_until"] == "2026-07-16"


def test_with_no_window_configured_nothing_changes_at_all(
    owner_session: Session, clean_billing: None,
) -> None:
    """The default is «no window». A host that has never heard of the variable
    captures locks exactly as it did before it existed — the guard must be the
    operator's deliberate act, never a new silence everyone inherits."""
    _p, tenant_id, _s = _customer(owner_session)
    assert owner_session.execute(sql_text(
        "SELECT amount_sar FROM price_locks WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == Decimal("199.00")
    assert _events(owner_session, tenant_id, "price_lock_refused") == []


def test_the_window_closes_itself_the_day_after_it_passes(
    owner_session: Session, clean_billing: None,
) -> None:
    """A DATE and not a flag: forgetting to clear it cannot keep the promise
    switched off forever. The day after the last day, capture is armed again
    with nobody's help."""
    with _window("2026-07-14"):                 # yesterday, in Riyadh terms
        result, _order = _buy(
            owner_session, product="prod_pro", phone=_phone(),
            pricing=_PRICING, amount=Decimal("199.00"),
        )
    tenant_id = uuid.UUID(str(result.tenant_id))
    assert owner_session.execute(sql_text(
        "SELECT amount_sar FROM price_locks WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == Decimal("199.00")


def test_an_unreadable_window_suppresses_instead_of_arming(
    owner_session: Session, clean_billing: None,
) -> None:
    """The two ways to be wrong are not symmetric. A lock not captured costs
    nothing today; a lock captured at a test price is permanent money. So a
    date nobody can parse is treated as an OPEN window — and the boot check
    reports it every morning until it is fixed or cleared."""
    with _window("next friday"):
        result, _order = _buy(
            owner_session, product="prod_pro", phone=_phone(),
            pricing=_PRICING_TEST, amount=Decimal("1.00"),
        )
    tenant_id = uuid.UUID(str(result.tenant_id))
    assert result.status is ProvisionStatus.PROVISIONED
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM price_locks WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == 0
    assert len(_events(owner_session, tenant_id, "price_lock_refused")) == 1


def test_the_tester_is_owed_nothing_and_the_code_gives_him_it(
    owner_session: Session, clean_billing: None,
) -> None:
    """A tester who buys during the window and renews after it closes.

    One riyal was never his founding price, so no lock is owed for it — and
    nothing about the renewal needs one: a lock is only ever consulted when
    the paid amount does NOT match today's price, and his renewal matches it
    exactly. He is provisioned by the ordinary §09 triple match, and THAT
    purchase is the first real price he has paid, so it is where his founding
    price is finally recorded.
    """
    phone = _phone()
    with _window("2026-07-16"):
        first, _o = _buy(owner_session, product="prod_pro", phone=phone,
                         pricing=_PRICING_TEST, amount=Decimal("1.00"))
    tenant_id = uuid.UUID(str(first.tenant_id))
    activate(owner_session, token=first.activation_token, from_phone=phone,
             display_name=None, now=NOW, whatsapp_client=FakeWhatsAppClient(),
             admin_client=FakeTelegramAdminClient())
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'ACTIVE', current_period_start = :s,"
        " current_period_end = :e WHERE tenant_id = :t"),
        {"s": NOW, "e": NOW + timedelta(days=30), "t": str(tenant_id)})
    owner_session.commit()

    # the window has closed and the store is back at 199
    later, _o2 = _buy(owner_session, product="prod_pro", phone=phone,
                      pricing=_PRICING, amount=Decimal("199.00"),
                      now=NOW + timedelta(days=25))

    assert later.status is ProvisionStatus.RENEWED
    assert owner_session.execute(sql_text(
        "SELECT amount_sar FROM price_locks WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == Decimal("199.00")


def test_a_real_founders_lock_survives_the_window_untouched(
    owner_session: Session, clean_billing: None,
) -> None:
    """The window suppresses CAPTURE and nothing else. A founder who happens to
    renew while the store is cheap keeps the price he was promised, and his
    renewal is not written down as a refusal — nothing was going to be
    captured for him in the first place."""
    phone, tenant_id, _s = _customer(owner_session)
    with _window("2026-08-20"):
        result, _o = _buy(owner_session, product="prod_pro", phone=phone,
                          pricing=_PRICING_TEST, amount=Decimal("1.00"),
                          now=NOW + timedelta(days=29))
    assert result.status is ProvisionStatus.RENEWED
    assert owner_session.execute(sql_text(
        "SELECT amount_sar FROM price_locks WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == Decimal("199.00")
    assert _events(owner_session, tenant_id, "price_lock_refused") == []


def test_the_window_is_read_on_the_orders_own_clock(
    owner_session: Session, clean_billing: None,
) -> None:
    """A webhook replayed or swept after the window closed is still judged by
    the day the money was taken — otherwise the trap arms on exactly the
    orders that are slowest to be processed."""
    with _window("2026-07-16"):
        inside, _o = _buy(owner_session, product="prod_pro", phone=_phone(),
                          pricing=_PRICING_TEST, amount=Decimal("1.00"),
                          now=datetime(2026, 7, 16, 20, 0, tzinfo=UTC))
        after, _o2 = _buy(owner_session, product="prod_pro", phone=_phone(),
                          pricing=_PRICING_TEST, amount=Decimal("1.00"),
                          now=datetime(2026, 7, 17, 6, 0, tzinfo=UTC))
    # 20:00 UTC on the 16th is 23:00 in Riyadh — the last hours of the window
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM price_locks WHERE tenant_id = :t"),
        {"t": str(inside.tenant_id)}).scalar_one() == 0
    # …and the morning after it, capture is live again
    assert owner_session.execute(sql_text(
        "SELECT amount_sar FROM price_locks WHERE tenant_id = :t"),
        {"t": str(after.tenant_id)}).scalar_one() == Decimal("1.00")


# ── the sweeps are wired to something that actually runs ────────────────────


def test_the_nightly_orchestrator_sweeps_both_clocks_even_on_the_weekend(
    owner_session: Session, clean_billing: None,
) -> None:
    """Both promises are written in flat hours while §08 delivers Sunday to
    Thursday, so the sweep sits ABOVE the weekend early return: a guarantee
    that runs out on a Friday is caught on Friday, not on Sunday with the
    operator two days late to a conversation about money."""
    from career.cv.daily_run import DailyDeps, run_daily_delivery
    from career.engine.run import RunReport

    _p, tenant_id, _s = _customer(owner_session)
    admin = FakeTelegramAdminClient()
    deps = DailyDeps(storage=None, whatsapp_client=FakeWhatsAppClient(),
                     admin_client=admin, llm=None)
    friday = datetime(2026, 7, 17, 4, 30, tzinfo=UTC)

    states = run_daily_delivery(
        owner_session,
        report=RunReport(run_id=uuid.uuid4(), status="completed",
                         per_tenant={}, counts={}),
        deps=deps, now=friday + timedelta(days=7), suppressor=lambda **k: None,
    )

    assert states == {}                              # still no weekend day
    assert any("ضمان" in m for m in admin.messages)  # but the clock was read
    assert owner_session.execute(sql_text(
        "SELECT status FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == guarantee.BREACHED


def test_the_guarantee_row_is_unique_per_customer(
    owner_session: Session, clean_billing: None,
) -> None:
    """«ضمان البداية» — a customer begins once, so a renewal must never
    restart the clock and manufacture a breach for somebody who has been
    served for months."""
    from sqlalchemy.exc import IntegrityError

    _p, tenant_id, sub_id = _customer(owner_session)
    guarantee.sweep_delivery_guarantee(
        owner_session, now=NOW + timedelta(hours=1),
        admin_client=FakeTelegramAdminClient(),
    )
    owner_session.commit()
    owner_session.add(DeliveryGuarantee(
        id=uuid.uuid4(), tenant_id=tenant_id, subscription_id=sub_id,
        activated_at=NOW, deadline_at=NOW + timedelta(hours=72),
        status=guarantee.WATCHING,
    ))
    try:
        owner_session.commit()
        raise AssertionError("a second guarantee row must not be insertable")
    except IntegrityError:
        owner_session.rollback()


def test_price_locks_are_tenant_isolated_like_every_other_ledger(
    owner_session: Session, clean_billing: None,
) -> None:
    """§15.10: a table carrying tenant_id is FORCE-RLS isolated, policy and
    all — the meta-test guards the catalog, this guards the three new tables
    by name so a future migration cannot quietly exempt one."""
    rows = owner_session.execute(sql_text(
        "SELECT relname FROM pg_class WHERE relname = ANY(:t)"
        " AND relrowsecurity AND relforcerowsecurity"),
        {"t": ["price_locks", "career_sessions", "delivery_guarantees"]}
    ).scalars().all()
    assert sorted(rows) == ["career_sessions", "delivery_guarantees",
                            "price_locks"]
    policies = owner_session.execute(sql_text(
        "SELECT tablename FROM pg_policies WHERE tablename = ANY(:t)"
        " AND qual LIKE '%tenant_id%'"),
        {"t": ["price_locks", "career_sessions", "delivery_guarantees"]}
    ).scalars().all()
    assert sorted(policies) == ["career_sessions", "delivery_guarantees",
                                "price_locks"]


def test_the_locked_price_never_reaches_the_admin_channel_with_a_phone(
    owner_session: Session, clean_billing: None,
) -> None:
    """§15.13 on the one new alert that carries money: TEN codes only."""
    phone, _t, _s = _customer(owner_session)
    admin = FakeTelegramAdminClient()
    _buy(owner_session, product="prod_pro", phone=phone,
         pricing=_PRICING_RAISED, amount=Decimal("199.00"),
         now=NOW + timedelta(days=29), admin=admin)
    assert admin.messages
    for message in admin.messages:
        assert phone not in message
        assert phone.lstrip("+") not in message


# ── the opt-out invariant, made structural ───────────────────────────────────
#
# `_silence_inside` answers «did he silence us inside the 72-hour window?» from
# `inbound_messages.classification`, because the column `customer_channels
# .opt_out_at` remembers only the LATEST flip: a customer who stopped at hour
# one and resumed at hour seventy-four read as «never opted out» for a window
# he was silent through, and the fact most likely to explain the breach was
# missing from the packet the operator refunds on.
#
# That rewrite is correct TODAY for a reason nothing enforces: there happen to
# be exactly two assignments to `opt_out_at` in the tree, both in
# `whatsapp/worker._handle_message`, each standing beside the `_record_inbound`
# call that writes its matching classified row, both inside one transaction and
# one commit. A third assignment written anywhere else — an operator command in
# the console, a compliance repair script, an unsubscribe link — silences a
# customer that `inbound_messages` never hears about, and the guarantee goes
# back to being confidently wrong in exactly the direction that costs money.
#
# So the pair is enforced instead of observed. The technique is the one
# `test_cv_close` uses for the single writer of `tenant_day_states` and
# `test_db_engine_guard` uses for `create_engine`: parse the tree, find every
# shape of the write, and prove on synthetic violations that the finder sees
# them.

_REPO = pathlib.Path(__file__).resolve().parents[1]

#: Scanned in full — `src` and not `src/career`, so `src/career_core` is
#: covered the day it grows a database surface. `scripts/` is deliberately
#: absent and it is a real residual: a one-shot repair script that flips the
#: column by hand is outside this guard. Naming it beats pretending.
_SCAN_ROOTS = ("src",)

#: Taken from the code under test, never typed here. If somebody renames
#: `guarantee._STOP`, the guard renames with it instead of silently enforcing
#: a spelling the query no longer looks for.
_STOP = guarantee._STOP
_RESUME = guarantee._RESUME

#: Flips that genuinely have no inbound message behind them, with the reason.
#: Empty on purpose: today there is no such thing, and the entry that arrives
#: has to be argued for in writing rather than added to make a red test green.
#: Ratcheted in both directions by
#: `test_the_opt_out_escape_list_still_earns_its_place`.
_FLIP_WITHOUT_AN_INBOUND: dict[tuple[str, str, str], str] = {}

#: The pair as it exists today, asserted so the guard cannot end up guarding
#: air on the day the flips move or are deleted.
_AUTHORITY = "src/career/whatsapp/worker.py"


def _classified_rows_in(node: ast.AST) -> set[str]:
    """Every classification LITERAL this scope writes onto an inbound row.

    Three shapes, because the row is built in three plausible ways: a keyword
    (`_record_inbound(..., classification="stop")`, which is how the tree does
    it, and `InboundMessage(classification="stop")`, which is how the next
    author might), an attribute assignment on a row already in hand, and a
    mapping literal handed to a bulk insert.

    A literal is REQUIRED. `classification=kind` — a variable — reads as no
    classification at all, and the guard says so rather than guessing: a value
    it cannot see is a value it cannot prove matches the flip beside it.
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.keyword) and child.arg == "classification":
            if isinstance(child.value, ast.Constant) and isinstance(
                child.value.value, str
            ):
                found.add(child.value.value)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "classification"
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)
                ):
                    found.add(child.value.value)
        elif isinstance(child, ast.Dict):
            for key, value in zip(child.keys, child.values, strict=True):
                if (
                    isinstance(key, ast.Constant) and key.value == "classification"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)
    return found


def _flip_kind(value: ast.AST | None) -> str:
    """Setting the column silences; clearing it is the way back."""
    if isinstance(value, ast.Constant) and value.value is None:
        return _RESUME
    return _STOP


#: A raw-SQL write of the column, however it is quoted or spaced. The table
#: name comes off the model for the same reason the classifications do.
def _sql_write(table: str) -> re.Pattern[str]:
    return re.compile(
        r"(insert\s+into|update|delete\s+from)\s+[\"']?" + re.escape(table)
        + r"[\s\S]*opt_out_at",
        re.IGNORECASE,
    )


def _flip_sites(
    paths: list[pathlib.Path], root: pathlib.Path
) -> set[tuple[str, str, str, str]]:
    """Every place in `paths` that WRITES `opt_out_at`, and what it wrote.

    Returns ``(file, scope, flip, paired)`` where ``flip`` is `_STOP` or
    `_RESUME` and ``paired`` is the classification recorded in the same
    function — or ``"<none>"``. The scope is qualified through nested classes
    and functions, so a flip hidden in a closure is still named and still has
    to satisfy the rule (`ast.walk` alone would lose which function it was in,
    which IS the rule here).
    """
    from career.db.models import CustomerChannel

    column = "opt_out_at"
    sql_write = _sql_write(CustomerChannel.__tablename__)
    found: set[tuple[str, str, str, str]] = set()

    for path in paths:
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        def record(
            scope: list[str], flip: str, paired: set[str], *, rel: str = rel
        ) -> None:
            found.add((
                rel, ".".join(scope) or "<module>", flip,
                ",".join(sorted(paired)) or "<none>",
            ))

        def check(node: ast.AST, scope: list[str], paired: set[str]) -> None:
            if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == column:
                        record(scope, _flip_kind(node.value), paired)
            if isinstance(node, ast.Call):
                verb = (
                    node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None)
                )
                if verb == "setattr" and len(node.args) >= 3:
                    name = node.args[1]
                    if isinstance(name, ast.Constant) and name.value == column:
                        record(scope, _flip_kind(node.args[2]), paired)
                # `CustomerChannel(opt_out_at=…)`, and the Core forms:
                # `update(CustomerChannel).values(opt_out_at=…)`,
                # `insert(...).values(...)`, `session.execute(stmt, {...})`.
                constructs = (
                    (isinstance(node.func, ast.Name)
                     and node.func.id == "CustomerChannel")
                    or (isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"CustomerChannel", "values"})
                )
                if constructs:
                    for kw in node.keywords:
                        if kw.arg == column:
                            record(scope, _flip_kind(kw.value), paired)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if sql_write.search(node.value):
                    flip = (
                        _RESUME
                        if re.search(r"opt_out_at\s*=\s*NULL", node.value, re.I)
                        else _STOP
                    )
                    record(scope, flip, paired)

        def descend(node: ast.AST, scope: list[str], paired: set[str]) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    descend(child, [*scope, child.name], _classified_rows_in(child))
                elif isinstance(child, ast.ClassDef):
                    descend(child, [*scope, child.name], paired)
                else:
                    check(child, scope, paired)
                    descend(child, scope, paired)

        descend(tree, [], set())
    return found


def _production_flip_sites() -> set[tuple[str, str, str, str]]:
    files = [
        p
        for root in _SCAN_ROOTS
        for p in sorted((_REPO / root).rglob("*.py"))
        if "__pycache__" not in p.parts
    ]
    return _flip_sites(files, _REPO)


def _unpaired(
    sites: set[tuple[str, str, str, str]]
) -> list[tuple[str, str, str, str]]:
    return sorted(
        site for site in sites
        if site[2] not in set(site[3].split(","))
        and (site[0], site[1], site[2]) not in _FLIP_WITHOUT_AN_INBOUND
    )


def test_every_opt_out_flip_records_the_inbound_row_that_caused_it() -> None:
    """The invariant `_silence_inside` rests on, enforced instead of observed.

    `opt_out_at` is a switch that remembers one flip; `inbound_messages` is the
    history the guarantee actually reads. Writing the switch without writing
    the history makes a customer's silence invisible to the promise — and the
    promise is «استرداد كامل أو تمديد»: real money, decided weeks later, from a
    packet that would simply not contain the reason.
    """
    offenders = _unpaired(_production_flip_sites())
    assert not offenders, (
        "opt_out_at is flipped without the inbound row that makes the flip "
        f"visible to the start guarantee: {offenders}\n"
        "\n"
        "WHAT TO DO: write the classified row in the same function, in the "
        "same transaction, the way whatsapp/worker._handle_message does —\n"
        "    channel.opt_out_at = now\n"
        '    _record_inbound(session, ..., classification="stop", ...)\n'
        "and `channel.opt_out_at = None` beside classification=\"resume\". The "
        "classification must be a LITERAL: this guard cannot read a variable, "
        "and will not assume one matches.\n"
        "\n"
        "WHY: promises/guarantee._silence_inside answers «did he silence us "
        "inside the 72-hour window?» from inbound_messages, NOT from this "
        "column — the column keeps only the LAST flip, so a stop at hour 1 "
        "followed by a resume at hour 74 reads as «never opted out» and the "
        "operator decides a refund without the one fact that explains the "
        "breach.\n"
        "\n"
        "IF THERE REALLY IS NO INBOUND MESSAGE behind this flip (an operator "
        "action, a data repair), then it is a silence the guarantee can never "
        "explain: add it to _FLIP_WITHOUT_AN_INBOUND with the reason, and "
        "expect to be asked why the customer's own history does not show it."
    )


def test_the_two_flips_the_guarantee_was_written_around_are_still_there() -> None:
    """The other direction: the guard above passes perfectly if nobody flips
    the column at all — including on the day the stop/resume handling is moved
    or deleted, which is exactly when it should speak up."""
    sites = _production_flip_sites()
    authority = {
        (flip, paired) for path, _scope, flip, paired in sites if path == _AUTHORITY
    }
    assert any(flip == _STOP and _STOP in paired.split(",")
               for flip, paired in authority), (
        f"nothing in {_AUTHORITY} silences a customer any more — if the opt-out "
        "moved, move _AUTHORITY with it; if it was deleted, this guard now "
        "guards air"
    )
    assert any(flip == _RESUME and _RESUME in paired.split(",")
               for flip, paired in authority), (
        f"nothing in {_AUTHORITY} clears the silence any more — the way BACK "
        "is the half that has already gone missing once (whatsapp/worker's own "
        "note: «nothing anywhere cleared opt_out_at»)"
    )


def test_the_classifications_the_guard_enforces_are_the_ones_stored() -> None:
    """The chain has three links and this is the one an AST cannot see: the
    literals the ratchet demands must be the literals `whatsapp/inbound`
    produces AND the ones `_silence_inside` filters on. Two of the three agree
    by construction here; the third is asserted."""
    from career.whatsapp.inbound import InboundKind

    assert _STOP == InboundKind.STOP.value
    assert _RESUME == InboundKind.RESUME.value


def test_the_opt_out_escape_list_still_earns_its_place() -> None:
    """An allow-list nobody re-checks stops being an exception and becomes
    permission (the `_PRICE_HISTORY` lesson, learned the same week)."""
    sites = {(f, scope, flip) for f, scope, flip, _paired in _production_flip_sites()}
    stale = sorted(set(_FLIP_WITHOUT_AN_INBOUND) - sites)
    assert not stale, (
        f"these flips no longer exist: {stale} — delete them from "
        "_FLIP_WITHOUT_AN_INBOUND so the guard covers those sites again"
    )


#: One synthetic module per way the pair can come apart, written the way a real
#: author would write it — hurried, not malicious. Kept here rather than tried
#: once by hand, because «somebody checked in August» is not a property the
#: next edit of the guard preserves.
_UNPAIRED_SHAPES: tuple[tuple[str, str], ...] = (
    (
        "a plain assignment in another module",
        "def unsubscribe(session, channel, now):\n"
        "    channel.opt_out_at = now\n",
    ),
    (
        "a clear with no resume row — the way back, silently",
        "def resume(session, channel):\n"
        "    channel.opt_out_at = None\n",
    ),
    (
        "the WRONG row beside the flip",
        "def unsubscribe(session, channel, now):\n"
        "    channel.opt_out_at = now\n"
        "    _record_inbound(session, classification='resume')\n",
    ),
    (
        "a classification the guard cannot read",
        "def flip(session, channel, now, kind):\n"
        "    channel.opt_out_at = now\n"
        "    _record_inbound(session, classification=kind)\n",
    ),
    (
        "setattr instead of an assignment",
        "def unsubscribe(session, channel, now):\n"
        "    setattr(channel, 'opt_out_at', now)\n",
    ),
    (
        "SQLAlchemy Core — update(CustomerChannel).values(...)",
        "from sqlalchemy import update\n"
        "from career.db.models import CustomerChannel\n"
        "def unsubscribe(session, now):\n"
        "    session.execute(update(CustomerChannel).values(opt_out_at=now))\n",
    ),
    (
        "raw SQL that never mentions the class",
        "from sqlalchemy import text\n"
        "def unsubscribe(session):\n"
        "    session.execute(text(\n"
        "        \"UPDATE customer_channels SET opt_out_at = now()\"))\n",
    ),
    (
        "raw SQL clearing it — a bulk un-silencing nobody can audit",
        "from sqlalchemy import text\n"
        "def resume_everyone(session):\n"
        "    session.execute(text(\n"
        "        \"UPDATE customer_channels SET opt_out_at = NULL\"))\n",
    ),
    (
        "a channel born silenced",
        "from career.db.models import CustomerChannel\n"
        "def seed(session, now):\n"
        "    session.add(CustomerChannel(opt_out_at=now))\n",
    ),
    (
        "an async writer",
        "async def unsubscribe(session, channel, now):\n"
        "    channel.opt_out_at = now\n",
    ),
    (
        "a flip hidden inside a closure",
        "def outer(channel, now):\n"
        "    def inner():\n"
        "        channel.opt_out_at = now\n"
        "    return inner\n",
    ),
    (
        "a flip on a class method",
        "class Compliance:\n"
        "    async def silence(self, channel, now):\n"
        "        channel.opt_out_at = now\n",
    ),
    (
        "the pair split across two functions",
        "def silence(channel, now):\n"
        "    channel.opt_out_at = now\n"
        "def audit(session):\n"
        "    _record_inbound(session, classification='stop')\n",
    ),
    (
        "a flip at module scope",
        "channel = get_channel()\n"
        "channel.opt_out_at = None\n",
    ),
)


@pytest.mark.parametrize(("label", "source"), _UNPAIRED_SHAPES, ids=lambda v: v[:44])
def test_each_way_the_pair_comes_apart_is_seen(
    tmp_path: pathlib.Path, label: str, source: str
) -> None:
    module = tmp_path / "second_flipper.py"
    module.write_text(source, encoding="utf-8")
    assert _unpaired(_flip_sites([module], tmp_path)), (
        f"the opt-out pairing guard does not see: {label}"
    )


def test_the_guard_accepts_the_pair_written_correctly(tmp_path: pathlib.Path) -> None:
    """The other half of «does it work». A guard that flags the correct code
    too is deleted by the first author it inconveniences, and then nothing is
    guarded at all. Both flips, both rows, one function — the shape
    `whatsapp/worker._handle_message` already has."""
    module = tmp_path / "correct.py"
    module.write_text(
        "def handle(session, channel, msg, now, kind):\n"
        "    if kind == 'stop':\n"
        "        channel.opt_out_at = now\n"
        "        _record_inbound(session, channel_id=channel.id,\n"
        "                        classification='stop', now=now)\n"
        "    elif kind == 'resume':\n"
        "        channel.opt_out_at = None\n"
        "        _record_inbound(session, channel_id=channel.id,\n"
        "                        classification='resume', now=now)\n",
        encoding="utf-8",
    )
    assert _unpaired(_flip_sites([module], tmp_path)) == []


def test_the_guard_does_not_flag_a_reader(tmp_path: pathlib.Path) -> None:
    """Reading the column is what half the tree does — the window calculation,
    the console screens, the privacy export. None of it is a flip."""
    module = tmp_path / "reader.py"
    module.write_text(
        "from sqlalchemy import select\n"
        "from career.db.models import CustomerChannel\n"
        "def show(session, channel, now):\n"
        "    rows = session.execute(\n"
        "        select(CustomerChannel).where(CustomerChannel.opt_out_at.is_(None))\n"
        "    ).scalars().all()\n"
        "    state = window_state(opt_out_at=channel.opt_out_at, now=now)\n"
        "    export = {'opted_out_at': str(channel.opt_out_at)}\n"
        "    return rows, state, export\n",
        encoding="utf-8",
    )
    assert _flip_sites([module], tmp_path) == set()


def test_the_direct_message_alerts_do_not_reverse_for_the_operator() -> None:
    """The لمّاح+ escalation alerts, held direction-pure.

    `{shape}` renders an Arabic noun today (`whatsapp.worker._SPOKEN_MEDIA` is
    the only caller and every value in it is Arabic), so the old one-line form
    read correctly — but nothing on this side of the call can HOLD a future
    caller to that, and a message type passed through verbatim would reverse
    the line and take «مشترك لمّاح+» with it. On its own line it cannot.
    Green counterpart to the tree-wide bidi guard, which is red for other
    owners' files; see the same test in `test_whatsapp_worker`.
    """
    from tests.test_alert_direction_purity import SLOT, verdict

    hits = verdict().get("src/career/promises/career_session.py", [])
    assert not hits, "mixed-direction operator line(s) — " + " ; ".join(
        f"L{n}: {line.replace(SLOT, '{…}')!r}" for n, line in hits
    )


# ── the gate that decides WHEN the cadence above is read ────────────────────
#
# Everything above measures how late a promise is noticed once the housekeeping
# block runs. This is the other half: what makes it run at all. Both paging
# sweeps live in `scripts/run_worker_loop.py`'s hourly block, so its gate is
# part of how these two promises are measured — and it is the piece that had
# no test.
#
# THE HAZARD, as it shipped: `last_reminder_sweep = 0.0` compared against
# `time.monotonic()`, which is ~2,170,615 on this host, so the first pass of
# EVERY boot ran the whole block. Under Restart=always / RestartSec=5 a crash
# loop ran it every five seconds. Every idempotency guard below it holds — but
# a pass killed between `_alert(...)` and its commit re-pages on the next boot,
# and that trade («duplicated by a crash, never lost») was priced for a block
# that ran once a day.


def _worker_loop() -> Any:
    """`scripts/run_worker_loop.py` as a module — the process systemd runs.

    Loaded exactly as `test_whatsapp_worker._worker_loop` loads it, and for
    the same reason: the gate is code, and the wiring in a script is where
    this repository keeps losing things. `main()` is never executed —
    importing under a name other than `__main__` cannot start the loop.
    """
    import importlib.util
    import sys

    path = _REPO / "scripts" / "run_worker_loop.py"
    spec = importlib.util.spec_from_file_location("career_worker_loop", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["career_worker_loop"] = module
    spec.loader.exec_module(module)
    return module


class _Boots:
    """A host: one wall clock, one monotonic clock, and a `/run` that survives
    processes and not reboots. Every `boot()` is a NEW gate, because every
    systemd restart is a new process — which is exactly what the float this
    replaced could not see."""

    def __init__(self, stamp: pathlib.Path) -> None:
        self.loop = _worker_loop()
        self.stamp = stamp
        #: A realistic host uptime, because the defect was arithmetic about
        #: this number: 0.0 is never within an hour of it.
        self.mono = 2_170_615.0
        self.wall = 1_754_600_000.0

    def tick(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds

    def boot(self) -> Any:
        return self.loop.HousekeepingGate(
            path=str(self.stamp),
            wall=lambda: self.wall,
            mono=lambda: self.mono,
        )

    @property
    def interval(self) -> float:
        """The number the loop actually passes — read from the module, never
        typed here, so this suite cannot go on describing an hour after the
        block has been moved to four."""
        return float(self.loop.REMINDER_SWEEP_SECONDS)

    def sweeps_over(self, *, restarts: int, every: float) -> int:
        """How many times the housekeeping block runs across `restarts`
        boots `every` seconds apart. This is the shape of the whole defect."""
        swept = 0
        for _ in range(restarts):
            gate = self.boot()
            if gate.due(self.interval):
                swept += 1
                gate.mark()
            self.tick(every)
        return swept


def test_a_crash_loop_cannot_run_the_housekeeping_block_at_restart_cadence(
    tmp_path: pathlib.Path,
) -> None:
    """Twenty restarts in ten minutes must not be twenty housekeeping passes.

    The block pages twice — the 72-hour guarantee breach and the لمّاح+ SLA
    escalation — so at RestartSec=5 the priced duplicate becomes a storm on
    the one channel whose entire value is that a message on it means something
    new happened. `scripts/alert_unit_failure.sh` had to learn the same lesson
    about 283 identical messages a day: «muting is what happens to identical
    messages», and a muted channel silences the next real failure too.

    The number the fix replaced is asserted beside it, because a bound only
    means something next to what it bounds.
    """
    host = _Boots(tmp_path / "run" / "career" / "worker-housekeeping")

    # THE DEFECT, in the shape it shipped: a process-local float, zero at
    # every boot, compared against a monotonic clock measured in weeks.
    storm = 0
    for _ in range(20):
        last_reminder_sweep = 0.0                    # a fresh process
        if host.mono - last_reminder_sweep >= host.interval:
            storm += 1
        host.tick(30.0)
    assert storm == 20, (
        "the old gate is supposed to be the defect this test names"
    )

    # …and the gate that ships now, over the same ten minutes, on a host that
    # has just started (an empty /run of its own).
    host = _Boots(tmp_path / "run-fixed" / "career" / "worker-housekeeping")
    assert host.sweeps_over(restarts=20, every=30.0) == 1, (
        "a crash loop still runs the housekeeping block once per restart"
    )

    # …and the loop is actually WIRED to it. A gate nothing calls is the shape
    # of defect this repository keeps finding: built, tested, reaching nobody.
    #
    # Read from the TREE and not from the text. The first version of this
    # grepped for «last_reminder_sweep» and went red on the fix itself, whose
    # docstring names the float in order to bury it — a tripwire that cannot
    # tell a quotation from a claim teaches its next reader to delete the
    # quotation. An assignment is a Name node; a docstring is not.
    tree = ast.parse(
        (_REPO / "scripts" / "run_worker_loop.py").read_text(encoding="utf-8"))
    assigned = {
        target.id
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    assert "last_reminder_sweep" not in assigned, (
        "the process-local float is back in the loop — whatever it gates, it "
        "cannot tell a crash loop from an outage, because it dies with the "
        "process that was crashing"
    )
    gated = [
        node for node in ast.walk(tree) if isinstance(node, ast.If)
        and "attr='due'" in ast.dump(node.test)
    ]
    assert len(gated) == 1, "the hourly block is not gated by the stamp"
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "mark" for n in ast.walk(gated[0])), (
        "the pass never claims its interval, so the next boot sweeps again"
    )


def test_a_worker_that_was_down_still_catches_up_the_moment_it_is_back(
    tmp_path: pathlib.Path,
) -> None:
    """The property the boot-time pass exists for, and which the fix must not
    trade away: catching up is the REASON housekeeping runs at boot.

    A worker down for six hours has six hours of unmeasured promises behind
    it. It must sweep the instant it returns — not an hour later — and the two
    cases below are the two ways a worker comes back.
    """
    host = _Boots(tmp_path / "run" / "career" / "worker-housekeeping")

    first = host.boot()
    assert first.due(host.interval), "the very first boot of a host has nothing to catch up"
    first.mark()

    # 1 — the process died and stayed dead for six hours (systemd gave up, or
    # a human stopped it). The host stayed up, so the stamp is still there and
    # is six hours old: that is an OUTAGE, and it is due.
    host.tick(6 * 3600.0)
    assert host.boot().due(host.interval), (
        "a worker back from a six-hour outage waited for its housekeeping — "
        "which is the whole reason the block runs at boot"
    )

    # 2 — the HOST rebooted. /run is tmpfs, so the stamp is gone with it, and
    # a machine that was down is an outage by definition. Even one second
    # later, this is due.
    host.stamp.unlink()
    host.tick(1.0)
    assert host.boot().due(host.interval), (
        "a reboot cleared the stamp and the block still stood down — /run "
        "being tmpfs is the mechanism, not an accident of the path"
    )


def test_the_gate_is_a_rate_and_not_a_one_shot(tmp_path: pathlib.Path) -> None:
    """A long-lived healthy worker still sweeps every interval, and a crash
    loop that outlives an hour gets exactly one more pass in the next hour —
    the bound is a RATE, so a wedge that restarts all night pages at the
    cadence the promises are already measured at."""
    host = _Boots(tmp_path / "run" / "career" / "worker-housekeeping")
    interval = host.interval

    gate = host.boot()
    assert gate.due(interval)
    gate.mark()
    host.tick(interval - 1.0)
    assert not gate.due(interval)
    host.tick(2.0)
    assert gate.due(interval), "the healthy loop stopped sweeping after one pass"

    # four hours of crash-looping every 30s: four passes, not four hundred.
    # A `/run` of its own — a stamp is per HOST, and reusing the one above
    # would be a second host that inherited the first one's memory.
    host = _Boots(tmp_path / "run-2" / "career" / "worker-housekeeping")
    assert host.sweeps_over(restarts=480, every=30.0) == 4


def test_a_clock_that_stepped_backwards_measures_rather_than_stalls(
    tmp_path: pathlib.Path,
) -> None:
    """A stamp in the future is not an age — it is evidence the wall clock
    moved (NTP correcting a drifted host, a restored snapshot). Doubt about
    the clock has to measure, never stall: the alternative is a housekeeping
    block that stops reading a 24-hour promise until the clock catches up.

    `scripts/alert_unit_failure.sh` reasons its way to the same answer in its
    own backwards-step branch, and this is the residue named in
    `HousekeepingGate`: a step during a crash loop can still buy one extra
    pass. Bounded by the step, and it is the cheap direction.
    """
    host = _Boots(tmp_path / "run" / "career" / "worker-housekeeping")
    gate = host.boot()
    gate.mark()
    assert not gate.due(host.interval)

    host.wall -= 3 * 3600.0                       # the clock steps back
    assert gate.due(host.interval), (
        "a backwards clock step silenced the housekeeping block")


def test_an_unwritable_run_degrades_toward_silence_and_says_so(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The one case the fix cannot have both halves of, decided out loud.

    With no durable stamp, a crash loop and a long outage are indistinguishable
    again — so the gate picks the bounded loss. It stands down at boot (the
    catch-up is delayed by at most the interval this block already prices as
    the worst-case lateness of every clock in it, and BOTH paging sweeps keep
    their nightly backstop), rather than sweeping at boot (a storm bounded by
    nothing). And it says ERROR, because the journal harvester puts ERROR
    lines on the operator's error screen: a degradation nobody can see is how
    the last one survived.

    Detected in `__init__` and not at the first failed `mark`, which is the
    part that actually matters: a gate that finds out afterwards answers «no
    stamp, so catch up» on every single boot — the storm, exactly as it was.
    """
    # A parent that is a FILE, so the failure is real for root too (this suite
    # runs as root, and root ignores a chmod).
    blocker = tmp_path / "run"
    blocker.write_text("not a directory", encoding="utf-8")
    host = _Boots(blocker / "career" / "worker-housekeeping")

    with caplog.at_level("ERROR", logger="career.worker_loop"):
        assert host.sweeps_over(restarts=20, every=30.0) == 0
    assert any("housekeeping stamp" in r.getMessage() for r in caplog.records), (
        "the gate lost its durable record and told nobody"
    )

    # …and it is still a working hourly gate inside the process it is in:
    # degraded means «no catch-up», never «no housekeeping».
    gate = host.boot()
    assert not gate.due(host.interval)
    host.tick(host.interval + 1.0)
    assert gate.due(host.interval)


def test_an_unreadable_stamp_sweeps_rather_than_trusting_it(
    tmp_path: pathlib.Path,
) -> None:
    """Nothing may be suppressed by a stamp nobody can read. The write is
    atomic so this is near-unreachable, and «near» is not a reason to let
    garbage in a file stand a promise sweep down."""
    host = _Boots(tmp_path / "run" / "career" / "worker-housekeeping")
    gate = host.boot()
    gate.mark()
    assert not gate.due(host.interval)

    host.stamp.write_text("not-a-timestamp\n", encoding="utf-8")
    assert host.boot().due(host.interval)


def test_the_stamp_says_when_the_last_pass_ran(tmp_path: pathlib.Path) -> None:
    """`cat /run/career/worker-housekeeping` has to answer «when did
    housekeeping last run» on the night somebody is asking. The epoch is for
    the gate; the ISO copy beside it is for the human."""
    host = _Boots(tmp_path / "run" / "career" / "worker-housekeeping")
    host.boot().mark()
    epoch, iso = host.stamp.read_text(encoding="utf-8").split(" ", 1)
    assert float(epoch) == pytest.approx(host.wall, abs=1.0)
    assert str(datetime.fromtimestamp(host.wall, UTC).year) in iso
    # nothing but the stamp is left behind — the tmp file is renamed, never
    # accumulated, in a directory that lives for the whole boot
    assert [p.name for p in host.stamp.parent.iterdir()] == [host.stamp.name]


# ── the guard whose reason was invented ─────────────────────────────────────


def test_re_assigning_a_guarantees_own_status_writes_nothing(
    owner_session: Session, owner_engine: Engine, clean_billing: None,
) -> None:
    """The claim a comment in `guarantee` used to make, driven instead of read.

    It said re-assigning `row.status = WATCHING` «would send an UPDATE per
    tenant per pass, which was free once a night and is not free at hourly
    cadence». SQLAlchemy 2.0 compares the assigned value against the LOADED
    one when it builds the UPDATE, so an equal value emits no statement at
    all — `session.dirty` reports the row as dirty (it is documented as an
    optimistic guess) and the flush writes nothing. There was no cost to
    avoid, at any cadence.

    Two halves, and the second is the one that will still be true in a year:
    the exact assignment the comment described, and then a whole real pass
    over a WATCHING customer, asserting that the pass writes to
    `delivery_guarantees` exactly once — the INSERT that creates the row — and
    never again.
    """
    from sqlalchemy import event

    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, params: Any,
               context: Any, executemany: bool) -> None:
        head = " ".join(statement.split()[:2]).upper()
        if "DELIVERY_GUARANTEES" in statement.upper() and head.startswith(
                ("INSERT", "UPDATE", "DELETE")):
            statements.append(head.split()[0])

    _p, tenant_id, sub_id = _customer(owner_session)
    event.listen(owner_engine, "before_cursor_execute", record)
    try:
        # 1 — the reviewer's experiment, on the real row through the real
        # loader: assign the value the row already holds, and flush.
        row = guarantee._guarantee_row(
            owner_session, tenant_id=tenant_id, subscription_id=sub_id,
            activated_at=NOW,
        )
        assert statements == ["INSERT"], "the row is created once"
        # Committed first, so what follows is a LOADED value and not the
        # constant this module built the row from. That is the whole question:
        # the UPDATE is built by comparing against what came back from the
        # database, and a comparison against itself would prove nothing.
        owner_session.commit()
        statements.clear()
        loaded = row.status
        assert (loaded is not guarantee.WATCHING
                and loaded == guarantee.WATCHING), (
            "the assignment has to be a DIFFERENT object with an EQUAL value, "
            "or this proves nothing about how the UPDATE is built"
        )
        row.status = guarantee.WATCHING
        assert row in owner_session.dirty, (
            "session.dirty is an optimistic guess — if it ever stops saying "
            "«dirty» here, the flush below is no longer the thing being proven"
        )
        owner_session.flush()
        assert statements == [], (
            "SQLAlchemy wrote an UPDATE for an unchanged value — the comment "
            "this test replaced would then have been right, and the guard it "
            "justified has to come back with this test's output beside it"
        )

        # 2 — and a whole pass over a customer still inside his window writes
        # nothing at all.
        owner_session.commit()
        statements.clear()
        counts = guarantee.sweep_and_commit(
            owner_session, now=NOW + timedelta(hours=1),
            admin_client=FakeTelegramAdminClient(),
        )
        assert counts["watching"] == 1
        assert statements == []
    finally:
        event.remove(owner_engine, "before_cursor_execute", record)
        # never leave an open transaction behind an assertion: `clean_billing`
        # deletes the tenant, and an uncommitted row that references it turns a
        # failing assertion into a hung suite
        owner_session.rollback()


def test_a_clock_that_steps_backwards_does_not_un_breach_a_guarantee(
    owner_session: Session, clean_billing: None,
) -> None:
    """«A breach is never un-breached» — including by the wall clock.

    The guard that carried the invented cost was `if row.status != WATCHING:
    row.status = WATCHING`, and the only status that could ever reach it —
    MET and SETTLED return above — is a BREACHED row seen with `now` behind
    its deadline. There the guard did not prevent a write, it PERFORMED one:
    it set a decided guarantee back to WATCHING, which drops the customer off
    `open_breaches`, the operator's queue of people owed a decision about
    money, until the clock catches up. And `breached_at` — the timestamp he
    reads beside that decision — would then be rewritten to whenever the row
    re-broke.

    Unlikely, and the module's own words are «never»: this promise costs
    money, and a sweep that can quietly reopen a settled question about it is
    not something to leave standing because the trigger is rare.
    """
    _p, tenant_id, _s = _customer(owner_session)
    admin = FakeTelegramAdminClient()
    breach_time = NOW + timedelta(hours=73)
    assert guarantee.sweep_and_commit(
        owner_session, now=breach_time, admin_client=admin)["breached"] == 1
    breached_at = owner_session.execute(sql_text(
        "SELECT breached_at FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()

    # NTP corrects a host that had drifted forward: the next pass runs with a
    # `now` inside the window this row has already outlived.
    guarantee.sweep_and_commit(
        owner_session, now=NOW + timedelta(hours=1), admin_client=admin)

    status, still = owner_session.execute(sql_text(
        "SELECT status, breached_at FROM delivery_guarantees"
        " WHERE tenant_id = :t"), {"t": str(tenant_id)}).one()
    assert status == guarantee.BREACHED, (
        "a backwards clock step un-breached a guarantee the customer is owed "
        "a refund or an extension for"
    )
    assert still == breached_at, "…and rewrote when it broke"
    assert [b.tenant_id for b in guarantee.open_breaches(owner_session)
            if b.tenant_id == tenant_id], (
        "the breach fell off the operator's queue while the clock was behind"
    )
    # and it is still paged exactly once, across all three passes
    assert len([m for m in admin.messages if "🛡️" in m]) == 1


# ── «commits its own work and NEVER raises», driven at the commit ───────────


class _CommitFails:
    """A session whose COMMIT dies — the failure both entry points claim to
    absorb, and the only one their tests never drove.

    Everything else delegates to a real session, so the sweep above the commit
    is the real sweep against the real database. `rollback_too` is the double
    fault: a connection that has already gone takes the rollback with it, and
    that is precisely the shape in which a bare `session.rollback()` in a
    caller's own except-block escapes into the delivery day.
    """

    def __init__(self, session: Session, *, rollback_too: bool = False) -> None:
        self._session = session
        self._rollback_too = rollback_too
        self.rolled_back = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def commit(self) -> None:
        raise RuntimeError("server closed the connection unexpectedly")

    def rollback(self) -> None:
        self.rolled_back += 1
        if self._rollback_too:
            raise RuntimeError("connection already closed")
        self._session.rollback()


@pytest.mark.parametrize("rollback_too", [False, True])
def test_the_scheduled_entry_points_never_raise_when_the_commit_does(
    owner_session: Session, clean_billing: None, rollback_too: bool,
) -> None:
    """The contract both `*_and_commit` docstrings state, at the one line
    nothing was driving.

    «Commits its own work and NEVER raises» was tested for failures INSIDE the
    sweep and never for a failure of `session.commit()` itself — so the suite
    would not have noticed a future edit moving the commit out of the `try`.
    It holds today, including the double fault where the rollback dies too;
    this is what makes that a fact about the tree rather than a reading of it.

    It matters at both callers. In the worker the hourly block is a barrier —
    an escape costs that cycle its watchdog pet, and a wedged cycle is a
    killed worker. In the nightly it runs FIRST, before any of the delivery
    day exists.
    """
    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(
        owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()

    overdue = NOW + timedelta(hours=25, days=4)     # both promises are broken
    admin = FakeTelegramAdminClient()
    dying = _CommitFails(owner_session, rollback_too=rollback_too)

    # The rollback is in a `finally` and the assertions come after it. A
    # double fault leaves the real session «idle in transaction» holding an
    # uncommitted INSERT, and `clean_billing`'s DELETE then waits on it
    # forever: without this, a FAILING assertion here does not report, it
    # hangs the suite.
    try:
        swept = guarantee.sweep_and_commit(
            dying, now=overdue, admin_client=admin)
        escalated = career_session.escalate_overdue_and_commit(
            dying, now=overdue, admin_client=admin)
    finally:
        owner_session.rollback()

    assert swept == {"watching": 0, "met": 0, "breached": 0, "alerted": 0}
    assert escalated == {"escalated": 0, "unticketed": 0}
    assert dying.rolled_back == 2, "a failed pass has to try to roll back"
    # nothing survived the failed passes: no breach, no ticket, no stamp
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM delivery_guarantees WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() == 0
    assert owner_session.execute(sql_text(
        "SELECT escalated_at FROM career_sessions WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one() is None
    assert owner_session.execute(sql_text(
        "SELECT count(*) FROM support_events WHERE tenant_id = :t AND kind = :k"),
        {"t": str(tenant_id), "k": career_session.OVERDUE_TICKET_KIND}
    ).scalar_one() == 0


def test_a_dying_connection_in_the_promise_sweep_cannot_stop_the_night(
    owner_session: Session, clean_billing: None,
) -> None:
    """The same double fault through the REAL nightly caller.

    `cv.daily_run.sweep_promises` is called by `run_daily_delivery` FIRST,
    before any of the night's own work exists, so anything that escapes it
    costs a paying customer his delivery day. Until 2026-08-08 the SLA half
    did not go through `escalate_overdue_and_commit` at all: it called the raw
    sweep and re-implemented the contract here with its own try/except and a
    bare `session.rollback()`. That copy was the weaker one — a rollback that
    itself raised went straight up — and the entry point it declined to use is
    the one whose docstring says it is «the entry point every SCHEDULED caller
    should use», with the guarded rollback already in it.
    """
    from career.cv.daily_run import DailyDeps, sweep_promises

    _p, tenant_id, _s = _customer(owner_session, product="prod_plus")
    career_session.request_session(
        owner_session, tenant_id=tenant_id, now=NOW)
    owner_session.commit()

    deps = DailyDeps(storage=None, whatsapp_client=FakeWhatsAppClient(),
                     admin_client=FakeTelegramAdminClient(), llm=None)
    dying = _CommitFails(owner_session, rollback_too=True)

    try:                       # see the note in the test above: a failed
        counts = sweep_promises(   # assertion must report, never hang
            dying, deps=deps, now=NOW + timedelta(hours=25, days=4))
    finally:
        owner_session.rollback()

    # every key present and zero: the night gets an honest «nothing measured»,
    # never a missing counter and never an exception
    assert counts == {"watching": 0, "met": 0, "breached": 0, "alerted": 0,
                      "escalated": 0, "unticketed": 0}
    assert dying.rolled_back == 2
