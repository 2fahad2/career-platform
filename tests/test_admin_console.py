"""Watchtower console — router auth, screens, stateless callbacks, PII-free
views. Renderers are golden-tested pure; DB screens run on career_test."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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


def _msg(chat_id: str, text: str = "/start") -> dict[str, Any]:
    return {"message": {"chat": {"id": int(chat_id)}, "text": text}}


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
    assert "لا تشغيلة مسجلة" in text
    assert "لا حالات عملاء" in text


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
