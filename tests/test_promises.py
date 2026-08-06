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

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text as sql_text
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
