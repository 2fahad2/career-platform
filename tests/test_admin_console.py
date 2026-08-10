"""Watchtower console — router auth, screens, stateless callbacks, PII-free
views. Renderers are golden-tested pure; DB screens run on career_test."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career import support
from career.telegram import console, views
from career.telegram.console import EXPIRED_BUTTON_AR, handle_update

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)   # Wed 12:00 Riyadh
ADMIN = "666"


class FakeProbes:
    def __init__(self, facts: dict[str, Any] | None = None,
                 errors: list[str] | None = None) -> None:
        self.facts = facts if facts is not None else {}
        self.errors = errors if errors is not None else []

    def collect(self) -> dict[str, Any]:
        return self.facts

    def error_lines(self) -> list[str]:
        return self.errors


def _msg(
    chat_id: str, text: str = "/start", reply_to: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"chat": {"id": int(chat_id)}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": 7, "text": reply_to}
    return {"message": message}


def _cbq(chat_id: str, data: str, message_id: int = 10) -> dict[str, Any]:
    return {"callback_query": {
        "id": "cbq1", "from": {"id": int(chat_id)},
        "data": data, "message": {"message_id": message_id},
    }}


# ── auth ─────────────────────────────────────────────────────────────────────


def test_foreign_chat_is_ignored_silently(owner_session: Session) -> None:
    assert handle_update(
        owner_session, _msg("999"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    ) == []
    assert handle_update(
        owner_session, _cbq("999", "v1|today"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    ) == []


def test_any_operator_text_opens_the_menu(owner_session: Session) -> None:
    for text in ("/start", "مرحبا", "؟"):
        outcomes = handle_update(
            owner_session, _msg(ADMIN, text), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        assert len(outcomes) == 1
        assert outcomes[0].kind == "send"
        assert "برج المراقبة" in outcomes[0].text
        assert outcomes[0].keyboard


# ── stateless callbacks ──────────────────────────────────────────────────────


def test_callback_acks_then_edits_in_place(owner_session: Session) -> None:
    outcomes = handle_update(
        owner_session, _cbq(ADMIN, "v1|menu", message_id=42),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
    )
    kinds = [o.kind for o in outcomes]
    assert kinds == ["ack", "edit"]
    assert outcomes[1].message_id == 42


def test_unknown_or_stale_callback_answers_expired(owner_session: Session) -> None:
    for data in ("v2|later", "garbage", "v1|no_such_screen", ""):
        outcomes = handle_update(
            owner_session, _cbq(ADMIN, data), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        assert [o.kind for o in outcomes] == ["ack"]
        assert outcomes[0].text == EXPIRED_BUTTON_AR


def test_soon_screens_render_with_home_button(owner_session: Session) -> None:
    outcomes = handle_update(
        owner_session, _cbq(ADMIN, "v1|soon|customers"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
    )
    assert "المرحلة القادمة" in outcomes[1].text
    assert outcomes[1].keyboard == [[("🏠 الرئيسية", "v1|menu")]]


# ── the today screen (DB) ────────────────────────────────────────────────────


def test_today_screen_shows_ten_codes_and_states(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    t1, _ = two_tenants
    code = owner_session.execute(
        sql_text("SELECT code FROM tenants WHERE id = :t"), {"t": t1}
    ).scalar_one()
    owner_session.execute(sql_text(
        "INSERT INTO tenant_day_states (id, tenant_id, run_date, state, counts)"
        " VALUES (:id, :t, :d, 'DELIVERED',"
        " '{\"delivered\": 2, \"failed_sends\": 0}'::jsonb)"),
        {"id": str(uuid.uuid4()), "t": t1, "d": NOW.date().isoformat()},
    )
    owner_session.commit()
    try:
        outcomes = handle_update(
            owner_session, _cbq(ADMIN, "v1|today"), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        text = outcomes[1].text
        assert code in text
        assert "✅ سُلّم" in text
        assert "سُلّم ٢" in text          # a count inside an Arabic line
    finally:
        owner_session.execute(sql_text(
            "DELETE FROM tenant_day_states WHERE tenant_id = :t"), {"t": t1})
        owner_session.commit()


# ── customers / business / errors (phase 2+3) ────────────────────────────────


def test_customers_list_and_card_are_pii_free(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    outcomes = handle_update(
        owner_session, _cbq(ADMIN, "v1|customers|0"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )
    text = outcomes[1].text
    assert "👥 العملاء" in text
    keyboard = outcomes[1].keyboard or []
    tenant_buttons = [
        b for row in keyboard for b in row if b[1].startswith("v1|tenant|")
    ]
    assert len(tenant_buttons) >= 2                      # seeded tenants listed
    # drill into the first card
    code = tenant_buttons[0][1].split("|")[2]
    card = handle_update(
        owner_session, _cbq(ADMIN, f"v1|tenant|{code}"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )
    assert code in card[1].text
    assert "قراراته" in card[1].text
    assert "+966" not in card[1].text                    # no phone can appear


def test_customers_pagination_bounds(owner_session: Session) -> None:
    outcomes = handle_update(
        owner_session, _cbq(ADMIN, "v1|customers|99"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )
    assert outcomes[1].kind == "edit"                    # far page renders empty
    assert handle_update(
        owner_session, _cbq(ADMIN, "v1|customers|x"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )[0].text == EXPIRED_BUTTON_AR                       # garbage page → expired


def test_unknown_tenant_card_answers_expired(owner_session: Session) -> None:
    outcomes = handle_update(
        owner_session, _cbq(ADMIN, "v1|tenant|TEN-9999"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )
    assert [o.kind for o in outcomes] == ["ack"]


def test_business_screen_ranges(owner_session: Session) -> None:
    for key in ("7", "30", "all"):
        outcomes = handle_update(
            owner_session, _cbq(ADMIN, f"v1|business|{key}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )
        assert "💰 الأعمال" in outcomes[1].text
    assert handle_update(
        owner_session, _cbq(ADMIN, "v1|business|9000"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )[0].text == EXPIRED_BUTTON_AR


def test_errors_screen_renders_collector_lines(owner_session: Session) -> None:
    outcomes = handle_update(
        owner_session, _cbq(ADMIN, "v1|errors"), admin_chat_id=ADMIN,
        probes=FakeProbes(errors=["career.worker_loop · worker cycle failed"]),
        now=NOW,
    )
    assert "worker cycle failed" in outcomes[1].text
    clean = handle_update(
        owner_session, _cbq(ADMIN, "v1|errors"), admin_chat_id=ADMIN,
        probes=FakeProbes(errors=[]), now=NOW,
    )
    assert "نظيف" in clean[1].text


# ── renderers: golden + PII discipline ───────────────────────────────────────


def test_render_health_full_and_unknown() -> None:
    text, keyboard = views.render_health({
        "worker_active": True, "timer_next": "السبت 04:30",
        "meta_token_ok": True, "salla_days_left": 5,
        "searchapi": (10_000, 9_000),
        "templates": {"APPROVED": 2, "PENDING": 5},
        "backup_age_hours": 11.0,
    })
    assert "🟢 يعمل" in text
    # every count on this screen is Arabic-Indic: it is what lets each fact
    # keep ONE line, and half-and-half would be worse than either choice
    assert "🟠 ينتهي بعد ٥" in text
    assert "باقي ٩٠٠٠ من ١٠٠٠٠" in text
    assert "⏳ ٥ معلق" in text
    assert "مؤقت التسليم: 🟢 التالي السبت ٠٤:٣٠" in text
    assert keyboard[0][0][1] == "v1|health"

    text_unknown, _ = views.render_health({})
    assert text_unknown.count("⚪ غير معروف") >= 4


def test_render_today_without_any_run() -> None:
    text, _ = views.render_today(NOW.date(), None, [])
    assert "ما صارت تشغيلة اليوم" in text
    assert "ولا توجد أي تشغيلة مسجلة من قبل" in text
    assert "لا حالات عملاء" in text


def test_render_today_names_yesterdays_run_as_yesterdays() -> None:
    """AUDIT: a missing nightly run must READ as missing — the last run is
    shown only under its OWN date, never as today's counters."""
    text, _ = views.render_today(
        NOW.date(), None, [],
        {"run_date": "2026-07-14", "status": "completed"},
    )
    assert "ما صارت تشغيلة اليوم" in text
    assert "آخر تشغيلة مسجلة كانت بتاريخ" in text
    assert "2026-07-14" in text
    assert "وحالتها: ✅ اكتملت" in text
    # yesterday's date is never presented as the day being reported
    assert text.splitlines()[1] == NOW.date().isoformat()


def test_render_today_unknown_run_status_keeps_its_own_line() -> None:
    text, _ = views.render_today(
        NOW.date(), {"status": "weird_new_status", "counts": {}}, []
    )
    lines = text.splitlines()
    assert "التشغيلة:" in lines
    assert "weird_new_status" in lines


def test_today_screen_says_no_run_when_the_night_did_not_fire(
    owner_session: Session,
) -> None:
    """The bug this fixes: yesterday's run row used to be read with NO date
    filter, so it sat under today's heading and looked like a good night."""
    yesterday = (NOW.date() - timedelta(days=1)).isoformat()
    old_id, today_id = str(uuid.uuid4()), str(uuid.uuid4())
    owner_session.execute(sql_text(
        "INSERT INTO discovery_runs (id, run_date, status, digest_only, counts,"
        " started_at) VALUES (:i, :d, 'completed', false,"
        " '{\"fetched\": 99, \"passed\": 7}'::jsonb, :s)"),
        {"i": old_id, "d": yesterday, "s": NOW - timedelta(days=1)})
    owner_session.commit()
    try:
        out = handle_update(
            owner_session, _cbq(ADMIN, "v1|today"), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        text = out[1].text
        assert "ما صارت تشغيلة اليوم" in text
        assert "٩٩" not in text            # yesterday's counters stay away
        assert yesterday in text           # but its date is shown, labelled

        # and once today's run exists, TODAY's numbers are the ones shown
        owner_session.execute(sql_text(
            "INSERT INTO discovery_runs (id, run_date, status, digest_only,"
            " counts, started_at) VALUES (:i, :d, 'partial', false,"
            " '{\"fetched\": 12, \"passed\": 3}'::jsonb, :s)"),
            {"i": today_id, "d": NOW.date().isoformat(), "s": NOW})
        owner_session.commit()
        out2 = handle_update(
            owner_session, _cbq(ADMIN, "v1|today"), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        assert "🟠 جزئية" in out2[1].text
        assert "المكتشف: ١٢" in out2[1].text
        assert "ما صارت تشغيلة اليوم" not in out2[1].text
    finally:
        owner_session.execute(
            sql_text("DELETE FROM discovery_runs WHERE id IN (:a, :b)"),
            {"a": old_id, "b": today_id},
        )
        owner_session.commit()


def test_views_render_only_ten_codes_never_identity_fields() -> None:
    """PII guard: a hostile counts payload with identity-looking keys must
    not leak — renderers print ONLY code/state/known numeric counts."""
    text, _ = views.render_today(
        NOW.date(), {"status": "completed", "counts": {}},
        [("TEN-0009", "DELIVERED",
          {"delivered": 1, "failed_sends": 0,
           "name": "فلان الفلاني", "phone": "+9665xxxxxxx"})],
    )
    assert "فلان" not in text
    assert "+9665" not in text
    assert "TEN-0009" in text


def test_every_screen_and_button_is_direction_pure_when_rendered() -> None:
    """The rule applied to the OUTPUT, not to the source.

    ``tests/test_alert_direction_purity`` reads the source and is the stronger
    guard — it cannot be dodged by a value nobody thought to test. This one
    catches what the source cannot see: a VALUE arriving mixed from outside
    the module (the timer probe's «السبت 04:30», a plan code or a day state
    nobody mapped, a status token straight from the database). Every screen is
    rendered with its unknown-token path taken on purpose, and the BUTTONS are
    checked too, because a button label is a line the same client scrambles
    and it is the one line that cannot be split in half.
    """
    import re

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")

    def check(what: str, text: str, keyboard: object = ()) -> None:
        for line in text.split("\n"):
            assert not (arabic.search(line) and latin_or_digit.search(line)), (
                f"{what}: mixed line {line!r}"
            )
        for row in keyboard or ():
            for label, _cb in row:
                assert not (arabic.search(label)
                            and latin_or_digit.search(label)), (
                    f"{what}: mixed button {label!r}"
                )

    check("menu", *views.render_menu())
    check("today", *views.render_today(
        NOW.date(),
        {"status": "weird_new_status", "counts": {"fetched": 12}},
        [("TEN-0002", "DELIVERED", {"delivered": 2, "failed_sends": 1}),
         ("TEN-0009", "A_STATE_NOBODY_MAPPED", {})],
        {"run_date": "2026-07-14", "status": "another_unknown"},
    ))
    check("health", *views.render_health({
        "worker_active": True, "timer_next": "السبت 04:30 بتوقيت الرياض",
        "meta_token_ok": True, "salla_days_left": 5,
        "searchapi": (10_000, 9_000),
        "templates": {"APPROVED": 2, "PENDING": 5, "REJECTED": 1},
        "deployed_source": ("abc123", "def456"), "backup_age_hours": 40.0,
    }))
    check("customers", *views.render_customers(
        [{"code": "TEN-0002", "plan_code": "executive",
          "journey_state": "ACTIVE", "window": "open"},
         {"code": "TEN-0009", "plan_code": "a_plan_nobody_mapped",
          "journey_state": "SOME_NEW_STATE", "window": "closed"}],
        page=0, total=17,
    ))
    check("card", *views.render_tenant_card({
        "code": "TEN-0002", "plan_code": "a_plan_nobody_mapped",
        "sub_status": "ACTIVE", "sub_age_days": 22,
        "journey_state": "SOME_NEW_STATE", "window": "a_new_window",
        "last_delivery": {"run_date": "2026-08-01", "status": "COMPLETED"},
        "outcomes": {"applied": 9, "ignored": 3}, "suppressions": 4,
        "support_minutes": 15, "review_count": 2,
    }))
    check("business", *views.render_business("30", {
        "subs_by_plan": {"professional": 3, "a_plan_nobody_mapped": 1},
        "revenue_sar": 1245, "funnel_upgrades": 2,
        "searches_this_month": 1340, "delivered_days": 27,
        "outcomes": {"applied": 9, "ignored": 3},
        "llm_generations": 41, "llm_cost_usd": "1.83",
        "message_statuses": {"read": 118, "delivered": 12, "sent": 4,
                             "failed": 1},
    }))
    check("business/unknown range", *views.render_business("99", {}))

    from datetime import date as _date

    from career.telegram.weekly_report import format_weekly_report

    check("weekly", format_weekly_report(
        _date(2026, 8, 2),
        {"subs_by_plan": {"professional": 3, "a_plan_nobody_mapped": 1},
         "revenue_sar": 1245, "funnel_upgrades": 2,
         "outcomes": {"applied": 9, "ignored": 3},
         "message_statuses": {"read": 118},
         "llm_generations": 41, "llm_cost_usd": "1.83"},
        {"DELIVERED": 24, "A_STATE_NOBODY_MAPPED": 2},
        covering_through=_date(2026, 8, 5),
    ))


def test_counts_are_arabic_indic_and_screens_did_not_double_in_height() -> None:
    """WHICH cure was applied, pinned — because the other one also passes the
    purity check and is the wrong answer here.

    A count can be made safe two ways: give it a line of its own, or write it
    in Arabic-Indic digits. On a dashboard read in two seconds the second is
    the only one that is still a dashboard afterwards, so the counts stay in
    their sentences and it is IDENTIFIERS — the TEN code, the ISO date, an
    unmapped token — that get the lines. If someone ever «fixes» this by
    splitting the counts out, every purity test stays green and this one does
    not.
    """
    text, _ = views.render_business("30", {
        "subs_by_plan": {"professional": 3}, "revenue_sar": 1245,
        "searches_this_month": 1340, "outcomes": {"applied": 9, "ignored": 3},
    })
    assert "💰 اشتراكات جديدة: لمّاح: ٣" in text
    assert "الإيراد: ١٢٤٥ ريال" in text
    assert "بحثات هذا الشهر: ١٣٤٠ من ١٠٠٠٠" in text
    assert "قرارات العملاء: قدّم ٩ · تجاهل ٣ (٧٥٪ تقديم)" in text
    # …and no Western digit is left anywhere in the Arabic block
    assert not any(ch.isdigit() and ch.isascii() for ch in text)

    # the identifier rule, the other half of the same judgement: the TEN code
    # takes the line above its row rather than dragging the row around with it
    today, _ = views.render_today(
        NOW.date(), {"status": "completed", "counts": {}},
        [("TEN-0002", "DELIVERED", {"delivered": 2, "failed_sends": 1})],
    )
    lines = today.split("\n")
    assert "TEN-0002" in lines
    assert "✅ سُلّم · سُلّم ٢ · فشل ١" in lines


def test_manual_usage_buttons_log_and_rerender(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """§14 manual counters: +5 support minutes / +1 human review."""
    t1, _ = two_tenants
    code = owner_session.execute(
        sql_text("SELECT code FROM tenants WHERE id = :t"), {"t": t1}
    ).scalar_one()
    out = handle_update(
        owner_session, _cbq(ADMIN, f"v1|log|{code}|support5"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
    )
    assert out[1].kind == "edit"
    assert "دقائق دعم: ٥" in out[1].text
    out2 = handle_update(
        owner_session, _cbq(ADMIN, f"v1|log|{code}|review"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
    )
    assert "مراجعات بشرية: ١" in out2[1].text
    owner_session.execute(sql_text(
        "DELETE FROM usage_events WHERE tenant_id = :t"), {"t": t1})
    owner_session.commit()


def test_pause_action_double_confirm_nonce_flow(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Phase 4: tenant card → pause → confirm card w/ one-shot nonce →
    confirm executes; a reused or stale nonce is refused."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = owner_session.execute(
        sql_text("SELECT code FROM tenants WHERE id = :t"), {"t": t1}
    ).scalar_one()
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id) VALUES (:i, :t, 'professional', 'ACTIVE', :o)"),
        {"i": str(uuid.uuid4()), "t": t1, "o": f"O-{uuid.uuid4()}"})
    owner_session.commit()
    try:
        # tap «pause» → a confirm card carrying a nonce
        out = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|pause"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )
        confirm_btn = out[1].keyboard[0][0][1]
        assert confirm_btn.startswith("v1|confirm|")
        assert "تأكيد" in out[1].text

        # confirm → paused + card re-rendered
        done = handle_update(
            owner_session, _cbq(ADMIN, confirm_btn), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        assert "أوقفنا" in done[1].text
        status = owner_session.execute(sql_text(
            "SELECT status FROM subscriptions WHERE tenant_id = :t"),
            {"t": t1}).scalar_one()
        assert status == "PAUSED"

        # the SAME nonce again → refused (one-shot)
        again = handle_update(
            owner_session, _cbq(ADMIN, confirm_btn), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        assert again[0].text == console.ACTION_EXPIRED_AR
    finally:
        owner_session.execute(sql_text(
            "DELETE FROM subscriptions WHERE tenant_id = :t"), {"t": t1})
        owner_session.commit()


def test_action_nonce_expires_after_five_minutes(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from datetime import timedelta

    import career.telegram.console as console

    t1, _ = two_tenants
    code = owner_session.execute(
        sql_text("SELECT code FROM tenants WHERE id = :t"), {"t": t1}
    ).scalar_one()
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id) VALUES (:i, :t, 'professional', 'ACTIVE', :o)"),
        {"i": str(uuid.uuid4()), "t": t1, "o": f"O-{uuid.uuid4()}"})
    owner_session.commit()
    try:
        out = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|pause"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )
        confirm_btn = out[1].keyboard[0][0][1]
        late = handle_update(
            owner_session, _cbq(ADMIN, confirm_btn), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW + timedelta(minutes=6),
        )
        assert late[0].text == console.ACTION_EXPIRED_AR
        status = owner_session.execute(sql_text(
            "SELECT status FROM subscriptions WHERE tenant_id = :t"),
            {"t": t1}).scalar_one()
        assert status == "ACTIVE"                     # never executed
    finally:
        owner_session.execute(sql_text(
            "DELETE FROM subscriptions WHERE tenant_id = :t"), {"t": t1})
        owner_session.commit()


def _seed_subscription(
    session: Session, tenant_id: str, status: str, *,
    plan: str = "professional",
) -> None:
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id) VALUES (:i, :t, :p, :s, :o)"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "p": plan, "s": status,
         "o": f"O-{uuid.uuid4()}"})
    session.commit()


def _sub_status(session: Session, tenant_id: str) -> str:
    return str(session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE tenant_id = :t"),
        {"t": tenant_id}).scalar_one())


def _drop_subscriptions(session: Session, tenant_id: str) -> None:
    session.rollback()
    session.execute(sql_text(
        "DELETE FROM subscription_events WHERE tenant_id = :t"), {"t": tenant_id})
    session.execute(sql_text(
        "DELETE FROM subscriptions WHERE tenant_id = :t"), {"t": tenant_id})
    session.commit()


def _confirm(
    session: Session, code: str, action: str, **kw: Any
) -> list[Any]:
    """Tap the action button, then its confirmation — the operator's two taps."""
    act = handle_update(
        session, _cbq(ADMIN, f"v1|act|{code}|{action}"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW, **kw,
    )
    return handle_update(
        session, _cbq(ADMIN, act[1].keyboard[0][0][1]), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW, **kw,
    )


def test_pause_and_resume_survive_a_tenant_with_no_live_subscription(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """INCIDENT: one tap on ⏸️ took the WHOLE watchtower down for good.

    A tenant with no live subscription is routine, not exotic — every §04
    upgrade leaves a shell behind and the customers list filters nothing — and
    its card carries a live pause button. ``privacy._subscription`` raises
    RequestNotFound for it, the action path had no guard, and the runner
    stores the Telegram offset only AFTER handle_update returns: the same
    update was re-fed forever, five seconds apart, and no button worked at
    all until someone restarted the service.

    Nothing the operator can tap may leave this function by raising.
    """
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    card = handle_update(
        owner_session, _cbq(ADMIN, f"v1|tenant|{code}"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )
    assert f"v1|act|{code}|pause" in _buttons(card[1])   # genuinely reachable

    for action in ("pause", "resume"):
        # a stale card still offers «استئناف» after a resume, so both halves
        # of the pair are reachable for a tenant that has no subscription
        done = _confirm(owner_session, code, action)
        assert [o.kind for o in done] == ["ack", "edit"]
        assert done[1].text.startswith(console.ACTION_NO_SUB_AR.format(code=code))
        assert "✅" not in console.ACTION_NO_SUB_AR


def test_resume_never_claims_a_success_it_did_not_achieve(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """AUDIT: ``resume_subscription`` returns the row UNCHANGED whenever the
    status is not exactly PAUSED, while the console printed «✅ استأنفنا
    الخدمة للعميل» unconditionally. A false success on the one action the
    operator needs most — putting a paying customer back in service — sends
    them away believing the customer is served while nothing moved."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_subscription(owner_session, t1, "EXPIRED")
    try:
        done = _confirm(owner_session, code, "resume")
        assert "استأنفنا" not in done[1].text
        assert done[1].text.startswith(
            console.RESUME_NOT_PAUSED_AR.format(code=code, status="EXPIRED")
        )
        assert _sub_status(owner_session, t1) == "EXPIRED"   # nothing moved
    finally:
        _drop_subscriptions(owner_session, t1)


def test_pause_never_claims_a_success_it_did_not_achieve(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The same lie in the other direction: pausing an EXPIRED subscription
    is a no-op inside privacy, and the console answered «✅ أوقفنا الخدمة»."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_subscription(owner_session, t1, "EXPIRED")
    try:
        done = _confirm(owner_session, code, "pause")
        assert "أوقفنا" not in done[1].text
        assert done[1].text.startswith(
            console.PAUSE_REFUSED_AR.format(code=code, status="EXPIRED")
        )
        assert _sub_status(owner_session, t1) == "EXPIRED"
    finally:
        _drop_subscriptions(owner_session, t1)


def test_pausing_an_already_paused_customer_says_so_plainly(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Idempotent, and honest about being idempotent — «✅ أوقفنا» on a second
    tap reads as a fresh pause of a customer who was already off."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_subscription(owner_session, t1, "PAUSED")
    try:
        done = _confirm(owner_session, code, "pause")
        assert done[1].text.startswith(
            console.PAUSE_ALREADY_AR.format(code=code)
        )
        assert _sub_status(owner_session, t1) == "PAUSED"
    finally:
        _drop_subscriptions(owner_session, t1)


def test_pause_in_grace_is_refused_not_crashed(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The second crash path, same shape as the first. privacy._PAUSABLE
    counts GRACE as pausable while the state machine has no GRACE→PAUSED edge,
    so the tap raised InvalidTransition out of handle_update — and a customer
    whose period just ended is exactly who the operator reaches for."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_subscription(owner_session, t1, "GRACE")
    try:
        done = _confirm(owner_session, code, "pause")
        assert [o.kind for o in done] == ["ack", "edit"]
        assert done[1].text.startswith(
            console.PAUSE_REFUSED_AR.format(code=code, status="GRACE")
        )
        assert _sub_status(owner_session, t1) == "GRACE"
    finally:
        _drop_subscriptions(owner_session, t1)


def test_pause_and_resume_still_work_and_say_so(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The honest ✅ is still reachable — verified against the DB, both ways."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_subscription(owner_session, t1, "ACTIVE")
    try:
        done = _confirm(owner_session, code, "pause")
        assert done[1].text.startswith("✅ أوقفنا")
        assert _sub_status(owner_session, t1) == "PAUSED"
        back = _confirm(owner_session, code, "resume")
        assert back[1].text.startswith(
            console._ACTIONS["resume"][1].format(code=code)
        )
        assert _sub_status(owner_session, t1) == "ACTIVE"
    finally:
        _drop_subscriptions(owner_session, t1)


def test_no_single_screen_can_take_the_console_down(
    owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blast radius, not the bug: because the runner advances the Telegram
    offset only after handle_update returns, ANY escaping exception is a
    permanent console outage rather than one failed tap. The barrier turns it
    back into one failed tap — logged as an ERROR (so it still surfaces on the
    ⚠️ الأخطاء screen) and never printed as a Python traceback."""
    import career.telegram.console as console

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("a bug nobody predicted")

    monkeypatch.setattr(console, "_today_data", boom)
    out = handle_update(
        owner_session, _cbq(ADMIN, "v1|today"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )
    assert [o.kind for o in out] == ["ack"]
    assert out[0].text == console.SCREEN_FAILED_AR
    assert "RuntimeError" not in out[0].text
    # and the very next tap is served normally — no loop, no wedged session
    assert handle_update(
        owner_session, _cbq(ADMIN, "v1|menu"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )[1].kind == "edit"


def test_a_broken_screen_is_never_answered_to_a_foreign_chat(
    owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The barrier must not become a reply channel for anyone but the
    operator — auth is decided before it, not inside it."""
    import career.telegram.console as console

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("a bug nobody predicted")

    monkeypatch.setattr(console, "_today_data", boom)
    assert handle_update(
        owner_session, _cbq("999", "v1|today"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    ) == []


def test_subscription_action_replies_are_direction_pure(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Fahad's client scrambles any line mixing Arabic with Latin/digits: the
    TEN code and the raw status token each stand on a line of their own."""
    import re

    import career.telegram.console as console

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    texts = [
        console.ACTION_NO_SUB_AR.format(code=code),
        console.PAUSE_ALREADY_AR.format(code=code),
        console.ACTION_FAILED_AR.format(code=code),
        console.SCREEN_FAILED_AR,
        console.REPLY_SENT_UNLOGGED_AR.format(code=code),
        console.PAUSE_REFUSED_AR.format(code=code, status="GRACE"),
        console.RESUME_NOT_PAUSED_AR.format(code=code, status="EXPIRED"),
    ]
    for text in texts:
        for line in text.splitlines():
            if arabic.search(line):
                assert not latin_or_digit.search(line), line


def test_weekly_report_format_is_arabic_and_complete() -> None:
    from datetime import date

    from career.telegram.weekly_report import format_weekly_report

    report = format_weekly_report(
        date(2026, 7, 26),
        {"subs_by_plan": {"professional": 2}, "revenue_sar": 558,
         "funnel_upgrades": 1, "outcomes": {"applied": 3, "ignored": 1},
         "message_statuses": {"read": 12, "delivered": 15},
         "llm_generations": 8, "llm_cost_usd": "0.40"},
        {"DELIVERED": 9, "SKIPPED_OPTED_OUT": 1, "NO_MATCHES": 2},
    )
    assert "التقرير الأسبوعي" in report
    assert "لمّاح: ٢" in report          # the product's ONE name (see below)
    assert "٥٥٨ ريال" in report
    assert "قدّم ٣/٤ (٧٥٪)" in report
    assert "موقف الرسائل: ١" in report
    # «$0.40» was a Latin run inside an Arabic line; the money is said in
    # Arabic instead, which is the one cure a digit fold cannot give a WORD
    assert "٠.٤٠ دولار" in report
    assert "$" not in report


def test_week_day_states_tally(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.telegram.console import week_day_states

    t1, _ = two_tenants
    owner_session.execute(sql_text(
        "INSERT INTO tenant_day_states (id, tenant_id, run_date, state, counts,"
        " recorded_at) VALUES (:i, :t, :d, 'DELIVERED', '{}'::jsonb, :r)"),
        {"i": str(uuid.uuid4()), "t": t1, "d": NOW.date().isoformat(),
         "r": NOW})
    owner_session.commit()
    try:
        tally = week_day_states(owner_session, now=NOW)
        assert tally.get("DELIVERED", 0) >= 1
    finally:
        owner_session.execute(sql_text(
            "DELETE FROM tenant_day_states WHERE tenant_id = :t"), {"t": t1})
        owner_session.commit()


# ── «إعادة إرسال»: re-attempting a held-but-undelivered bundle ───────────────


def _job(group: str) -> dict[str, Any]:
    return {
        "group": group,
        "card": {"body": f"وظيفة {group}"},
        "document": {"ref": f"key/{group}.pdf", "filename": "cv.pdf"},
    }


def _seed_held_bundle(
    session: Session, tenant_id: str, *, jobs: list[dict[str, Any]],
    opted_out: bool = False, window_closed: bool = False,
    attempts: int | None = None,
) -> None:
    """A PENDING_WINDOW delivery: the customer tapped nothing / the send
    failed, so the bundle is still claimable. ``window_closed`` reproduces
    the ordinary morning state (template sent, customer has not replied yet);
    ``attempts`` pre-loads the retry counter."""
    import json

    channel_id = str(uuid.uuid4())
    last_inbound = NOW - timedelta(hours=25) if window_closed else NOW
    bundle: dict[str, Any] = {
        "grouped": True, "header": "حزمة اليوم", "jobs": jobs,
        "close": {"gate_passes": len(jobs), "cv_resolved": len(jobs),
                  "cv_failed": 0},
    }
    if attempts is not None:
        bundle["attempts"] = attempts
    session.execute(sql_text(
        "INSERT INTO customer_channels (id, tenant_id, provider, phone_e164,"
        " last_inbound_at, opt_out_at)"
        " VALUES (:i, :t, 'whatsapp', :p, :l, :o)"),
        {"i": channel_id, "t": tenant_id,
         "p": f"+96650{uuid.uuid4().int % 10**7:07d}",
         "l": last_inbound, "o": NOW if opted_out else None})
    session.execute(sql_text(
        "INSERT INTO deliveries (id, tenant_id, channel_id, run_date, status,"
        " bundle) VALUES (:i, :t, :c, :d, 'PENDING_WINDOW', :b)"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "c": channel_id,
         "d": NOW.date().isoformat(), "b": json.dumps(bundle)})
    session.commit()


def _clear_delivery(session: Session, tenant_id: str) -> None:
    for table in ("delivery_messages", "deliveries", "customer_channels",
                  "tenant_day_states", "tenant_job_suppressions",
                  "cost_allocations", "usage_events"):
        session.execute(
            sql_text(f"DELETE FROM {table} WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    session.commit()


def _day_state(session: Session, tenant_id: str) -> str | None:
    row = session.execute(
        sql_text("SELECT state FROM tenant_day_states WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).first()
    return None if row is None else str(row[0])


def _code_of(session: Session, tenant_id: str) -> str:
    return str(session.execute(
        sql_text("SELECT code FROM tenants WHERE id = :t"), {"t": tenant_id}
    ).scalar_one())


def _buttons(outcome: Any) -> list[str]:
    return [b[1] for row in (outcome.keyboard or []) for b in row]


def _fake_wa() -> Any:
    from career.whatsapp.client import FakeWhatsAppClient

    return FakeWhatsAppClient()


def test_resend_button_hidden_without_a_held_bundle(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """No PENDING_WINDOW delivery → the button that could only answer
    "nothing to resend" is never drawn, and the action is refused."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    card = handle_update(
        owner_session, _cbq(ADMIN, f"v1|tenant|{code}"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW, whatsapp_client=_fake_wa(),
    )
    assert f"v1|act|{code}|resend" not in _buttons(card[1])
    refused = handle_update(
        owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        whatsapp_client=_fake_wa(),
    )
    assert [o.kind for o in refused] == ["ack"]


def test_resend_button_hidden_when_no_client_is_injected(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """A read-only console (no whatsapp client) never offers the action."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")])
    try:
        card = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )
        assert f"v1|act|{code}|resend" not in _buttons(card[1])
    finally:
        _clear_delivery(owner_session, t1)


def test_resend_button_hidden_for_opted_out_channel(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """§ opt-out is absolute: a held bundle for a customer who stopped the
    messages is not resendable, so no button and no confirmation."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")], opted_out=True)
    try:
        card = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=_fake_wa(),
        )
        assert f"v1|act|{code}|resend" not in _buttons(card[1])
        refused = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=_fake_wa(),
        )
        assert [o.kind for o in refused] == ["ack"]
    finally:
        _clear_delivery(owner_session, t1)


def test_resend_confirm_flow_delivers_the_held_bundle(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """card → «إعادة إرسال» → confirm card w/ one-shot nonce → the bundle is
    actually re-sent, the reply is the honest COMPLETED one, and the button
    disappears because nothing is held any more."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")])
    wa = _fake_wa()
    try:
        card = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert f"v1|act|{code}|resend" in _buttons(card[1])
        assert "حزمة محفوظة" in card[1].text

        act = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        confirm_btn = act[1].keyboard[0][0][1]
        assert confirm_btn.startswith("v1|confirm|")
        assert not wa.sent                       # confirmation sends nothing

        done = handle_update(
            owner_session, _cbq(ADMIN, confirm_btn), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW, whatsapp_client=wa,
        )
        assert console.RESEND_DONE_AR.format(code=code) in done[1].text
        assert [m.kind for m in wa.sent] == ["text", "text", "document"]
        status = owner_session.execute(sql_text(
            "SELECT status FROM deliveries WHERE tenant_id = :t"),
            {"t": t1}).scalar_one()
        assert status == "COMPLETED"
        assert f"v1|act|{code}|resend" not in _buttons(done[1])

        # the SAME nonce again → refused (one-shot, like pause/resume)
        again = handle_update(
            owner_session, _cbq(ADMIN, confirm_btn), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW, whatsapp_client=wa,
        )
        assert again[0].text == console.ACTION_EXPIRED_AR
    finally:
        _clear_delivery(owner_session, t1)


def test_resend_reply_is_honest_on_partial_delivery(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """One job lands, the other's CV fails → PARTIAL, said plainly."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1"), _job("g2")])
    wa = _fake_wa()
    original = wa.send_document

    def flaky_document(to_phone: str, ref: str, **kw: Any) -> str:
        if ref.endswith("g2.pdf"):
            raise RuntimeError("graph refused the media")
        return original(to_phone, ref, **kw)

    wa.send_document = flaky_document        # type: ignore[method-assign]
    try:
        act = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        done = handle_update(
            owner_session, _cbq(ADMIN, act[1].keyboard[0][0][1]),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert console.RESEND_PARTIAL_AR.format(code=code) in done[1].text
        status = owner_session.execute(sql_text(
            "SELECT status FROM deliveries WHERE tenant_id = :t"),
            {"t": t1}).scalar_one()
        assert status == "PARTIAL"
    finally:
        _clear_delivery(owner_session, t1)


def test_resend_reply_is_honest_when_nothing_lands(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Everything failed again → the bundle stays claimable and the operator
    is told so (never a green ✅)."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")])
    wa = _fake_wa()

    def dead_text(to_phone: str, body: str) -> str:
        raise RuntimeError("graph is down")

    wa.send_text = dead_text                 # type: ignore[method-assign]
    try:
        act = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        done = handle_update(
            owner_session, _cbq(ADMIN, act[1].keyboard[0][0][1]),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert console.RESEND_FAILED_AR.format(code=code) in done[1].text
        assert "✅" not in done[1].text.splitlines()[0]
        status = owner_session.execute(sql_text(
            "SELECT status FROM deliveries WHERE tenant_id = :t"),
            {"t": t1}).scalar_one()
        assert status == "PENDING_WINDOW"     # still claimable, not consumed
        # still offered — the operator may try again
        assert f"v1|act|{code}|resend" in _buttons(done[1])
    finally:
        _clear_delivery(owner_session, t1)


def test_resend_replies_and_confirm_card_are_direction_pure(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Fahad's client scrambles a line that mixes Arabic with Latin/digits:
    every Arabic line we emit must be free of them (the TEN code lives on a
    line of its own)."""
    import re

    import career.telegram.console as console

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")])
    try:
        act = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=_fake_wa(),
        )
        texts = [act[1].text] + [
            template.format(code=code) for template in (
                console.RESEND_DONE_AR, console.RESEND_PARTIAL_AR,
                console.RESEND_FAILED_AR, console.RESEND_NOTHING_AR,
                console.RESEND_EXHAUSTED_AR, console.RESEND_CLOSED_AR,
                console.RESEND_OPTED_OUT_AR,
            )
        ]
        for text in texts:
            assert code in text
            for line in text.splitlines():
                if arabic.search(line):
                    assert not latin_or_digit.search(line), line
    finally:
        _clear_delivery(owner_session, t1)


# ── «رد على العميل»: one free-form operator reply, on the same nonce ─────────


def _seed_channel(
    session: Session, tenant_id: str, *,
    last_inbound_at: datetime | None = NOW, opted_out: bool = False,
) -> str:
    """A WhatsApp channel with no delivery attached — the plain «someone to
    reply to» case."""
    channel_id = str(uuid.uuid4())
    session.execute(sql_text(
        "INSERT INTO customer_channels (id, tenant_id, provider, phone_e164,"
        " last_inbound_at, opt_out_at)"
        " VALUES (:i, :t, 'whatsapp', :p, :l, :o)"),
        {"i": channel_id, "t": tenant_id,
         "p": f"+96650{uuid.uuid4().int % 10**7:07d}",
         "l": last_inbound_at, "o": NOW if opted_out else None})
    session.commit()
    return channel_id


def _open_reply_box(
    session: Session, code: str, *, wa: Any, now: datetime = NOW
) -> list[Any]:
    return handle_update(
        session, _cbq(ADMIN, f"v1|reply|{code}"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=now, whatsapp_client=wa,
    )


def _prompt_text(outcomes: list[Any]) -> str:
    return outcomes[1].text


def test_reply_button_needs_a_channel_and_an_injected_client(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """No channel → nobody to reply to; no client → a read-only console."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    bare = handle_update(
        owner_session, _cbq(ADMIN, f"v1|tenant|{code}"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW, whatsapp_client=_fake_wa(),
    )
    assert f"v1|reply|{code}" not in _buttons(bare[1])
    _seed_channel(owner_session, t1)
    try:
        read_only = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )
        assert f"v1|reply|{code}" not in _buttons(read_only[1])
        with_client = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=_fake_wa(),
        )
        assert f"v1|reply|{code}" in _buttons(with_client[1])
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_is_never_offered_to_someone_who_opted_out(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Opt-out is absolute: no button, and the action refuses out loud."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_channel(owner_session, t1, opted_out=True)
    wa = _fake_wa()
    try:
        card = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert f"v1|reply|{code}" not in _buttons(card[1])
        refused = _open_reply_box(owner_session, code, wa=wa)
        assert refused[1].kind == "edit"          # no reply box is opened
        assert refused[1].force_reply is False
        assert console.REPLY_OPTED_OUT_AR.format(code=code) == _prompt_text(refused)
        assert not wa.sent
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_refuses_honestly_when_the_window_is_closed(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """§08: outside 24h only templates may go out. The operator is told the
    real reason instead of typing into a void — and no nonce is minted."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_channel(owner_session, t1, last_inbound_at=NOW - timedelta(hours=30))
    wa = _fake_wa()
    try:
        card = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        # still offered: the refusal explains, a missing button would not
        assert f"v1|reply|{code}" in _buttons(card[1])
        refused = _open_reply_box(owner_session, code, wa=wa)
        assert refused[1].kind == "edit"
        assert console.REPLY_CLOSED_AR.format(code=code) == _prompt_text(refused)
        assert console._REPLY_MARKER not in _prompt_text(refused)
        assert not wa.sent
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_free_text_reaches_the_customer_and_is_recorded(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """card → «رد على العميل» → force-reply prompt → the operator's typed
    text is sent as-is and logged in delivery_messages like any outbound."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    channel_id = _seed_channel(owner_session, t1)
    phone = owner_session.execute(sql_text(
        "SELECT phone_e164 FROM customer_channels WHERE id = :c"),
        {"c": channel_id}).scalar_one()
    wa = _fake_wa()
    try:
        prompt = _open_reply_box(owner_session, code, wa=wa)
        assert [o.kind for o in prompt] == ["ack", "send"]
        assert prompt[1].force_reply is True       # Telegram opens the box
        assert prompt[1].keyboard is None
        assert console._REPLY_MARKER in prompt[1].text
        assert not wa.sent                         # the prompt sends nothing

        body = "نشتغل على طلبك الآن ونرجع لك خلال ساعة"
        done = handle_update(
            owner_session, _msg(ADMIN, body, reply_to=prompt[1].text),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert [o.kind for o in done] == ["send"]
        assert done[0].text == console.REPLY_SENT_AR.format(code=code)
        assert [(m.kind, m.to_phone, m.body) for m in wa.sent] == [
            ("text", phone, body)
        ]
        # §14: recorded exactly like any other outbound
        row = owner_session.execute(sql_text(
            "SELECT kind, status, wa_message_id, delivery_id FROM"
            " delivery_messages WHERE tenant_id = :t"), {"t": t1}).one()
        assert row[0] == console.REPLY_KIND
        assert row[1] == "sent"
        assert row[2] == wa.sent[0].message_id
        assert row[3] is None                      # not part of a delivery

        # one-shot: replying to the same prompt again does nothing
        again = handle_update(
            owner_session, _msg(ADMIN, "مرة ثانية", reply_to=prompt[1].text),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert again[0].text == console.ACTION_EXPIRED_AR
        assert len(wa.sent) == 1
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_nonce_expires_after_five_minutes(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_channel(owner_session, t1)
    wa = _fake_wa()
    try:
        prompt = _open_reply_box(owner_session, code, wa=wa)
        late = handle_update(
            owner_session, _msg(ADMIN, "متأخر", reply_to=prompt[1].text),
            admin_chat_id=ADMIN, probes=FakeProbes(),
            now=NOW + timedelta(minutes=6), whatsapp_client=wa,
        )
        assert late[0].text == console.ACTION_EXPIRED_AR
        assert not wa.sent
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_rechecks_the_window_at_send_time(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The window may close while the operator types — the send-time check is
    the one that protects the customer."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_channel(owner_session, t1)
    wa = _fake_wa()
    try:
        prompt = _open_reply_box(owner_session, code, wa=wa)
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET last_inbound_at = :l"
            " WHERE tenant_id = :t"),
            {"l": NOW - timedelta(hours=30), "t": t1})
        owner_session.commit()
        out = handle_update(
            owner_session, _msg(ADMIN, "نص", reply_to=prompt[1].text),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert out[0].text == console.REPLY_CLOSED_AR.format(code=code)
        assert not wa.sent
        assert owner_session.execute(sql_text(
            "SELECT count(*) FROM delivery_messages WHERE tenant_id = :t"),
            {"t": t1}).scalar_one() == 0
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_rechecks_opt_out_at_send_time(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_channel(owner_session, t1)
    wa = _fake_wa()
    try:
        prompt = _open_reply_box(owner_session, code, wa=wa)
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET opt_out_at = :o WHERE tenant_id = :t"),
            {"o": NOW, "t": t1})
        owner_session.commit()
        out = handle_update(
            owner_session, _msg(ADMIN, "نص", reply_to=prompt[1].text),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert out[0].text == console.REPLY_OPTED_OUT_AR.format(code=code)
        assert not wa.sent
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_refuses_empty_and_command_text(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """A fat-fingered /start must never reach a paying customer."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_channel(owner_session, t1)
    wa = _fake_wa()
    try:
        for body in ("   ", "/start"):
            prompt = _open_reply_box(owner_session, code, wa=wa)
            out = handle_update(
                owner_session, _msg(ADMIN, body, reply_to=prompt[1].text),
                admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
                whatsapp_client=wa,
            )
            assert out[0].text == console.REPLY_EMPTY_AR.format(code=code)
        prompt = _open_reply_box(owner_session, code, wa=wa)
        long_out = handle_update(
            owner_session,
            _msg(ADMIN, "ا" * (console.REPLY_MAX_CHARS + 1),
                 reply_to=prompt[1].text),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert long_out[0].text == console.REPLY_TOO_LONG_AR.format(code=code)
        assert not wa.sent
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_reports_a_refused_send_and_records_nothing(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """WhatsApp refuses → the operator is told, and no phantom row is left in
    the outbound log (never a green ✅ for a message that did not go)."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_channel(owner_session, t1)
    wa = _fake_wa()

    def dead_text(to_phone: str, body: str) -> str:
        raise RuntimeError("graph refused")

    wa.send_text = dead_text                 # type: ignore[method-assign]
    try:
        prompt = _open_reply_box(owner_session, code, wa=wa)
        out = handle_update(
            owner_session, _msg(ADMIN, "نص", reply_to=prompt[1].text),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert out[0].text == console.REPLY_FAILED_AR.format(code=code)
        assert "✅" not in out[0].text
        assert owner_session.execute(sql_text(
            "SELECT count(*) FROM delivery_messages WHERE tenant_id = :t"),
            {"t": t1}).scalar_one() == 0
    finally:
        _clear_delivery(owner_session, t1)


def test_reply_never_leaks_the_phone_or_the_body_to_the_admin_channel(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """§15.13: the watchtower shows TEN codes only. Neither the prompt nor
    the confirmation may carry the number we sent to."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    channel_id = _seed_channel(owner_session, t1)
    phone = owner_session.execute(sql_text(
        "SELECT phone_e164 FROM customer_channels WHERE id = :c"),
        {"c": channel_id}).scalar_one()
    wa = _fake_wa()
    try:
        prompt = _open_reply_box(owner_session, code, wa=wa)
        body = "رقمي الخاص وتفاصيل لا يجب أن تعود للقناة الإدارية"
        done = handle_update(
            owner_session, _msg(ADMIN, body, reply_to=prompt[1].text),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        for text in (prompt[1].text, done[0].text):
            assert phone not in text
            assert "+966" not in text
            assert code in text
        assert body not in done[0].text      # the body is never echoed back
    finally:
        _clear_delivery(owner_session, t1)


def test_operator_text_without_a_prompt_is_still_just_the_menu(
    owner_session: Session
) -> None:
    """Only a reply to OUR prompt is treated as a customer reply."""
    out = handle_update(
        owner_session, _msg(ADMIN, "نص عادي", reply_to="رسالة قديمة بلا علامة"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        whatsapp_client=_fake_wa(),
    )
    assert len(out) == 1
    assert "برج المراقبة" in out[0].text


def test_reply_strings_and_prompt_are_direction_pure(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Same rule as the resend replies: no Latin/digits inside an Arabic
    line — the TEN code and the nonce marker live on lines of their own."""
    import re

    import career.telegram.console as console

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_channel(owner_session, t1)
    try:
        prompt = _open_reply_box(owner_session, code, wa=_fake_wa())
        texts = [prompt[1].text] + [
            template.format(code=code) for template in (
                console.REPLY_SENT_AR, console.REPLY_FAILED_AR,
                console.REPLY_CLOSED_AR, console.REPLY_OPTED_OUT_AR,
                console.REPLY_NO_CHANNEL_AR, console.REPLY_EMPTY_AR,
                console.REPLY_TOO_LONG_AR,
            )
        ]
        for text in texts:
            assert code in text
            for line in text.splitlines():
                if arabic.search(line):
                    assert not latin_or_digit.search(line), line
    finally:
        _clear_delivery(owner_session, t1)


def test_today_screen_lines_are_direction_pure_where_arabic() -> None:
    """The honest «no run today» lines keep the date and any unknown status
    on their own lines."""
    import re

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")

    text, _ = views.render_today(
        NOW.date(), None, [],
        {"run_date": "2026-07-14", "status": "completed"},
    )
    for line in text.splitlines():
        if arabic.search(line):
            assert not latin_or_digit.search(line), line


# ── the delivery day must close with the truth, whoever closed it ────────────

_JOB_URL = "https://careers.example.test/j/77"


def test_resend_button_is_hidden_while_the_window_is_closed(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """AUDIT: a held bundle is held BECAUSE the window is shut, and واتساب
    rejects every free-form message outside it — so the button used to be
    offered in the one state it could never serve, and each doomed tap spent
    one of three attempts. The card now says what is held and why it waits,
    and the action is refused even if a stale callback arrives."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")], window_closed=True)
    try:
        card = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=_fake_wa(),
        )
        assert f"v1|act|{code}|resend" not in _buttons(card[1])
        assert "حزمة محفوظة" in card[1].text        # the fact is not hidden
        assert "النافذة مقفولة" in card[1].text     # …and the reason is given
        refused = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=_fake_wa(),
        )
        assert [o.kind for o in refused] == ["ack"]
    finally:
        _clear_delivery(owner_session, t1)


def test_a_window_that_closes_mid_confirm_refuses_and_costs_nothing(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The window can shut between minting the confirm card and the tap —
    re-checked at SEND time, refused out loud, and the attempt budget is
    untouched so the bundle is still there when the customer replies."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")])
    wa = _fake_wa()
    try:
        act = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET last_inbound_at = :l"
            " WHERE tenant_id = :t"),
            {"l": NOW - timedelta(hours=25), "t": t1})
        owner_session.commit()

        done = handle_update(
            owner_session, _cbq(ADMIN, act[1].keyboard[0][0][1]),
            admin_chat_id=ADMIN, probes=FakeProbes(),
            now=NOW + timedelta(minutes=1), whatsapp_client=wa,
        )
        assert console.RESEND_CLOSED_AR.format(code=code) in done[1].text
        assert not wa.sent
        row = owner_session.execute(sql_text(
            "SELECT status, bundle FROM deliveries WHERE tenant_id = :t"),
            {"t": t1}).one()
        assert row[0] == "PENDING_WINDOW"
        assert "attempts" not in row[1]           # no attempt was consumed
    finally:
        _clear_delivery(owner_session, t1)


def test_a_landed_console_resend_closes_the_day_and_suppresses(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """AUDIT §15.12: the operator's resend used to deliver card+CV and leave
    the run_date with no state row at all — permanently, since the delivery
    is no longer PENDING_WINDOW — and wrote no suppression, so the same job
    and the same cached PDF were sent again the next night."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job(_JOB_URL)])
    wa = _fake_wa()
    try:
        assert _day_state(owner_session, t1) is None
        act = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        handle_update(
            owner_session, _cbq(ADMIN, act[1].keyboard[0][0][1]),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert _day_state(owner_session, t1) == "DELIVERED"
        suppressed = owner_session.execute(sql_text(
            "SELECT count(*) FROM tenant_job_suppressions WHERE tenant_id = :t"),
            {"t": t1}).scalar_one()
        assert suppressed == 1
    finally:
        _clear_delivery(owner_session, t1)


def test_an_exhausted_resend_is_never_reported_as_partly_arrived(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """AUDIT: the third zero-delivery attempt stamps PARTIAL with an EMPTY
    delivered list. The operator was told «وصل جزء من الحزمة» and stopped
    investigating while nothing at all had landed — and the burned day got no
    state. Now the reply reports the truth and the day closes WHATSAPP_FAILED.
    """
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    # two attempts already spent — this tap is the last one
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")], attempts=2)
    wa = _fake_wa()

    def dead_text(to_phone: str, body: str) -> str:
        raise RuntimeError("graph is down")

    wa.send_text = dead_text                 # type: ignore[method-assign]
    try:
        act = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        done = handle_update(
            owner_session, _cbq(ADMIN, act[1].keyboard[0][0][1]),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            whatsapp_client=wa,
        )
        assert console.RESEND_EXHAUSTED_AR.format(code=code) in done[1].text
        assert "وصل جزء" not in done[1].text
        status = owner_session.execute(sql_text(
            "SELECT status FROM deliveries WHERE tenant_id = :t"),
            {"t": t1}).scalar_one()
        assert status == "PARTIAL"
        # the card beneath the reply must not contradict it either
        assert "لم يصل منها شيء" in done[1].text
        assert _day_state(owner_session, t1) == "WHATSAPP_FAILED"
    finally:
        _clear_delivery(owner_session, t1)


def test_a_resend_that_lands_closes_the_day_and_suppresses(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The operator's resend went through the same delivery machinery as the
    customer's tap but skipped everything that HAPPENS after it: the day was
    never closed (§15.12 — a tenant with no honest state) and no suppression
    was written, so the very same jobs stayed eligible and would be sent
    again the next day. The operator fixed a delivery and silently created a
    duplicate."""
    from sqlalchemy import text as _sql

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    _seed_held_bundle(owner_session, t1, jobs=[_job("g1")])
    wa = _fake_wa()

    before = owner_session.execute(_sql(
        "SELECT count(*) FROM tenant_day_states WHERE tenant_id = :t"),
        {"t": str(t1)}).scalar_one()

    handle_update(owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
                  admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
                  whatsapp_client=wa)
    nonce_btn = handle_update(
        owner_session, _cbq(ADMIN, f"v1|act|{code}|resend"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        whatsapp_client=wa,
    )[1].keyboard[0][0][1]
    handle_update(owner_session, _cbq(ADMIN, nonce_btn),
                  admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
                  whatsapp_client=wa)
    owner_session.commit()

    after = owner_session.execute(_sql(
        "SELECT count(*) FROM tenant_day_states WHERE tenant_id = :t"),
        {"t": str(t1)}).scalar_one()
    assert after == before + 1, "a landed resend must close the tenant's day"

    state = owner_session.execute(_sql(
        "SELECT state FROM tenant_day_states WHERE tenant_id = :t"
        " ORDER BY run_date DESC LIMIT 1"), {"t": str(t1)}).scalar_one()
    assert state in ("DELIVERED", "PARTIAL_DELIVERY"), state


def test_the_business_screen_shows_the_search_allowance(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The ceiling the operator could not see. Money was on the screen and the
    SEARCH allowance was not — and it is the allowance that stops discovery
    for every customer at once. It is consumed by career PATHS, not by
    customers, so spend rising tells you nothing about how close it is.

    The arithmetic matters: a family's credit count is written onto every
    tenant that family serves (the cost is split, the count is not), so a
    naive sum multiplies by the audience."""
    import uuid as _uuid

    from sqlalchemy import text as _sql

    import career.telegram.console as console

    t1, t2 = two_tenants
    run = _uuid.uuid4()
    # ONE family of 8 credits serving TWO tenants: the count is written on
    # each row, the COST is split between them.
    for tid in (t1, t2):
        owner_session.execute(_sql(
            "INSERT INTO usage_events (id, tenant_id, kind, run_id,"
            " input_tokens, cost_usd, occurred_at)"
            " VALUES (:i, :t, 'search_api', :r, 8, 0.016, now())"),
            {"i": str(_uuid.uuid4()), "t": str(tid), "r": str(run)})
    # …and TWO more families in the SAME run, 3 and 2 credits, one tenant
    # each. Taking the max per run reported 8 of a real 13 — only the biggest
    # family — which is how the operator saw roughly half the consumption of
    # the one limit that halts discovery for everybody at once.
    for credits in (3, 2):
        owner_session.execute(_sql(
            "INSERT INTO usage_events (id, tenant_id, kind, run_id,"
            " input_tokens, cost_usd, occurred_at)"
            " VALUES (:i, :t, 'search_api', :r, :c, :u, now())"),
            {"i": str(_uuid.uuid4()), "t": str(t1), "r": str(run),
             "c": credits, "u": credits * 0.004})
    owner_session.commit()
    try:
        data = console._business_data(owner_session, "30", now=NOW)
        assert data["searches_this_month"] == 13, (
            "8 + 3 + 2 credits were billed once each, not once per tenant "
            "and not only the largest family"
        )
        text, _ = views.render_business("30", data)
        assert "بحثات هذا الشهر" in text
        assert "بحثات هذا الشهر: ١٣ من ١٠٠٠٠" in text
    finally:
        owner_session.rollback()
        owner_session.execute(_sql(
            "DELETE FROM usage_events WHERE run_id = :r"), {"r": str(run)})
        owner_session.commit()


# ── «إصدار رابط تفعيل»: the operator-facing half of the activation fallback ──


def _seed_unclaimed_order(session: Session, tenant_id: str) -> None:
    """A paid order nobody has claimed yet — the ONLY state that can be
    handed an activation link."""
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id) VALUES (:i, :t, 'professional', 'PAID_UNCLAIMED',"
        " :o)"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "o": f"O-{uuid.uuid4()}"})
    session.commit()


def _tokens_of(session: Session, tenant_id: str) -> list[Any]:
    return list(session.execute(sql_text(
        "SELECT id, used_at FROM activation_tokens WHERE tenant_id = :t"
        " ORDER BY created_at"), {"t": tenant_id}).all())


def _link_in(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("https://wa.me/"):
            return line
    raise AssertionError(f"no activation link in the reply:\n{text}")


def test_the_link_button_appears_only_for_an_order_still_waiting(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The capability the operator could not reach. It is offered exactly
    where it can do something — a paid order nobody has claimed — and not on
    the card of a customer who is already live."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    try:
        _seed_subscription(owner_session, t1, "ACTIVE")
        out = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )
        assert f"v1|act|{code}|issue_link" not in _buttons(out[1])
        _drop_subscriptions(owner_session, t1)

        _seed_unclaimed_order(owner_session, t1)
        out = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tenant|{code}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )
        assert f"v1|act|{code}|issue_link" in _buttons(out[1])
    finally:
        _drop_subscriptions(owner_session, t1)


def test_issuing_a_link_says_it_is_short_lived_single_use_and_rotating(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """An operator who does not know the link expires — and that issuing a
    new one retires the old — reads «رمز التفعيل مستخدم مسبقًا» as a bug in
    the product rather than as the fail-closed answer it is."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    try:
        _seed_unclaimed_order(owner_session, t1)
        done = _confirm(owner_session, code, "issue_link")
        text = done[1].text
        assert _link_in(text).startswith("https://wa.me/")
        assert code in text
        assert "٦٠ دقيقة" in text            # the real TTL, in Arabic digits
        assert "لمرة واحدة" in text
        assert "أُلغي" in text                # a new link retires the old one
        assert "رمز التفعيل مستخدم مسبقًا" in text
        # and it really minted one live token for the waiting order
        tokens = _tokens_of(owner_session, t1)
        assert len(tokens) == 1
        assert tokens[0][1] is None
    finally:
        _drop_subscriptions(owner_session, t1)


def test_issuing_a_second_link_retires_the_first(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Rotation is what makes on-demand issuance safe, and the reply promises
    it — so the promise is pinned here: the old token is retired, and exactly
    one live token survives (two would break activation for that buyer)."""
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    try:
        _seed_unclaimed_order(owner_session, t1)
        first = _link_in(_confirm(owner_session, code, "issue_link")[1].text)
        second = _link_in(_confirm(owner_session, code, "issue_link")[1].text)
        assert first != second
        tokens = _tokens_of(owner_session, t1)
        assert len(tokens) == 2
        assert tokens[0][1] is not None      # retired as the new one was cut
        assert tokens[1][1] is None
    finally:
        _drop_subscriptions(owner_session, t1)


def test_an_already_claimed_order_is_refused_out_loud(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The operator taps because a buyer is waiting. A silent no-op sends
    them hunting for a fault that is not there, so the refusal is spoken and
    names the reason — and nothing is minted."""
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    try:
        _seed_subscription(owner_session, t1, "ACTIVE")
        done = _confirm(owner_session, code, "issue_link")
        assert done[1].text.startswith(
            console.LINK_ALREADY_ACTIVATED_AR.format(code=code)
        )
        assert "https://wa.me/" not in done[1].text
        assert _tokens_of(owner_session, t1) == []
    finally:
        _drop_subscriptions(owner_session, t1)


def test_a_tenant_with_no_order_at_all_is_refused_out_loud(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    import career.telegram.console as console

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    done = _confirm(owner_session, code, "issue_link")
    assert done[1].text.startswith(console.LINK_NO_ORDER_AR.format(code=code))
    assert "https://wa.me/" not in done[1].text


def test_the_link_goes_to_the_operator_alone_and_never_to_a_log(
    owner_session: Session, two_tenants: tuple[str, str], caplog: Any
) -> None:
    """The link IS the credential: whoever opens it binds their phone to a
    paid subscription. The previous design posted it to this channel on every
    sale and it sat there live for seven days per order — and the secret
    filter cannot cover for that, because the token rides in a `text=` query
    parameter and matches no key name and no provider token shape.

    So it may exist in exactly one place: the answer to the button the
    operator just pressed, in their own chat. Never a log line, at any level.
    """
    import logging

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    try:
        _seed_unclaimed_order(owner_session, t1)
        with caplog.at_level(logging.DEBUG):
            done = _confirm(owner_session, code, "issue_link")
        link = _link_in(done[1].text)
        raw_token = link.rsplit("%20", 1)[-1]
        assert len(raw_token) >= 20

        carriers = [o for o in done if link in (o.text or "")]
        assert len(carriers) == 1                    # exactly one, and it is
        assert carriers[0] is done[1]                # the operator's screen
        for record in caplog.records:
            assert link not in record.getMessage()
            assert raw_token not in record.getMessage()
            assert raw_token not in str(record.args or "")
    finally:
        _drop_subscriptions(owner_session, t1)


def test_link_replies_are_direction_pure(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """§16: no Latin or digits inside an Arabic line. The TEN code and the
    link each stand alone — a mixed line arrives scrambled on the operator's
    client, and a scrambled link is an unusable one."""
    import re

    import career.telegram.console as console

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    try:
        _seed_unclaimed_order(owner_session, t1)
        issued = _confirm(owner_session, code, "issue_link")[1].text
        _drop_subscriptions(owner_session, t1)
        _seed_subscription(owner_session, t1, "ACTIVE")
        refused = _confirm(owner_session, code, "issue_link")[1].text
        act = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|issue_link"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )[1].text
        # only the ANSWER — the tenant card re-rendered under it belongs to
        # views and is pinned by its own tests
        for text in (issued.split("\n\n")[0], refused.split("\n\n")[0], act,
                     console.LINK_NO_ORDER_AR.format(code=code)):
            for line in text.splitlines():
                if arabic.search(line):
                    assert not latin_or_digit.search(line), line
    finally:
        _drop_subscriptions(owner_session, t1)


# ── open support tickets: the first reader `support_events` has ever had ─────


def _seed_ticket(
    session: Session, tenant_id: str, channel_id: str, *,
    kind: str = "support_request", age_hours: int = 1,
    inbound_message_id: str | None = None,
) -> str:
    """A ticket exactly as the product makes one: «open», never anything else.

    The status used to be a parameter, and one test set it to «resolved» —
    a value NO production path could produce, because nothing in the codebase
    ever wrote that column a second time. The screen it was proving («resolved
    tickets disappear») therefore proved nothing about the running system: it
    proved that a state the system could not reach would have behaved. The
    close action is now real, so the tests reach «resolved» the way the
    operator does, by pressing the button.
    """
    ticket_id = str(uuid.uuid4())
    session.execute(sql_text(
        "INSERT INTO support_events (id, tenant_id, channel_id, kind, status,"
        " inbound_message_id, created_at)"
        " VALUES (:i, :t, :c, :k, 'open', :m, :a)"),
        {"i": ticket_id, "t": tenant_id, "c": channel_id, "k": kind,
         "m": inbound_message_id, "a": NOW - timedelta(hours=age_hours)})
    session.commit()
    return ticket_id


def _close_ticket(session: Session, ticket_id: str, **kw: Any) -> str:
    """Drive the operator's two taps — confirm card, then confirmation — and
    return the text of the screen that answers. Nothing here writes SQL."""
    card = handle_update(
        session, _cbq(ADMIN, f"v1|tclose|{ticket_id}"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW, **kw,
    )[1]
    nonces = [data for row in card.keyboard for _label, data in row
              if data.startswith("v1|tdone|")]
    assert len(nonces) == 1, card.keyboard
    return handle_update(
        session, _cbq(ADMIN, nonces[0]), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW, **kw,
    )[1].text


def _drop_tickets(session: Session, *tenant_ids: str) -> None:
    session.rollback()
    for tenant_id in tenant_ids:
        session.execute(sql_text(
            "DELETE FROM support_events WHERE tenant_id = :t"), {"t": tenant_id})
    session.commit()


def _tickets(session: Session, page: int | None = None, **kw: Any) -> Any:
    """The tickets screen as the operator reaches it.

    `page=None` is the bare `v1|tickets` every menu button and «إلغاء» in the
    file already sends — kept as the default so these tests keep proving that
    the un-paged callback still means «the first page».
    """
    data = "v1|tickets" if page is None else f"v1|tickets|{page}"
    return handle_update(
        session, _cbq(ADMIN, data), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW, **kw,
    )[1]


def _listed(outcome: Any) -> list[str]:
    """The ticket ids this screen is actually offering to close.

    Read off the close buttons rather than off the text, because the ids are
    what the buttons carry and the body deliberately prints TEN codes only —
    with two tenants and fifteen tickets, the text cannot tell one row from
    another and the keyboard can.
    """
    return [
        data.split("|")[2] for row in outcome.keyboard for _label, data in row
        if data.startswith("v1|tclose|")
    ]


def test_open_tickets_are_visible_with_their_age_and_whose_they_are(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """`support_events` has been written since C4 and read by nobody: the
    operator was paged once, at the moment it happened, and after that the
    ticket existed only in the database. Oldest first, because age is the
    only priority signal this screen has."""
    t1, t2 = two_tenants
    code1, code2 = _code_of(owner_session, t1), _code_of(owner_session, t2)
    channel1 = _seed_channel(owner_session, t1)
    channel2 = _seed_channel(owner_session, t2)
    try:
        _seed_ticket(owner_session, t2, channel2, age_hours=2)
        _seed_ticket(owner_session, t1, channel1,
                     kind="funnel_consent_stuck", age_hours=50)
        text = _tickets(owner_session).text
        assert "طلب التواصل مع الدعم" in text
        assert "متعثّر عند بوابة الموافقة" in text
        assert "منذ ٢ ساعة" in text
        assert "منذ ٢ يوم" in text
        # oldest first — the customer who has been waiting longest is on top
        assert text.index(code1) < text.index(code2)
    finally:
        _drop_tickets(owner_session, t1, t2)
        _clear_delivery(owner_session, t1)
        _clear_delivery(owner_session, t2)


def test_a_closed_ticket_leaves_the_screen_and_the_empty_state_is_explicit(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """«لا توجد تذاكر مفتوحة» is a fact the operator can act on; a blank
    screen is one they cannot tell apart from a broken one.

    The ticket now reaches «resolved» through the console, which is the only
    path that exists — before the close action there was none at all, and this
    test reached it with an INSERT.
    """
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    channel = _seed_channel(owner_session, t1)
    try:
        ticket = _seed_ticket(owner_session, t1, channel)
        assert code in _tickets(owner_session).text

        answer = _close_ticket(owner_session, ticket)
        assert "أغلقنا التذكرة" in answer
        assert code not in answer.split("\n\n", 1)[1]
        assert "لا توجد تذاكر مفتوحة" in answer

        text = _tickets(owner_session).text
        assert code not in text
        assert "لا توجد تذاكر مفتوحة" in text
    finally:
        _drop_tickets(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_closing_a_ticket_records_when_it_was_dealt_with(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """`resolved_at` has been on the model since C4 with nothing ever writing
    it. «When was this dealt with» is the first question anyone asks of a
    closed ticket, and an SLA cannot be measured from a column nobody fills."""
    t1, _ = two_tenants
    channel = _seed_channel(owner_session, t1)
    try:
        ticket = _seed_ticket(owner_session, t1, channel)
        _close_ticket(owner_session, ticket)
        row = owner_session.execute(sql_text(
            "SELECT status, resolved_at FROM support_events WHERE id = :i"),
            {"i": ticket}).one()
        assert row.status == "resolved"
        assert row.resolved_at == NOW
    finally:
        _drop_tickets(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_the_eleventh_ticket_is_reachable_at_all(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """THE FREEZE. The screen filters «open», orders oldest-first and cuts at
    ten — and nothing in the product ever moved a ticket out of «open». So
    from the eleventh ticket onward the operator saw the same ten forever and
    every later «دعم» was invisible behind them, on a table whose entire
    purpose is «a paying customer asked for a human». Closing the oldest is
    what lets the next one through, so it has to be possible from here."""
    t1, t2 = two_tenants
    code2 = _code_of(owner_session, t2)
    channel1 = _seed_channel(owner_session, t1)
    channel2 = _seed_channel(owner_session, t2)
    try:
        oldest = [
            _seed_ticket(owner_session, t1, channel1, age_hours=100 - n)
            for n in range(console.TICKETS_PAGE)
        ]
        _seed_ticket(owner_session, t2, channel2, age_hours=1)  # the eleventh

        text = _tickets(owner_session).text
        assert code2 not in text                       # buried, and stuck
        assert "من أصل ١١" in text

        for ticket in oldest[:1]:
            _close_ticket(owner_session, ticket)
        assert code2 in _tickets(owner_session).text   # the queue moves again
    finally:
        _drop_tickets(owner_session, t1, t2)
        _clear_delivery(owner_session, t1)
        _clear_delivery(owner_session, t2)


def test_the_eleventh_ticket_is_reachable_without_closing_anybodys_ticket(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The freeze above was fixed with the wrong key, and this is the right one.

    Closing the oldest DOES let the eleventh through — and closing is the
    operator's claim that he dealt with a human, the one claim this whole file
    refuses to let anything make on his behalf. A screen whose only route to
    the next ticket is that claim is a screen that asks him to make it falsely
    about a customer he has not answered, in order to look at his own queue.

    And it is not a corner: RELEASED tickets stay listed, oldest-first, so ten
    tickets he could not honestly close in 48 hours freeze the list exactly as
    it was before the button existed — with the newest ticket, the customer
    writing into silence right now, behind them.
    """
    t1, t2 = two_tenants
    code2 = _code_of(owner_session, t2)
    channel1 = _seed_channel(owner_session, t1)
    channel2 = _seed_channel(owner_session, t2)
    try:
        oldest = [
            _seed_ticket(owner_session, t1, channel1, age_hours=100 - n)
            for n in range(console.TICKETS_PAGE)
        ]
        eleventh = _seed_ticket(owner_session, t2, channel2, age_hours=1)

        first = _tickets(owner_session)
        assert code2 not in first.text
        assert any(data == "v1|tickets|1"
                   for row in first.keyboard for _label, data in row)

        second = _tickets(owner_session, page=1)
        assert _listed(second) == [eleventh]
        assert code2 in second.text
        assert "صفحة ٢ من ٢" in second.text
        # and NOTHING was claimed about a human to get here. The ten oldest
        # are past TICKET_FORGOTTEN_AFTER, so the sweep on the operator's own
        # tap has RELEASED them — which is the freeze in its worst form (ten
        # rows he could not honestly close, permanently at the top) and still
        # not a claim that anybody was dealt with.
        assert all(_ticket_status(owner_session, t) in support.QUEUED_STATUSES
                   for t in [*oldest, eleventh])
    finally:
        _drop_tickets(owner_session, t1, t2)
        _clear_delivery(owner_session, t1)
        _clear_delivery(owner_session, t2)


def test_closing_a_ticket_from_a_later_page_redraws_that_page(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Sending him back to page one after every close would rebuild the same
    habit one level up: the queue drainable only from the top, and the work he
    was doing on page two lost every time he finishes a piece of it."""
    t1, _t2 = two_tenants
    channel1 = _seed_channel(owner_session, t1)
    try:
        tickets = [
            _seed_ticket(owner_session, t1, channel1, age_hours=100 - n)
            for n in range(console.TICKETS_PAGE + 5)
        ]
        second = _tickets(owner_session, page=1)
        assert _listed(second) == tickets[console.TICKETS_PAGE:]

        answer = _close_ticket(owner_session, f"{tickets[10]}|1")

        assert console.TICKET_CLOSED_AR.split("\n")[0] in answer
        assert "صفحة ٢ من ٢" in answer            # still where he was
        assert _ticket_status(owner_session, tickets[10]) == support.RESOLVED
    finally:
        _drop_tickets(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_a_page_that_emptied_underneath_him_lands_on_the_last_one(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """«التالي» tapped on a screen drawn before somebody drained the queue —
    or a close that took the last ticket off the last page. An empty customers
    page is merely empty; an empty TICKETS page would print «🟢 لا توجد تذاكر
    مفتوحة» over a queue that still has people waiting in it, which is the one
    sentence this screen may never say untruthfully."""
    t1, _t2 = two_tenants
    channel1 = _seed_channel(owner_session, t1)
    try:
        for n in range(console.TICKETS_PAGE + 1):
            _seed_ticket(owner_session, t1, channel1, age_hours=100 - n)

        far_past_the_end = _tickets(owner_session, page=9)

        assert console.TICKETS_NONE_AR not in far_past_the_end.text
        assert "صفحة ٢ من ٢" in far_past_the_end.text
        assert len(_listed(far_past_the_end)) == 1
    finally:
        _drop_tickets(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_an_empty_queue_still_says_so_and_offers_no_page_buttons(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The other direction of the clamp: with nothing to show, the screen has
    to keep saying the good news, and a «التالي» to nowhere would be a button
    that answers «انتهت صلاحية الزر» — a broken control on the watchtower."""
    t1, _t2 = two_tenants
    try:
        empty = _tickets(owner_session)
        assert console.TICKETS_NONE_AR in empty.text
        assert "صفحة" not in empty.text
        assert not [data for row in empty.keyboard for _label, data in row
                    if data in ("v1|tickets|1", "v1|tickets|-1")]
    finally:
        _drop_tickets(owner_session, t1)


def test_a_ticket_cannot_be_closed_twice_or_by_a_stale_button(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Same one-shot nonce as every other mutating action: it is consumed by
    the first tap, and a second tap on the card WhatsApp/Telegram keeps
    tappable forever gets «expired», never a second silent write."""
    t1, _ = two_tenants
    channel = _seed_channel(owner_session, t1)
    try:
        ticket = _seed_ticket(owner_session, t1, channel)
        card = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tclose|{ticket}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )[1]
        nonce = [d for row in card.keyboard for _l, d in row
                 if d.startswith("v1|tdone|")][0]
        first = handle_update(
            owner_session, _cbq(ADMIN, nonce), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )[1]
        assert "أغلقنا التذكرة" in first.text

        second = handle_update(
            owner_session, _cbq(ADMIN, nonce), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        assert second[0].kind == "ack"
        assert second[0].text == console.ACTION_EXPIRED_AR

        # and a nonce older than its five minutes is refused the same way
        card2 = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tclose|{ticket}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )[1]
        nonce2 = [d for row in card2.keyboard for _l, d in row
                  if d.startswith("v1|tdone|")][0]
        late = handle_update(
            owner_session, _cbq(ADMIN, nonce2), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW + timedelta(minutes=6),
        )
        assert late[0].text == console.ACTION_EXPIRED_AR
    finally:
        _drop_tickets(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_closing_a_ticket_takes_the_mute_off_the_customers_line(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """THE POINT OF CLOSING, and the half `release_forgotten_tickets` refuses
    to make.

    Three separate dedupes — the لمّاح+ direct line, the funnel's consent
    stall and the paid-while-opted-out alert — all key on «status = open», so
    an open ticket does not merely sit in a queue: it SILENCES that customer's
    channel until something moves the row. The 48-hour sweep is the safety
    net and says out loud that it is not a closure. The button is the real
    exit, and «the operator's tap unmutes the line» is a claim that spans the
    console and a module it does not own, so it is asserted end to end here:
    if `_run_ticket_close` ever wrote some other word than the one the dedupes
    read, every test above would still pass and the customer would stay muted
    with his ticket off the screen — the original silence, made permanent.
    """
    t1, _ = two_tenants
    channel = _seed_channel(owner_session, t1)
    _seed_subscription(owner_session, t1, "ACTIVE", plan="executive")
    try:
        ticket = _seed_ticket(owner_session, t1, channel,
                              kind="executive_direct_message")
        # before: his next message reaches nobody
        assert _direct_line(owner_session, t1, channel) == "already_open"
        owner_session.rollback()

        assert "أغلقنا التذكرة" in _close_ticket(owner_session, ticket)
        assert _ticket_status(owner_session, ticket) == support.RESOLVED

        # after: the same message raises a ticket of its own — with its own
        # age, so the queue says how long THIS person has waited, not how long
        # ago the one the operator already dealt with arrived.
        assert _direct_line(owner_session, t1, channel) == "escalated"
        owner_session.commit()
        fresh = owner_session.execute(sql_text(
            "SELECT id FROM support_events WHERE tenant_id = :t"
            " AND status = 'open'"), {"t": t1}).scalars().all()
        assert len(fresh) == 1 and str(fresh[0]) != ticket
    finally:
        _drop_tickets(owner_session, t1)
        _drop_subscriptions(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_two_confirm_cards_for_one_ticket_close_it_once_and_say_so(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The one-shot nonce proves a BUTTON cannot be tapped twice; it says
    nothing about the write, because two taps on «إغلاق ١» mint two different
    nonces and both are live for five minutes. At fifty tickets that is the
    ordinary case, not an exotic one: the queue is open on the phone and on
    the laptop, and the operator closes the same ticket from whichever is in
    his hand.

    So the second confirmation must refuse OUT LOUD — «مغلقة أصلًا» — and must
    not restamp `resolved_at`. The stamp answers «when was this human dealt
    with», and a stamp that moves to whenever a stale card was last tapped is
    a worse answer than none: it reads as a fresh closure of a ticket nobody
    touched since morning. Silence here would be the incident this file keeps
    re-learning, in its cheapest form — a ✅ over a write that did not happen.
    """
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    channel = _seed_channel(owner_session, t1)
    try:
        ticket = _seed_ticket(owner_session, t1, channel)
        nonces = []
        for _ in range(2):     # two screens open at once
            card = handle_update(
                owner_session, _cbq(ADMIN, f"v1|tclose|{ticket}"),
                admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
            )[1]
            nonces.append([d for row in card.keyboard for _l, d in row
                           if d.startswith("v1|tdone|")][0])
        assert nonces[0] != nonces[1]

        first = handle_update(
            owner_session, _cbq(ADMIN, nonces[0]), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )[1]
        assert console.TICKET_CLOSED_AR.format(code=code) in first.text

        # the other card, still live, three minutes later
        later = NOW + timedelta(minutes=3)
        second = handle_update(
            owner_session, _cbq(ADMIN, nonces[1]), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=later,
        )[1]
        assert console.TICKET_ALREADY_AR.format(code=code) in second.text
        # …and the refusal is a refusal: the record still says when it was
        # actually dealt with, and the queue is not lying about a second one
        assert console.TICKETS_NONE_AR in second.text
        row = owner_session.execute(sql_text(
            "SELECT status, resolved_at FROM support_events WHERE id = :i"),
            {"i": ticket}).one()
        assert row.status == support.RESOLVED
        assert row.resolved_at == NOW
    finally:
        _drop_tickets(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_a_ticket_that_vanishes_between_the_card_and_the_confirm_says_so(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The last close branch, and the only one nothing reached: the ticket is
    gone by the time the confirmation lands — its customer's channel purged,
    or the row removed underneath. The operator gets a sentence and a redrawn
    queue, never a traceback and never a ✅ over nothing.

    ``expunge_all`` models what production already is: ``run_admin_bot``
    opens a FRESH ``Session`` per update, so the confirmation tap never
    inherits the card tap's identity map.
    """
    t1, _ = two_tenants
    channel = _seed_channel(owner_session, t1)
    try:
        ticket = _seed_ticket(owner_session, t1, channel)
        card = handle_update(
            owner_session, _cbq(ADMIN, f"v1|tclose|{ticket}"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
        )[1]
        nonce = [d for row in card.keyboard for _l, d in row
                 if d.startswith("v1|tdone|")][0]

        _drop_tickets(owner_session, t1)
        owner_session.expunge_all()

        answer = handle_update(
            owner_session, _cbq(ADMIN, nonce), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )[1]
        assert console.TICKET_GONE_AR in answer.text
        assert console.TICKETS_NONE_AR in answer.text
    finally:
        _drop_tickets(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_an_unknown_ticket_button_answers_instead_of_raising(
    owner_session: Session
) -> None:
    """A ticket deleted, or a button from a screen two days old. The tap gets
    an honest ack — never a traceback through the barrier."""
    gone = handle_update(
        owner_session, _cbq(ADMIN, f"v1|tclose|{uuid.uuid4()}"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
    )
    assert gone[0].kind == "ack"
    assert gone[0].text == EXPIRED_BUTTON_AR
    junk = handle_update(
        owner_session, _cbq(ADMIN, "v1|tclose|not-a-uuid"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
    )
    assert junk[0].kind == "ack"


def test_the_console_never_describes_a_cursor_ordering_that_is_gone() -> None:
    """Four comments in console.py — the pause/resume header, the
    subscription-action docstring and two paragraphs of ``handle_update`` —
    all justified themselves with «the runner stores the Telegram offset only
    AFTER handle_update returns», and the runner had just been turned around
    to store it BEFORE. The barrier's entire stated rationale described an
    ordering that no longer existed, which is worse than no comment: the next
    reader trusts it, and reasons about a wedge that cannot happen while
    missing the at-most-once cost that now can.

    This is a source check on purpose. There is no behaviour to assert — the
    behaviour is right; the prose was the defect — and the only thing that can
    catch prose drifting away from code is to read them together.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    runner_src = (root / "scripts" / "run_admin_bot.py").read_text("utf-8")
    console_src = (root / "src" / "career" / "telegram"
                   / "console.py").read_text("utf-8")

    # the fact the comments must agree with: the cursor moves first
    store = runner_src.index("_store_offset(session, update_id)")
    work = runner_src.index("outcomes = handler(session, update)")
    assert store < work

    # Comment text is wrapped and re-wrapped, so compare on one flat line.
    prose = " ".join(console_src.split())
    # Present tense is the whole test: «stored ... returned» is history and
    # belongs in these comments; «stores ... returns» is a claim about the
    # program as it is now, and that claim is false.
    for claim in (
        "runner stores the Telegram offset only AFTER handle_update returns",
        "runner stores the Telegram offset only after we return",
        "the runner never advanced the Telegram offset",
    ):
        assert claim not in prose, claim
    assert "now stores the offset BEFORE" in prose


def test_every_listed_ticket_carries_its_own_close_button(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The button is tied to the ticket by an Arabic-Indic numeral, not by its
    TEN code: «إغلاق TEN-0002» mixes Arabic and Latin on one line and arrives
    reversed on the operator's client (§16)."""
    import re

    t1, t2 = two_tenants
    channel1 = _seed_channel(owner_session, t1)
    channel2 = _seed_channel(owner_session, t2)
    latin_or_digit = re.compile(r"[A-Za-z0-9]")
    try:
        _seed_ticket(owner_session, t1, channel1, age_hours=5)
        _seed_ticket(owner_session, t2, channel2, age_hours=2)
        screen = _tickets(owner_session)
        closes = [(label, data) for row in screen.keyboard
                  for label, data in row if data.startswith("v1|tclose|")]
        assert len(closes) == 2
        assert "١ •" in screen.text and "٢ •" in screen.text
        for label, _data in closes:
            assert not latin_or_digit.search(label), label
    finally:
        _drop_tickets(owner_session, t1, t2)
        _clear_delivery(owner_session, t1)
        _clear_delivery(owner_session, t2)


def test_the_tickets_screen_is_reachable_from_the_menu(
    owner_session: Session
) -> None:
    """A screen with no way in is the same defect as an action with no
    button — which is the other half of this change."""
    menu = handle_update(
        owner_session, _msg(ADMIN, "/start"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )[0]
    assert any("v1|tickets" == data
               for row in menu.keyboard for _label, data in row)


def test_the_tickets_screen_is_pii_free_and_direction_pure(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """§15.13 — TEN codes only, never a phone. And no Latin or digits inside
    an Arabic line: the code stands alone, the age uses Arabic digits."""
    import re

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")

    t1, _ = two_tenants
    channel = _seed_channel(owner_session, t1)
    phone = owner_session.execute(sql_text(
        "SELECT phone_e164 FROM customer_channels WHERE id = :c"),
        {"c": channel}).scalar_one()
    try:
        _seed_ticket(owner_session, t1, channel, age_hours=0)
        text = _tickets(owner_session).text
        assert phone not in text
        assert "+966" not in text
        assert "منذ ٠ دقيقة" in text
        for line in text.splitlines():
            if arabic.search(line):
                assert not latin_or_digit.search(line), line
    finally:
        _drop_tickets(owner_session, t1)
        _clear_delivery(owner_session, t1)


# ── the forgotten ticket: a mute nobody could ever lift ──────────────────────


def _seed_inbound(
    session: Session, tenant_id: str, channel_id: str, *,
    message_type: str = "text", when: datetime = NOW,
) -> str:
    inbound_id = str(uuid.uuid4())
    session.execute(sql_text(
        "INSERT INTO inbound_messages (id, tenant_id, channel_id,"
        " wa_message_id, message_type, text_body, classification, payload,"
        " received_at) VALUES (:i, :t, :c, :w, :m, :b, 'other', '{}', :r)"),
        {"i": inbound_id, "t": tenant_id, "c": channel_id,
         "w": f"wamid.{uuid.uuid4()}", "m": message_type,
         "b": "أبي رأيك في عرض وظيفي وصلني", "r": when})
    session.commit()
    return inbound_id


def _drop_inbound(session: Session, *tenant_ids: str) -> None:
    session.rollback()
    for tenant_id in tenant_ids:
        session.execute(sql_text(
            "DELETE FROM inbound_messages WHERE tenant_id = :t"),
            {"t": tenant_id})
    session.commit()


def _phone_of(session: Session, channel_id: str) -> str:
    return str(session.execute(sql_text(
        "SELECT phone_e164 FROM customer_channels WHERE id = :c"),
        {"c": channel_id}).scalar_one())


def _ticket_status(session: Session, ticket_id: str) -> str:
    session.rollback()
    return str(session.execute(sql_text(
        "SELECT status FROM support_events WHERE id = :i"),
        {"i": ticket_id}).scalar_one())


def _direct_line(session: Session, tenant_id: str, channel_id: str) -> str:
    """What the لمّاح+ direct line does with this customer's next message."""
    from career.promises import career_session

    return career_session.escalate_direct_message(
        session, tenant_id=uuid.UUID(tenant_id),
        channel_id=uuid.UUID(channel_id), now=NOW,
    )


def test_a_forgotten_ticket_stops_silencing_the_customers_direct_line(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The residue `escalate_direct_message` wrote down and could not close.

    ONE open ticket per customer is right — four messages in a row are one
    human waiting — but nothing ever swept a forgotten one, so a ticket left
    open by accident silenced that customer's direct line for as long as it
    stayed open, which was forever. Here the ticket is two days old: the line
    reopens, the operator is told once, and the ticket is neither closed nor
    hidden.
    """
    import re

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    channel = _seed_channel(owner_session, t1)
    _seed_subscription(owner_session, t1, "ACTIVE", plan="executive")
    try:
        opener = _seed_inbound(owner_session, t1, channel,
                               when=NOW - timedelta(hours=72))
        ticket = _seed_ticket(
            owner_session, t1, channel, kind="executive_direct_message",
            age_hours=72, inbound_message_id=opener,
        )
        # he wrote twice more into the silence
        _seed_inbound(owner_session, t1, channel, when=NOW - timedelta(hours=5))
        _seed_inbound(owner_session, t1, channel, when=NOW - timedelta(hours=2))

        # before: his next message reaches nobody
        assert _direct_line(owner_session, t1, channel) == "already_open"
        owner_session.rollback()

        outcomes = handle_update(
            owner_session, _msg(ADMIN, "/start"), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW,
        )
        owner_session.commit()

        pages = [o for o in outcomes if o.kind == "send"
                 and "🔁" in o.text]
        assert len(pages) == 1, [o.text for o in outcomes]
        page = pages[0].text
        assert code in page
        assert "منذ ٣ يوم" in page
        assert "وراسلك بعدها مرات عددها: ٢" in page
        # §15.13 + the bidi rule: TEN code only, and no Latin inside Arabic
        assert _phone_of(owner_session, channel) not in page
        for line in page.splitlines():
            if re.search(r"[؀-ۿ]", line):
                assert not re.search(r"[A-Za-z0-9]", line), line

        # the ticket is released — not closed, not hidden
        assert _ticket_status(owner_session, ticket) == support.RELEASED
        screen = _tickets(owner_session).text
        assert code in screen
        assert console.TICKET_RELEASED_MARK_AR in screen

        # after: the same message now reaches the operator
        assert _direct_line(owner_session, t1, channel) == "escalated"
        owner_session.rollback()

        # …and it pages ONCE, ever: a second sweep releases nothing new
        again = handle_update(
            owner_session, _msg(ADMIN, "/start"), admin_chat_id=ADMIN,
            probes=FakeProbes(), now=NOW + timedelta(hours=1),
        )
        owner_session.commit()
        assert not [o for o in again if "🔁" in o.text]

        # the operator's own button still closes it
        assert "أغلقنا التذكرة" in _close_ticket(owner_session, ticket)
        assert _ticket_status(owner_session, ticket) == support.RESOLVED
    finally:
        _drop_tickets(owner_session, t1)
        _drop_inbound(owner_session, t1)
        _drop_subscriptions(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_the_queue_says_which_message_opened_each_ticket(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """`inbound_message_id` was stored from the first day and rendered
    nowhere, so the queue said «since when» and never «about what». The body
    is PII and stays out (§15.13); the SHAPE is what tells a customer who
    wrote to you from a thumb on a card three months old."""
    t1, t2 = two_tenants
    channel1 = _seed_channel(owner_session, t1)
    channel2 = _seed_channel(owner_session, t2)
    try:
        wrote = _seed_inbound(owner_session, t1, channel1)
        _seed_ticket(owner_session, t1, channel1, age_hours=3,
                     inbound_message_id=wrote)
        tapped = _seed_inbound(owner_session, t2, channel2,
                               message_type="interactive")
        _seed_ticket(owner_session, t2, channel2, age_hours=2,
                     inbound_message_id=tapped)
        text = _tickets(owner_session).text

        assert console._TICKET_OPENER_AR["text"] in text
        assert console._TICKET_OPENER_AR["interactive"] in text
        # a system-raised ticket says so rather than pretending to a message
        _seed_ticket(owner_session, t1, channel1,
                     kind="career_session_overdue", age_hours=1)
        assert console.TICKET_OPENER_SYSTEM_AR in _tickets(owner_session).text
        # There is no «its message was deleted» line, and this is why: a §12
        # deletion removes the customer's channel, and support_events cascades
        # from the channel — so the ticket goes with the message rather than
        # outliving it. A branch for the other case would be a state only a
        # test could produce.
        owner_session.execute(sql_text(
            "DELETE FROM inbound_messages WHERE id = :i"), {"i": wrote})
        owner_session.commit()
        assert console.TICKET_OPENER_SYSTEM_AR in _tickets(owner_session).text
    finally:
        _drop_tickets(owner_session, t1, t2)
        _drop_inbound(owner_session, t1, t2)
        _clear_delivery(owner_session, t1)
        _clear_delivery(owner_session, t2)


def test_the_traffic_line_never_counts_the_message_that_opened_the_ticket(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """ADVERSARIAL 2026-08-07, in the production order of operations.

    `escalate_direct_message` stamps the ticket with the `now` the worker loop
    captured before it opened its transaction; `inbound_messages.received_at`
    is `server_default=func.now()`, the transaction clock, which is later. So
    `received_at > created_at` matched the ticket's OWN opener and the alert
    told the operator the customer had written once SINCE — about a customer
    who had written once and then waited.
    """
    from career.db.models import InboundMessage
    from career.promises import career_session as _cs

    t1, _ = two_tenants
    channel = _seed_channel(owner_session, t1)
    _seed_subscription(owner_session, t1, "ACTIVE", plan="executive")
    try:
        now = datetime.now(UTC)          # the worker's `now`, captured first
        inbound = InboundMessage(
            id=uuid.uuid4(), tenant_id=uuid.UUID(t1),
            channel_id=uuid.UUID(channel),
            wa_message_id=f"wamid.{uuid.uuid4()}", message_type="text",
            text_body="أبي رأيك في عرض وظيفي وصلني", classification="other",
            payload={}, processed_at=now,
        )
        owner_session.add(inbound)
        owner_session.flush()            # …and only now does the clock start
        assert _cs.escalate_direct_message(
            owner_session, tenant_id=uuid.UUID(t1),
            channel_id=uuid.UUID(channel), now=now,
            inbound_message_id=inbound.id,
        ) == "escalated"
        owner_session.commit()

        alerts = console.release_forgotten_tickets(
            owner_session, now=now + timedelta(hours=49))
        owner_session.commit()
        assert len(alerts) == 1
        assert console.TICKET_TRAFFIC_QUIET_AR in alerts[0], alerts[0]
    finally:
        owner_session.rollback()
        _drop_tickets(owner_session, t1)
        _drop_inbound(owner_session, t1)
        _drop_subscriptions(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_a_release_the_operators_own_tap_rolled_back_is_never_paged(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """ADVERSARIAL 2026-08-07. The sweep runs at the top of `handle_update`,
    inside the transaction `_dispatch` is about to use — and several dispatch
    actions END on `session.rollback()`: `_run_career_session` when there is
    no request on file, `_run_subscription_action` on a refused transition,
    `_run_reply` on a failed send. Each of them threw the release away while
    the alert was still returned, so the operator read «فتحنا خطه من جديد»
    about a ticket that was still `open` and still muting the customer.

    And it did not stop at one wrong page. The row stayed releasable, so the
    same ticket was released and paged again on his next refusing tap, and
    again after that — the repetition the «one-way edge» was supposed to make
    structurally impossible, rebuilt by the caller.

    Driven through the operator's real two taps on a real refusing action.
    """
    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    channel = _seed_channel(owner_session, t1)
    _seed_subscription(owner_session, t1, "ACTIVE", plan="executive")
    try:
        ticket = _seed_ticket(
            owner_session, t1, channel, kind="executive_direct_message",
            age_hours=72,
        )
        # «تم التنسيق للجلسة» with no session request on file — the refusal
        # path rolls back.
        first = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|cs_scheduled"),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW)
        second = handle_update(
            owner_session, _cbq(ADMIN, first[1].keyboard[0][0][1]),
            admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW)
        assert console.SESSION_NO_REQUEST_AR.format(code=code) in second[1].text

        paged = [o for o in (*first, *second)
                 if o.kind == "send" and "🔁" in o.text]
        assert len(paged) == 1, [o.text for o in (*first, *second)]
        # …and what he was told is TRUE of the ledger he acts on.
        assert _ticket_status(owner_session, ticket) == support.RELEASED

        # the mute is really gone: his next message reaches a human
        assert _direct_line(owner_session, t1, channel) == "escalated"
        owner_session.rollback()

        # and no later refusing tap pages it a second time
        third = handle_update(
            owner_session, _cbq(ADMIN, f"v1|act|{code}|cs_scheduled"),
            admin_chat_id=ADMIN, probes=FakeProbes(),
            now=NOW + timedelta(hours=1))
        assert not [o for o in third if "🔁" in o.text]
    finally:
        owner_session.rollback()
        _drop_tickets(owner_session, t1)
        _drop_subscriptions(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_two_concurrent_sweeps_page_one_forgotten_ticket_exactly_once(
    owner_engine: Engine, owner_session: Session,
    two_tenants: tuple[str, str],
) -> None:
    """ADVERSARIAL 2026-08-07 — the second sweeper, in two real transactions.

    `release_forgotten_tickets` has two callers in two processes: the admin
    bot runs it on every operator tap (`handle_update`) and
    `scripts/run_worker_loop.sweep_forgotten_tickets` runs it hourly in the
    worker. Its select read `status = 'open'` and its loop then wrote
    `released` keyed on `id` with no status predicate, so a transaction that
    read before the other committed still wrote and still returned an alert:
    ONE customer, TWO «🔁» pages, on the channel whose whole value is that a
    message on it means something new happened. Both docstrings called that
    impossible.

    NO THREADS, and that is the point of the test rather than a convenience.
    `FOR UPDATE … SKIP LOCKED` never waits, so the interleaving can be built
    by hand: session A takes the row and holds it open, session B sweeps in
    the same thread and must find nothing. There is no timing window to lose,
    so this cannot become a test that passes because the machine was busy.

    `lock_timeout` is the safety valve, not the subject. WITHOUT the locking
    clause B's select happily returns the row and B's UPDATE then blocks on
    A's uncommitted write — a hang, which is a rotten way for a suite to
    report a defect. With the timeout it is a fast, legible failure; with the
    fix the timeout is never reached, because SKIP LOCKED does not wait.
    """
    t1, _ = two_tenants
    channel = _seed_channel(owner_session, t1)
    try:
        ticket = _seed_ticket(owner_session, t1, channel, age_hours=72)
        owner_session.commit()

        other = Session(owner_engine)
        try:
            # A: the hourly worker sweep. Reads, releases, still uncommitted.
            first = console.release_forgotten_tickets(other, now=NOW)
            assert len(first) == 1, first
            assert "🔁" in first[0]

            # B: the operator taps the console a millisecond later.
            owner_session.execute(sql_text("SET LOCAL lock_timeout = '3s'"))
            second = console.release_forgotten_tickets(owner_session, now=NOW)
            assert second == [], f"{len(second) + 1} pages for one ticket"

            other.commit()
        finally:
            other.rollback()
            other.close()

        # the release itself is untouched by the skip: exactly once, one-way.
        assert _ticket_status(owner_session, ticket) == support.RELEASED
        # …and once A has committed, B's next sweep has genuinely nothing to
        # do — the skip deferred no work, because the row is no longer open.
        assert console.release_forgotten_tickets(
            owner_session, now=NOW + timedelta(hours=1)) == []
    finally:
        owner_session.rollback()
        _drop_tickets(owner_session, t1)


# ── one product, one Arabic name, on every screen ────────────────────────────


def test_weekly_report_and_customer_card_cannot_name_a_plan_differently() -> None:
    """Three names for one product is what the second copy bought us: the
    weekly report said «احترافي» where the card said «لمّاح», it carried
    «elite» — a plan code that never existed — and it had no ``executive`` at
    all, so لمّاح+ went out as a raw Latin token every Sunday. This fails the
    moment either screen grows a mapping the other does not share.
    """
    from datetime import date

    from career.telegram.weekly_report import format_weekly_report

    for code, expected in views._PLAN_AR.items():
        report = format_weekly_report(
            date(2026, 7, 26), {"subs_by_plan": {code: 1}}, {}
        )
        assert f"{expected}: ١" in report, code
        assert code not in report          # never the raw Latin plan code


def test_weekly_report_speaks_every_plan_and_every_day_state() -> None:
    """A plan or a day state with no Arabic word renders as Latin inside an
    Arabic line, which Fahad's client scrambles — so an unmapped code is a
    formatting bug, not a cosmetic one."""
    from datetime import date

    from career.cv.close import DAILY_STATES
    from career.salla.renewal import RENEWABLE_PLANS
    from career.telegram.weekly_report import _STATE_AR, format_weekly_report

    assert set(DAILY_STATES) <= set(_STATE_AR)
    for code in {*RENEWABLE_PLANS, "cv_analysis"}:
        assert code in views._PLAN_AR, code

    report = format_weekly_report(
        date(2026, 7, 26),
        {"subs_by_plan": {"executive": 1, "cv_analysis": 2}},
        dict.fromkeys(DAILY_STATES, 1),
    )
    assert "لمّاح+: ١" in report
    for state in DAILY_STATES:
        assert state not in report


def test_weekly_report_carries_no_second_plan_map() -> None:
    """The class of bug, not the instance: the drift was only possible because
    the mapping existed twice. A new dict here would let it happen again with
    every assertion above still green."""
    from pathlib import Path

    import career.telegram.weekly_report as wr

    source = Path(wr.__file__).read_text(encoding="utf-8")
    assert "_PLAN_AR = {" not in source
    # No module-level dict here may key plan codes, whatever it is called —
    # and the labels must be the console's own function, not a lookalike.
    assert [v for v in vars(wr).values()
            if isinstance(v, dict) and "professional" in v] == []
    # The whole breakdown, not only the names: the two screens write the sales
    # line with ONE function, so a plan the report cannot name behaves the same
    # way on both (its own line, never dropped) instead of twice differently.
    assert wr.plan_counts_lines is views.plan_counts_lines


# ── the Sunday report remembers the week, not the process (0023) ────────────


def test_the_two_day_state_maps_differ_in_words_and_never_in_keys() -> None:
    """The wording difference between the console's map and the report's is
    deliberate — one state per line with a traffic light in front of it, versus
    eight of them joined by « · » on the line the operator scans on a Sunday —
    and it is documented in place. The KEYS are a different matter entirely: a
    state one screen knows and the other does not renders as a raw Latin token
    inside an Arabic line, which Fahad's client scrambles. That is the defect
    that already cost this file its plan map, and it is now impossible to
    reintroduce on either side without failing here."""
    from career.cv.close import DAILY_STATES
    from career.telegram.weekly_report import _STATE_AR as report_states

    assert set(report_states) == set(views._STATE_AR)
    assert set(DAILY_STATES) <= set(report_states)
    assert set(DAILY_STATES) <= set(views._STATE_AR)
    # and the difference is real, not an accident nobody noticed
    assert report_states != views._STATE_AR


def test_the_owed_week_is_answerable_at_any_moment() -> None:
    """«Is it Sunday between 06:00 and noon» was the only memory the report
    had, which is why being outside the window meant both «already sent» and
    «never going to happen». «Which week is owed» has an answer on a Wednesday
    too, and that is what makes a late report possible instead of a lost one."""
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    from career.telegram.weekly_report import due_week_ending

    riyadh = ZoneInfo("Asia/Riyadh")
    sunday = _date(2026, 8, 2)
    # Sunday 06:00 — the week that just ended
    assert due_week_ending(
        datetime(2026, 8, 2, 6, 0, tzinfo=riyadh)) == sunday
    # Sunday 05:59 — still LAST week's report that is owed, not this one's
    assert due_week_ending(
        datetime(2026, 8, 2, 5, 59, tzinfo=riyadh)) == sunday - timedelta(days=7)
    # Wednesday — an outage that swallowed Sunday still owes Sunday's report
    assert due_week_ending(
        datetime(2026, 8, 5, 14, 0, tzinfo=riyadh)) == sunday
    # the following Sunday is a new debt
    assert due_week_ending(
        datetime(2026, 8, 9, 7, 0, tzinfo=riyadh)) == sunday + timedelta(days=7)


def _marker(session: Session) -> Any:
    return session.execute(sql_text(
        "SELECT weekly_report_sent_for FROM admin_bot_state WHERE id = 1"
    )).scalar_one()


def test_a_restart_inside_the_sunday_window_never_resends_the_week(
    owner_session: Session
) -> None:
    """The incident: `last_weekly_report` was a local of the worker loop's
    main(), and the unit is Restart=always — so a deploy with three restarts
    between 06:00 and noon put three identical weekly reports on the operator's
    phone. The claim is a conditional UPDATE, so the second caller loses
    whether it is a restart, the next hourly sweep, or a systemd timer running
    beside the loop."""
    from datetime import date as _date

    from career.telegram.weekly_report import claim_weekly_report

    before = _marker(owner_session)
    try:
        week = _date(2026, 8, 2)
        assert claim_weekly_report(owner_session, week_ending=week) is True
        assert claim_weekly_report(owner_session, week_ending=week) is False
        assert claim_weekly_report(owner_session, week_ending=week) is False
        assert _marker(owner_session) == week
        # and the next week is owed again — the marker gates a week, not a send
        assert claim_weekly_report(
            owner_session, week_ending=week + timedelta(days=7)) is True
    finally:
        owner_session.execute(sql_text(
            "UPDATE admin_bot_state SET weekly_report_sent_for = :d WHERE id = 1"),
            {"d": before})
        owner_session.commit()


def test_an_outage_across_sunday_owes_the_week_it_missed(
    owner_session: Session
) -> None:
    """The silent half of the same bug. With the guard living in the process,
    an outage spanning the six-hour window dropped the week and nothing
    anywhere remembered it was owed. A marker that is a WEEK rather than a
    window is claimable on the Monday, late and honest."""
    from zoneinfo import ZoneInfo

    from career.telegram.weekly_report import claim_weekly_report, due_week_ending

    before = _marker(owner_session)
    try:
        riyadh = ZoneInfo("Asia/Riyadh")
        owner_session.execute(sql_text(
            "UPDATE admin_bot_state SET weekly_report_sent_for = :d WHERE id = 1"),
            {"d": due_week_ending(datetime(2026, 7, 26, 7, 0, tzinfo=riyadh))})
        owner_session.commit()
        # the worker was down all Sunday and comes back on Monday afternoon
        monday = datetime(2026, 8, 3, 15, 0, tzinfo=riyadh)
        owed = due_week_ending(monday)
        assert claim_weekly_report(owner_session, week_ending=owed) is True
    finally:
        owner_session.execute(sql_text(
            "UPDATE admin_bot_state SET weekly_report_sent_for = :d WHERE id = 1"),
            {"d": before})
        owner_session.commit()


def test_a_week_that_was_claimed_but_never_sent_is_given_back(
    owner_session: Session
) -> None:
    """Claiming before sending is what stops the duplicate storm, and this is
    the price it must pay: a Telegram refusal after the claim would otherwise
    spend the week on a report nobody received."""
    from datetime import date as _date

    from career.telegram.weekly_report import (
        claim_weekly_report,
        release_weekly_report,
        weekly_report_marker,
    )

    before = _marker(owner_session)
    try:
        week = _date(2026, 8, 2)
        owner_session.execute(sql_text(
            "UPDATE admin_bot_state SET weekly_report_sent_for = NULL WHERE id = 1"))
        owner_session.commit()
        previous = weekly_report_marker(owner_session)
        assert claim_weekly_report(owner_session, week_ending=week) is True
        release_weekly_report(owner_session, restore_to=previous)
        assert _marker(owner_session) is None
        assert claim_weekly_report(owner_session, week_ending=week) is True
    finally:
        owner_session.execute(sql_text(
            "UPDATE admin_bot_state SET weekly_report_sent_for = :d WHERE id = 1"),
            {"d": before})
        owner_session.commit()


def test_a_late_report_says_which_day_its_numbers_end_on() -> None:
    """A catch-up report is headed «حتى الأحد» while carrying Wednesday's
    seven-day numbers, and the reader has no way of noticing. The date sits on
    its own line — a digit inside an Arabic line is scrambled."""
    from datetime import date as _date

    from career.telegram.weekly_report import format_weekly_report

    on_time = format_weekly_report(_date(2026, 8, 2), {}, {})
    assert "متأخر" not in on_time

    late = format_weekly_report(
        _date(2026, 8, 2), {}, {}, covering_through=_date(2026, 8, 5))
    assert "متأخر" in late
    assert "2026-08-05" in late.split("\n")


# ── the business screen reads the money functions, not its own copies ────────


def test_business_revenue_excludes_the_money_that_went_back(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """«الإيراد» is the money that arrived and STAYED.

    This screen used to sum `amount_sar` over every subscription row with no
    status predicate at all, so a refunded order, a cancelled one and a
    chargeback each counted forever — and a chargeback is money that left the
    account twice. On the rows below that reads 826 riyals against a true 199:
    an overstatement of 312%, on the number the operator prices the product
    from. The correct definition already lived in cv.close, derived from the
    state machine's own TERMINAL_STATES; this asserts the screen uses it.
    """
    import uuid as _uuid

    from sqlalchemy import text as _sql

    import career.telegram.console as console

    t1, _ = two_tenants
    rows = (
        ("professional", "ACTIVE", "199"),      # the only real revenue
        ("professional", "REFUNDED", "199"),    # money returned
        ("professional", "CANCELED", "199"),    # order cancelled
        ("professional", "CHARGEBACK", "229"),  # left the account twice
    )
    for plan, status, amount in rows:
        owner_session.execute(_sql(
            "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
            " salla_order_id, amount_sar, currency)"
            " VALUES (:i, :t, :p, :s, :o, :a, 'SAR')"),
            {"i": str(_uuid.uuid4()), "t": str(t1), "p": plan, "s": status,
             "o": f"O-{_uuid.uuid4()}", "a": amount})
    owner_session.commit()

    data = console._business_data(owner_session, "30", now=NOW)
    assert int(data["revenue_sar"]) == 199
    # the COUNT is filtered by the same rule as the sum — a plan showing four
    # subscriptions against one order's riyals is the screen contradicting
    # itself in front of the reader
    assert data["subs_by_plan"].get("professional") == 1

    text, _ = views.render_business("30", data)
    assert "الإيراد: ١٩٩ ريال" in text


def test_business_spend_is_the_one_definition_including_the_whatsapp_half(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The screen no longer spells the spend expression out by hand.

    Two expressions for one number lived in two files (this screen and
    close.rollup_costs) and disagreed about which kinds count; only the screen
    was ever looked at. It now calls close.spend_by_kind, which is that one
    expression — metered usage_events plus the WhatsApp half derived from the
    delivery ledger, which is where the newly-recorded lifecycle templates
    land.
    """
    import uuid as _uuid

    from sqlalchemy import text as _sql

    import career.telegram.console as console
    from career.cv import close as close_mod

    t1, _ = two_tenants
    channel = str(_uuid.uuid4())
    owner_session.execute(_sql(
        "INSERT INTO customer_channels (id, tenant_id, provider, phone_e164)"
        " VALUES (:i, :t, 'whatsapp', :p)"),
        {"i": channel, "t": str(t1), "p": f"+96650{_uuid.uuid4().int % 10**7:07d}"})
    owner_session.execute(_sql(
        "INSERT INTO usage_events (id, tenant_id, kind, cost_usd, occurred_at)"
        " VALUES (:i, :t, 'llm_generation', 0.25, now())"),
        {"i": str(_uuid.uuid4()), "t": str(t1)})
    # one billed template with a channel, one WITHOUT — the pre-activation
    # shape 0028 made recordable at all
    for chan in (channel, None):
        owner_session.execute(_sql(
            "INSERT INTO delivery_messages (id, tenant_id, channel_id, kind,"
            " template_name, status, wa_message_id)"
            " VALUES (:i, :t, :c, 'template', 'renewal_reminder', 'sent', :m)"),
            {"i": str(_uuid.uuid4()), "t": str(t1), "c": chan,
             "m": f"wamid-{_uuid.uuid4()}"})
    owner_session.commit()

    data = console._business_data(owner_session, "30", now=NOW)
    spend = data["spend_by_category"]
    assert spend["llm_generation"][0] == 1
    # both templates are billed — the channel-less one is not a second-class row
    # (bucket corrected 2026-08-08: `renewal_reminder` is MARKETING at Meta,
    # which re-categorised it after approving it as utility)
    assert spend["wa_marketing"][0] == 2
    # and the screen agrees with the function, exactly
    canonical = close_mod.spend_by_kind(owner_session, since=NOW - timedelta(days=30))
    assert {k: v for k, v in spend.items()} == canonical


# ── the لمّاح+ pass, both halves of it, on the operator's screens ────────────
#
# Two promises from the same 449-riyal page were built and reachable by nobody
# (STORE-PAGES-AR §6, and the refund page's own deduction sentence). Both are
# operator-facing, so both are proven from the operator's own screens here.


class _FakeAdmin:
    """The admin channel, captured. Nothing is sent anywhere."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_admin(self, text: str) -> None:
        self.sent.append(text)


def _seed_plus(
    session: Session, tenant_id: str, *, plan: str = "executive",
    status: str = "ACTIVE", period_start: datetime | None = None,
) -> str:
    """A real pass row: plan + status + the period the refund would be about."""
    sub_id = str(uuid.uuid4())
    start = period_start if period_start is not None else NOW - timedelta(days=3)
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency, current_period_start,"
        " current_period_end) VALUES (:i, :t, :p, :s, :o, 449.00, 'SAR',"
        " :ps, :pe)"),
        {"i": sub_id, "t": tenant_id, "p": plan, "s": status,
         "o": f"O-{uuid.uuid4()}", "ps": start, "pe": start + timedelta(days=30)})
    session.commit()
    return sub_id


def _drop_sessions(session: Session, tenant_id: str) -> None:
    session.rollback()
    session.execute(sql_text(
        "DELETE FROM career_sessions WHERE tenant_id = :t"), {"t": tenant_id})
    session.commit()


def _card(session: Session, code: str) -> str:
    return handle_update(
        session, _cbq(ADMIN, f"v1|tenant|{code}"), admin_chat_id=ADMIN,
        probes=FakeProbes(), now=NOW,
    )[1].text


def test_the_card_deduction_is_read_from_the_ledger_and_names_its_period(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """«تُخصم قيمة الخدمات البشرية اللي استلمتها فعليًا» — actually received,
    and for the period being refunded.

    The card printed a constant, `f"{SESSION_VALUE_SAR} SAR"`, under a comment
    that called it computed, while `career_session.refund_deduction_sar` — the
    function that reads the ledger — had no production caller at all. And it
    printed a number with no period attached, on a card whose session line can
    belong to a DIFFERENT period than the current one (the cross-period
    fallback), so «how much» arrived without «for which order».
    """
    from career.promises import career_session

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    start = NOW - timedelta(days=5)
    try:
        _seed_plus(owner_session, t1, period_start=start)
        career_session.request_session(
            owner_session, tenant_id=uuid.UUID(t1), now=NOW - timedelta(days=2))
        career_session.mark_completed(
            owner_session, tenant_id=uuid.UUID(t1), now=NOW - timedelta(days=1))
        owner_session.commit()

        text = _card(owner_session, code)
        assert "وتُخصم قيمتها من أي استرداد:" in text
        assert "150 SAR" in text
        # WHICH period, said on the screen: the count that produced the
        # number, and the period that count was scoped to.
        assert "عن جلسات فترة اشتراك واحدة، عددها المحسوب: ١" in text
        assert start.date().isoformat() in text
        assert "واسترداد أي فترة ثانية يُحسب على حدة" in text
        # and the number is the ledger's own answer for that one period
        assert career_session.refund_deduction_sar(
            owner_session, tenant_id=uuid.UUID(t1)) == Decimal("150")
    finally:
        _drop_sessions(owner_session, t1)
        _drop_subscriptions(owner_session, t1)


def test_the_card_prints_what_the_refund_function_answers(
    owner_session: Session, two_tenants: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring itself, pinned.

    Today the constant and the computation agree in every reachable state —
    `uq_career_sessions_live_per_subscription` allows one live session per
    period, so the count is always 1 when this line renders. That agreement is
    a property of the catalog, not of the screen, and it is exactly why the
    defect survived: a coincidence reads like a correct answer. This asserts
    the screen prints whatever the ledger function returns, and that it asks
    it about the period of the session being displayed.
    """
    from career.promises import career_session

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    seen: dict[str, Any] = {}
    try:
        sub_id = _seed_plus(owner_session, t1)
        career_session.request_session(
            owner_session, tenant_id=uuid.UUID(t1), now=NOW - timedelta(days=2))
        career_session.mark_completed(
            owner_session, tenant_id=uuid.UUID(t1), now=NOW - timedelta(days=1))
        owner_session.commit()

        def _fake(session: Any, **kw: Any) -> Decimal:
            seen.update(kw)
            return Decimal("300")

        monkeypatch.setattr(career_session, "refund_deduction_sar", _fake)
        text = _card(owner_session, code)
        assert "300 SAR" in text                      # never a constant again
        assert "150 SAR" not in text
        assert str(seen.get("subscription_id")) == sub_id   # that period only
        assert str(seen.get("tenant_id")) == t1
    finally:
        _drop_sessions(owner_session, t1)
        _drop_subscriptions(owner_session, t1)


def test_a_plus_customers_ordinary_message_reaches_the_operator(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """«تواصل مباشر معي … اكتب لي وقت ما تحتاج» — the 449 tier's headline.

    An ACTIVE customer's ordinary sentence classifies as OTHER, the worker
    answers it with a template, and nothing reached a human: the only escape
    hatch was the word «دعم», which every 199 customer has too. So the higher
    tier bought a star on somebody else's alert, not access. The escalation
    lands in `support_events` — the queue the operator already works — and the
    alert carries the TEN code and not one character of what was written.
    """
    from career.promises import career_session

    t1, _ = two_tenants
    code = _code_of(owner_session, t1)
    channel = _seed_channel(owner_session, t1)
    admin = _FakeAdmin()
    try:
        _seed_plus(owner_session, t1)
        outcome = career_session.escalate_direct_message(
            owner_session, tenant_id=uuid.UUID(t1),
            channel_id=uuid.UUID(channel), now=NOW - timedelta(hours=2),
            admin_client=admin,
        )
        owner_session.commit()
        assert outcome == "escalated"

        # it is on the screen the operator already reads, with its age
        screen = _tickets(owner_session).text
        assert "⭐ رسالة مباشرة من مشترك لمّاح+" in screen
        assert code in screen
        assert "منذ ٢ ساعة" in screen

        # and the page names who, never what — no phone, no body (§15.13)
        assert len(admin.sent) == 1
        assert code in admin.sent[0] and "⭐" in admin.sent[0]
        assert "+966" not in admin.sent[0]

        # four messages are one human waiting, not four tickets
        assert career_session.escalate_direct_message(
            owner_session, tenant_id=uuid.UUID(t1),
            channel_id=uuid.UUID(channel), now=NOW, admin_client=admin,
        ) == "already_open"
        owner_session.commit()
        assert len(admin.sent) == 1
        assert owner_session.execute(sql_text(
            "SELECT count(*) FROM support_events WHERE tenant_id = :t"),
            {"t": t1}).scalar_one() == 1

        # closing it re-arms the line — the next message pages again
        ticket = str(owner_session.execute(sql_text(
            "SELECT id FROM support_events WHERE tenant_id = :t"),
            {"t": t1}).scalar_one())
        _close_ticket(owner_session, ticket)
        assert career_session.escalate_direct_message(
            owner_session, tenant_id=uuid.UUID(t1),
            channel_id=uuid.UUID(channel), now=NOW, admin_client=admin,
        ) == "escalated"
        owner_session.commit()
        assert len(admin.sent) == 2
    finally:
        _drop_tickets(owner_session, t1)
        _drop_subscriptions(owner_session, t1)
        _clear_delivery(owner_session, t1)


def test_the_direct_line_is_the_tier_and_never_an_answer_being_swallowed(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The three refusals, each with its own name.

    A 199 customer does not buy the direct line — if he did, the 449 page
    would be selling a star again. A lapsed pass is not «مشترك لمّاح+». And a
    customer in the middle of a conversation WE started is ANSWERING, not
    writing: `whatsapp.inbound` has two live incidents of exactly that
    mistake, and both times a paying customer's answer was thrown away and a
    ticket was raised against someone who had asked for nothing.
    """
    from career.promises import career_session

    t1, t2 = two_tenants
    channel1 = _seed_channel(owner_session, t1)
    channel2 = _seed_channel(owner_session, t2)
    admin = _FakeAdmin()

    def _escalate(tenant: str, channel: str) -> str:
        return career_session.escalate_direct_message(
            owner_session, tenant_id=uuid.UUID(tenant),
            channel_id=uuid.UUID(channel), now=NOW, admin_client=admin,
        )

    try:
        # nobody at all — no order, nothing to be a subscriber of
        assert _escalate(t1, channel1) == "no_subscription"
        # the 199 tier — the whole point of the difference
        _seed_plus(owner_session, t2, plan="professional")
        assert _escalate(t2, channel2) == "not_entitled"
        # a pass that ended
        _seed_plus(owner_session, t1, status="EXPIRED")
        assert _escalate(t1, channel1) == "lapsed"
        _drop_subscriptions(owner_session, t1)
        # mid-onboarding: this is an ANSWER, and it must not become a ticket
        _seed_plus(owner_session, t1)
        owner_session.execute(sql_text(
            "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
            " channel_id, state) SELECT :i, :t, s.id, :c, 'CV_UPLOAD_PENDING'"
            " FROM subscriptions s WHERE s.tenant_id = :t"),
            {"i": str(uuid.uuid4()), "t": t1, "c": channel1})
        owner_session.commit()
        assert _escalate(t1, channel1) == "in_flow"

        assert admin.sent == []
        assert owner_session.execute(sql_text(
            "SELECT count(*) FROM support_events WHERE tenant_id IN (:a, :b)"),
            {"a": t1, "b": t2}).scalar_one() == 0
    finally:
        owner_session.rollback()
        owner_session.execute(sql_text(
            "DELETE FROM onboarding_sessions WHERE tenant_id = :t"), {"t": t1})
        owner_session.commit()
        _drop_tickets(owner_session, t1, t2)
        _drop_subscriptions(owner_session, t1)
        _drop_subscriptions(owner_session, t2)
        _clear_delivery(owner_session, t1)
        _clear_delivery(owner_session, t2)


def test_the_direct_line_alert_and_its_ticket_line_are_direction_pure(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """§16: an Arabic line carrying a Latin run arrives scrambled on the
    operator's client, and the TEN code is the one thing he must read right."""
    import re

    from career.promises import career_session

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")
    for text in (career_session.DIRECT_MESSAGE_ALERT_AR.format(code=""),
                 console._TICKET_KIND_AR["executive_direct_message"],
                 console._TICKET_KIND_AR["career_session_overdue"]):
        for line in text.splitlines():
            if arabic.search(line):
                assert not latin_or_digit.search(line), line


# ── «انتهت مهلتها دون تسليم» — a sentence about the customer covering for a
#    fact about Meta ───────────────────────────────────────────────────────
#
# A bundle expires when it was held for the 24h window and the next run day
# arrived before that window opened. Two causes reach that state: the customer
# never wrote back, or the template we sent to re-open the window was REFUSED
# by Meta — 131049 (per-user marketing cap), 131050 (recipient switched
# «Offers and announcements» off), 131047 (re-engagement required). The old
# line named neither and the operator supplied the first one himself, every
# time. Two of the six expiries in the live data were the second one.


def test_an_expiry_no_longer_tells_the_operator_the_customer_ignored_us(
) -> None:
    """The default line, with no new data at all. It must stop implying one of
    two causes it cannot distinguish — a screen that guesses is worse than one
    that says it does not know, because he acts on it."""
    line = views._delivery_ar({"run_date": "2026-08-02",
                               "status": "EXPIRED_WINDOW"})

    assert "ميتا" in line, (
        "the expiry line still names only the customer's silence — the "
        "operator goes on reading «he ignored us» about a refused send")
    assert "العميل" in line, "and it must not swing to the other guess either"


def test_a_refusal_the_card_knows_about_is_stated_outright() -> None:
    """When the card carries the fact, the screen stops hedging. Checked
    before the status word, because a refusal is true whatever the bundle's
    own status happens to say."""
    refused = views._delivery_ar({
        "run_date": "2026-08-02", "status": "EXPIRED_WINDOW",
        views.REFUSED_KEY: True,
    })

    assert "ميتا رفضت" in refused
    assert "انتهت المهلة" not in refused, \
        "the hedge outlived the fact that settles it"
    # the PARTIAL correction is not lost to the new branch
    assert views._delivery_ar(
        {"status": "PARTIAL", "delivered": 0}) == "🔴 لم يصل منها شيء"
    assert views._delivery_ar(
        {"status": "COMPLETED"}) == "✅ وصلت كاملة"


def test_the_expiry_and_refusal_lines_are_direction_pure() -> None:
    """§16 — Fahad's client reverses any line mixing Arabic with Latin or
    European digits, and these two lines are new."""
    import re

    arabic = re.compile(r"[؀-ۿ]")
    latin_or_digit = re.compile(r"[A-Za-z0-9]")
    for line in (views._DELIVERY_AR["EXPIRED_WINDOW"],
                 views._delivery_ar({"status": "EXPIRED_WINDOW",
                                     views.REFUSED_KEY: True})):
        assert arabic.search(line)
        assert not latin_or_digit.search(line), line


# ── … and the card that has to SUPPLY that fact ─────────────────────────────
#
# The three tests above are about the RENDERER, which shipped hedging because
# `_tenant_card` had nothing definite to give it: the card carried the
# bundle's own status word and no way to reach the outbound messages under it.
# The predicate is `delivery_messages.status == 'failed'` on this bundle —
# every one of Meta's refusal codes lands on that single word on purpose (the
# worker refused to mint a «refused» status because `close.whatsapp_spend`
# bills everything that is not exactly `failed`), so it is the only fact there
# is to join to, and joining to it is why `Delivery.id` is now selected.


def _seed_bundle(
    session: Session, tenant_id: str, *, status: str, run_date: str,
    delivered: list[str], message_statuses: list[str],
) -> str:
    """One delivery with its outbound message log. Returns the delivery id.

    ``delivered`` is what LANDED (the bundle's own results), ``message_statuses``
    are the receipts Meta sent back for the sends — the two halves the card has
    to read together."""
    import json

    channel_id = str(uuid.uuid4())
    did = str(uuid.uuid4())
    session.execute(sql_text(
        "INSERT INTO customer_channels (id, tenant_id, provider, phone_e164)"
        " VALUES (:i, :t, 'whatsapp', :p)"),
        {"i": channel_id, "t": tenant_id,
         "p": f"+96650{uuid.uuid4().int % 10**7:07d}"})
    session.execute(sql_text(
        "INSERT INTO deliveries (id, tenant_id, channel_id, run_date, status,"
        " bundle) VALUES (:i, :t, :c, :d, :s, :b)"),
        {"i": did, "t": tenant_id, "c": channel_id, "d": run_date, "s": status,
         "b": json.dumps({"results": {"delivered": delivered, "failed": []}})})
    for msg_status in message_statuses:
        session.execute(sql_text(
            "INSERT INTO delivery_messages (id, tenant_id, channel_id,"
            " delivery_id, kind, template_name, status, wa_message_id)"
            " VALUES (:i, :t, :c, :d, 'template', 'daily_jobs_ready', :s, :m)"),
            {"i": str(uuid.uuid4()), "t": tenant_id, "c": channel_id, "d": did,
             "s": msg_status, "m": f"wamid-{uuid.uuid4()}"})
    session.commit()
    return did


def test_the_card_names_the_refusal_behind_an_expiry(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The whole point: an expiry Meta caused stops being read as silence.

    Two of the six live expiries were 131049 — the re-engagement template was
    withheld, the customer never saw anything to answer, and the screen told
    the operator he had gone quiet. With the fact on the card the sentence is
    definite, and it is the RENDERED line that is asserted, because a key
    nobody reads would be a fact that changed nothing.
    """
    t1, _ = two_tenants
    _seed_bundle(owner_session, t1, status="EXPIRED_WINDOW",
                 run_date="2026-07-14", delivered=[],
                 message_statuses=["failed"])

    card = console._tenant_card(owner_session, code=_code_of(owner_session, t1),
                                now=NOW)

    assert card is not None
    last = card["last_delivery"]
    assert last[views.REFUSED_KEY] is True
    assert "ميتا رفضت" in views._delivery_ar(last)


def test_an_expiry_with_no_refusal_keeps_the_honest_hedge(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The customer who really did go quiet. Nothing failed, so the card must
    not claim a refusal — the hedged line is the true one here, and inventing
    the definite one would be the same lie pointing the other way."""
    t1, _ = two_tenants
    _seed_bundle(owner_session, t1, status="EXPIRED_WINDOW",
                 run_date="2026-07-14", delivered=[],
                 message_statuses=["sent", "delivered"])

    card = console._tenant_card(owner_session, code=_code_of(owner_session, t1),
                                now=NOW)

    assert card is not None
    assert card["last_delivery"][views.REFUSED_KEY] is False
    assert "إما ما رد العميل" in views._delivery_ar(card["last_delivery"])


def test_a_refusal_among_messages_that_landed_does_not_erase_them(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Why the predicate is «failed AND nothing landed», not «failed».

    A bundle can deliver three cards and have a fourth refused. «ميتا رفضت
    الإرسال — ما وصلت العميل» about that bundle is a NEW falsehood on the one
    screen written against falsehoods, so the refusal branch is withheld
    wherever something did land and the card keeps saying what reached him.
    On the expiries this change exists for the two readings agree exactly:
    an expired bundle delivered nothing.
    """
    t1, _ = two_tenants
    _seed_bundle(owner_session, t1, status="COMPLETED", run_date="2026-07-14",
                 delivered=["job-1", "job-2", "job-3"],
                 message_statuses=["delivered", "delivered", "delivered",
                                   "failed"])

    card = console._tenant_card(owner_session, code=_code_of(owner_session, t1),
                                now=NOW)

    assert card is not None
    last = card["last_delivery"]
    assert last["delivered"] == 3
    assert last[views.REFUSED_KEY] is False
    assert views._delivery_ar(last) == "✅ وصلت كاملة"


def test_another_tenants_failed_message_cannot_speak_on_this_card(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Constant 10, on a query that now reaches a second table.

    The bundle id alone would already isolate this; the tenant predicate is
    there so that a row which somehow carries the wrong bundle id cannot put a
    sentence about Meta on an innocent customer's card. Seeded as exactly that
    row, because a guard nobody attacked is a guard nobody has tested.
    """
    t1, t2 = two_tenants
    did = _seed_bundle(owner_session, t1, status="EXPIRED_WINDOW",
                       run_date="2026-07-14", delivered=[],
                       message_statuses=["sent"])
    _seed_bundle(owner_session, t2, status="EXPIRED_WINDOW",
                 run_date="2026-07-14", delivered=[],
                 message_statuses=["failed"])
    # …and the neighbour's refusal, mis-pointed at t1's bundle
    owner_session.execute(sql_text(
        "UPDATE delivery_messages SET delivery_id = :d WHERE tenant_id = :t"),
        {"d": did, "t": t2})
    owner_session.commit()

    card = console._tenant_card(owner_session, code=_code_of(owner_session, t1),
                                now=NOW)

    assert card is not None
    assert card["last_delivery"][views.REFUSED_KEY] is False
