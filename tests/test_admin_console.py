"""Watchtower console — router auth, screens, stateless callbacks, PII-free
views. Renderers are golden-tested pure; DB screens run on career_test."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.telegram import views
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
        assert "سُلّم 2" in text
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
    assert "🟠 ينتهي بعد 5" in text
    assert "باقي 9000 من 10000" in text
    assert "⏳ 5 معلق" in text
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
        assert "99" not in text            # yesterday's counters stay away
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
        assert "المكتشف: 12" in out2[1].text
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
    assert "دقائق دعم: 5" in out[1].text
    out2 = handle_update(
        owner_session, _cbq(ADMIN, f"v1|log|{code}|review"),
        admin_chat_id=ADMIN, probes=FakeProbes(), now=NOW,
    )
    assert "مراجعات بشرية: 1" in out2[1].text
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
    assert "احترافي: 2" in report
    assert "558 ريال" in report
    assert "قدّم 3/4 (75٪)" in report
    assert "موقف الرسائل: 1" in report
    assert "$0.40" in report


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
