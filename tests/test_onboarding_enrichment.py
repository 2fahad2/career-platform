"""F-ENRICH state honesty: a session that cannot proceed must not stay open.

Two ways an enrichment cursor used to sit `open` for ever, each of which is a
customer being held in a conversation that nothing on our side can finish:

1. THE REPLY WITH NO RENDERER. `orchestrator.handle_enrichment` decided its
   own CAPABILITY before it decided OWNERSHIP: the first line returned False
   when `deps.achievement_renderer is None`, so a process without a renderer
   disowned a message that unambiguously belongs to an open enrichment
   session. The worker fell through to its generic fallback, the answer we
   had asked for was discarded, and the cursor stayed `open` — the state
   persisted, the conversation did not.

2. THE CURSOR WITH NO CLOCK. `enrichment.run_hourly_sweep` ages a session by
   its `opened_at`; when that value was missing or unreadable it set the age
   to None and skipped the row silently, so the 72h auto-close could never
   fire. That is not harmless clutter: `enqueue_enrichment` refuses while a
   session is open (so the customer never gets another nudge, for any role,
   ever) and `promises.career_session._mid_flow` reads the same flag, so the
   لمّاح+ direct line stops escalating anything that customer writes.

Both tests seed the real tables and assert on the rows, not on return values
alone — the bug in each case is what was LEFT BEHIND.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.onboarding import enrichment as enr
from career.onboarding import orchestrator as orch
from career.whatsapp.client import FakeWhatsAppClient

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
PHONE = "+966500000077"


def _seed(
    session: Session, tenant_id: str, *, cursor: dict[str, Any],
    completed_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Thin role + ASKED ledger row + ACTIVE journey carrying `cursor`."""
    role_id, sub_id, channel_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session.execute(sql_text(
        "INSERT INTO profile_facts (id, tenant_id, category, payload, status,"
        " source) VALUES (:i, :t, 'experience', CAST(:p AS jsonb),"
        " 'CUSTOMER_CONFIRMED', 'test')"),
        {"i": str(role_id), "t": tenant_id, "p": json.dumps(
            {"title": "Network Engineer", "employer": "Ericsson",
             "start_date": "2021-01", "end_date": "2023-01",
             "achievements": []})})
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t, 'basic',"
        " 'ACTIVE', :o, 149, 'SAR')"),
        {"i": str(sub_id), "t": tenant_id, "o": f"O-{uuid.uuid4()}"})
    session.execute(sql_text(
        "INSERT INTO customer_channels (id, tenant_id, subscription_id,"
        " provider, phone_e164, verified_at, opt_in_at, last_inbound_at)"
        " VALUES (:i, :t, :s, 'whatsapp', :p, :n, :n, :n)"),
        {"i": str(channel_id), "t": tenant_id, "s": str(sub_id),
         "p": PHONE, "n": NOW})
    session.execute(sql_text(
        "INSERT INTO role_enrichments (id, tenant_id, fact_id, status,"
        " trigger, asked_at) VALUES (:i, :t, :f, 'ASKED', 'lazy_generation',"
        " :n)"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "f": str(role_id), "n": NOW})
    session.execute(sql_text(
        "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
        " channel_id, state, context, completed_at) VALUES (:i, :t, :s, :c,"
        " 'ACTIVE', CAST(:x AS jsonb), :d)"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "s": str(sub_id),
         "c": str(channel_id), "d": completed_at,
         "x": json.dumps({"enrichment": {**cursor, "current": str(role_id)}})})
    session.commit()
    return role_id, channel_id


def _cleanup(session: Session, tenant_id: str) -> None:
    for table in ("delivery_messages", "role_enrichments",
                  "onboarding_sessions", "customer_channels", "subscriptions",
                  "profile_facts"):
        session.execute(sql_text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                        {"t": tenant_id})
    session.commit()


def _cursor(session: Session, tenant_id: str) -> dict[str, Any]:
    ctx = session.execute(sql_text(
        "SELECT context FROM onboarding_sessions WHERE tenant_id = :t"),
        {"t": tenant_id}).scalar_one()
    return (ctx or {}).get("enrichment") or {}


def _role_status(session: Session, role_id: uuid.UUID) -> str:
    return session.execute(sql_text(
        "SELECT status FROM role_enrichments WHERE fact_id = :f"),
        {"f": str(role_id)}).scalar_one()


# ── 1. the reply that arrives at a process with no renderer ─────────────────


def test_answer_with_no_renderer_closes_the_session_instead_of_disowning_it(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The customer answered the question WE asked. Whatever we can or cannot
    do with the answer, the message is ours: it must be consumed (never fall
    through to the worker's generic fallback, never become a ticket), the
    customer must hear something that does not blame them, and the cursor
    must not be left waiting for a turn that can never come."""
    a, _ = two_tenants
    role_id, channel_id = _seed(
        owner_session, a,
        cursor={"open": True, "opened_at": NOW.isoformat(), "attempts": 0},
    )
    deps = orch.Deps(
        whatsapp_client=FakeWhatsAppClient(), scanner=object(),
        storage=object(), extractor=object(),
        achievement_renderer=None,          # the dataclass default
    )
    try:
        handled = orch.handle_enrichment(
            owner_session, channel_id=channel_id,
            text="قللت وقت الإغلاق الشهري من عشرة أيام لأربعة",
            deps=deps, now=NOW,
        )
        owner_session.commit()
        assert handled is True, \
            "an answer to our own enrichment question was disowned"
        assert _cursor(owner_session, a).get("open") is False, \
            "the session stayed open for a flow that cannot run"
        assert _role_status(owner_session, role_id) == "SKIPPED", \
            "the ledger row still claims we are waiting for an answer"
        assert deps.whatsapp_client.sent, "the customer heard nothing back"
    finally:
        _cleanup(owner_session, a)


def test_a_confirm_tap_with_no_renderer_still_promotes_the_draft(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The counterweight to «no renderer ⟹ stop»: promoting a draft that
    already exists needs no model at all. A bullet the customer rendered on
    one deploy and approved on the next must never be thrown away by OUR
    wiring — the very thing closing the session would have done."""
    a, _ = two_tenants
    draft_id = uuid.uuid4()
    role_id, channel_id = _seed(
        owner_session, a,
        cursor={"open": True, "opened_at": NOW.isoformat(),
                "pending_fact_id": str(draft_id),
                "draft_fact_id": str(draft_id)},
    )
    owner_session.execute(sql_text(
        "INSERT INTO profile_facts (id, tenant_id, category, payload, status,"
        " source) VALUES (:i, :t, 'achievement', CAST(:p AS jsonb),"
        " 'EXTRACTED', 'conversation_achievement')"),
        {"i": str(draft_id), "t": a, "p": json.dumps(
            {"text": "Reduced monthly close from ten days to four.",
             "experience_fact_id": str(role_id), "lang": "en",
             "arabic_gloss": "قللت وقت الإغلاق"})})
    owner_session.commit()
    deps = orch.Deps(
        whatsapp_client=FakeWhatsAppClient(), scanner=object(),
        storage=object(), extractor=object(), achievement_renderer=None,
    )
    try:
        assert orch.handle_enrichment(
            owner_session, channel_id=channel_id, text=enr.BTN_OK,
            deps=deps, now=NOW,
        ) is True
        owner_session.commit()
        status = owner_session.execute(sql_text(
            "SELECT status FROM profile_facts WHERE id = :i"),
            {"i": str(draft_id)}).scalar_one()
        assert status == "CUSTOMER_CONFIRMED"
        assert _role_status(owner_session, role_id) == "ENRICHED"
        assert _cursor(owner_session, a).get("open") is False
    finally:
        _cleanup(owner_session, a)


# ── 2. the cursor the sweep could never age ─────────────────────────────────


def test_sweep_closes_a_session_whose_opened_at_is_missing(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """No `opened_at` ⟹ no age ⟹ the 72h auto-close never fired and the
    cursor was open for life. An unreadable clock now reads as EXPIRED —
    the only reading that can ever resolve itself."""
    a, _ = two_tenants
    role_id, _channel_id = _seed(
        owner_session, a, cursor={"open": True},      # no opened_at at all
        completed_at=NOW - timedelta(days=30),
    )
    try:
        counts = enr.run_hourly_sweep(
            owner_session, whatsapp_client=FakeWhatsAppClient(),
            examples_writer=None, now=NOW,
        )
        owner_session.commit()
        assert counts["auto_closed"] >= 1
        assert _cursor(owner_session, a).get("open") is False
        assert _role_status(owner_session, role_id) == "SKIPPED"
    finally:
        _cleanup(owner_session, a)


def test_sweep_survives_a_naive_opened_at(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """A naive timestamp compared against an aware `now` raised TypeError
    straight out of the sweep — taking the §20 outcome questions and the
    weekly report that run after it down with it, every hour."""
    a, _ = two_tenants
    naive = (NOW - timedelta(days=5)).replace(tzinfo=None).isoformat()
    role_id, _channel_id = _seed(
        owner_session, a, cursor={"open": True, "opened_at": naive},
        completed_at=NOW - timedelta(days=30),
    )
    try:
        counts = enr.run_hourly_sweep(
            owner_session, whatsapp_client=FakeWhatsAppClient(),
            examples_writer=None, now=NOW,
        )
        owner_session.commit()
        assert counts["auto_closed"] >= 1
        assert _cursor(owner_session, a).get("open") is False
        assert _role_status(owner_session, role_id) == "SKIPPED"
    finally:
        _cleanup(owner_session, a)


def test_a_live_session_with_a_readable_clock_is_left_alone(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """The counterweight: «unreadable ⟹ expired» must not become «expired».
    A session opened an hour ago is still the customer's turn."""
    a, _ = two_tenants
    role_id, _channel_id = _seed(
        owner_session, a,
        cursor={"open": True,
                "opened_at": (NOW - timedelta(hours=1)).isoformat()},
        completed_at=NOW - timedelta(days=30),
    )
    try:
        enr.run_hourly_sweep(
            owner_session, whatsapp_client=FakeWhatsAppClient(),
            examples_writer=None, now=NOW,
        )
        owner_session.commit()
        assert _cursor(owner_session, a).get("open") is True
        assert _role_status(owner_session, role_id) == "ASKED"
    finally:
        _cleanup(owner_session, a)
