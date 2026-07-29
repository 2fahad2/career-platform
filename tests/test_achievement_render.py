"""F-ENRICH grounding guard (D14): every number/entity in the English bullet
must trace to the raw Arabic answer — zero invention."""

from __future__ import annotations

from career.onboarding.achievement_render import (
    bullet_is_grounded,
    render_achievement,
)

VOCAB = {"stc", "ericsson"}   # confirmed-bank tokens


def _g(english: str, arabic: str) -> tuple[bool, str]:
    return bullet_is_grounded(english, arabic_answer=arabic, vocabulary=VOCAB)


def test_qualitative_bullet_with_no_atoms_is_grounded() -> None:
    ok, _ = _g("Significantly reduced fault-resolution time.",
               "قللت وقت حل الأعطال بشكل كبير")
    assert ok


def test_invented_percentage_is_rejected() -> None:
    ok, reason = _g("Reduced downtime by 40%.", "قللت وقت حل الأعطال بشكل كبير")
    assert not ok and reason == "invented_number"


def test_arabic_number_grounds_english_digit() -> None:
    ok, _ = _g("Led a team of 5 engineers.", "كنت مسؤول عن فريق ٥ أشخاص")
    assert ok


def test_inflated_number_is_rejected() -> None:
    ok, reason = _g("Managed a team of 15.", "كنت مسؤول عن فريق ٥ أشخاص")
    assert not ok and reason == "invented_number"


def test_spelled_out_arabic_number_grounds() -> None:
    ok, _ = _g("Trained five new employees.", "دربت خمسة موظفين جدد")
    assert ok


def test_latin_tool_in_arabic_grounds_english_tool() -> None:
    ok, _ = _g("Coordinated workflow using Jira.", "نظمت شغل الفريق باستخدام Jira")
    assert ok


def test_invented_tool_is_rejected() -> None:
    ok, reason = _g("Coordinated workflow in ServiceNow with ITIL.",
                    "نظمت شغل الفريق")
    assert not ok and reason == "invented_entity"


def test_bank_entity_is_allowed() -> None:
    ok, _ = _g("Monitored the STC network operations center.",
               "راقبت شبكة العمليات على مدار الساعة")
    assert ok                      # "stc" is in the confirmed bank vocabulary


def test_arabic_leak_is_rejected() -> None:
    ok, reason = _g("Reduced وقت resolution.", "قللت الوقت")
    assert not ok and reason == "arabic_leak"


def test_large_number_with_separator_grounds() -> None:
    ok, _ = _g("Served 500+ clients.", "خدمت أكثر من ٥٠٠ عميل")
    assert ok


class _FakeRenderer:
    def __init__(self, results: list[dict]) -> None:
        self._results = results
        self.calls = 0

    def render(self, arabic_answer: str) -> dict:
        r = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return r


def test_render_retries_once_then_accepts_grounded() -> None:
    r = _FakeRenderer([
        {"is_achievement": True, "english_bullet": "Cut time by 40%.",
         "qualitative_only": False, "arabic_gloss": "…"},          # ungrounded
        {"is_achievement": True, "english_bullet": "Significantly cut time.",
         "qualitative_only": True, "arabic_gloss": "قلّلت الوقت"},  # grounded
    ])
    out = render_achievement(r, arabic_answer="قللت الوقت بشكل كبير", vocabulary=VOCAB)
    assert out is not None and out["english_bullet"] == "Significantly cut time."
    assert r.calls == 2


def test_render_returns_none_when_never_grounded() -> None:
    r = _FakeRenderer([
        {"is_achievement": True, "english_bullet": "Cut time by 40%.",
         "qualitative_only": False, "arabic_gloss": "…"},
    ])
    assert render_achievement(r, arabic_answer="قللت الوقت", vocabulary=VOCAB) is None


def test_render_returns_none_for_non_achievement() -> None:
    r = _FakeRenderer([{"is_achievement": False}])
    assert render_achievement(r, arabic_answer="ما ادري", vocabulary=VOCAB) is None


def test_normalize_folds_confirmed_conversation_achievement_into_role() -> None:
    """D14: a CONFIRMED achievement fact linked by experience_fact_id appends
    to its parent role's bullets; unlinked/other roles are untouched."""
    from career.cv import normalize

    bank = {
        "experience": [
            {"_fact_id": "role-1", "title": "Network Engineer",
             "employer": "Ericsson", "start_date": "2021-01",
             "end_date": "2023-01",
             "achievements": ["Monitored the STC network 24/7."]},
            {"_fact_id": "role-2", "title": "IT Specialist",
             "employer": "Sanabel", "start_date": "2020-01",
             "end_date": "2020-11", "achievements": []},
        ],
        "achievement": [
            {"experience_fact_id": "role-1",
             "text": "Led a team of 5 and cut resolution time."},
        ],
        "skill": [{"name": "SQL"}],
    }
    master = normalize.build_master_cv(
        contact={"name": "F", "email": "f@x.com", "phone": "+9665",
                 "location": "Riyadh"},
        bank=bank, headline=None, summary="Base.",
    )
    role1 = next(e for e in master.experience if e.title == "Network Engineer")
    assert "Led a team of 5 and cut resolution time." in role1.achievements
    assert len(role1.achievements) == 2                 # original + folded
    role2 = next(e for e in master.experience if e.title == "IT Specialist")
    assert role2.achievements == []                     # unlinked role untouched


# ── the conversation flow (DB) ───────────────────────────────────────────────

import uuid as _uuid  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

from sqlalchemy import text as _sql  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

_NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


class _StubRenderer:
    def __init__(self, bullet: str, gloss: str = "ملخص") -> None:
        self._bullet, self._gloss = bullet, gloss

    def render(self, arabic_answer: str) -> dict:
        return {"is_achievement": True, "english_bullet": self._bullet,
                "qualitative_only": True, "arabic_gloss": self._gloss}


def _seed_role(session: Session, tenant_id, achievements) -> _uuid.UUID:
    import json
    fid = _uuid.uuid4()
    session.execute(_sql(
        "INSERT INTO profile_facts (id, tenant_id, category, payload, status,"
        " source) VALUES (:id, :t, 'experience', CAST(:p AS jsonb),"
        " 'CUSTOMER_CONFIRMED', 'test')"),
        {"id": str(fid), "t": str(tenant_id),
         "p": json.dumps({"title": "Network Engineer", "employer": "Ericsson",
                          "start_date": "2021-01", "end_date": "2023-01",
                          "achievements": achievements})})
    session.commit()
    return fid


def test_thin_role_detected_and_rich_role_ignored(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    tid = _uuid.UUID(a)
    thin_id = _seed_role(owner_session, tid, ["Only one bullet."])
    _seed_role(owner_session, tid, ["One.", "Two."])   # rich → ignored
    try:
        thin = enr.thin_roles(owner_session, tenant_id=tid)
        ids = {f.id for f in thin}
        assert thin_id in ids and len(thin) == 1
    finally:
        owner_session.execute(_sql(
            "DELETE FROM profile_facts WHERE tenant_id = :t"), {"t": a})
        owner_session.commit()


def test_enqueue_is_once_ever(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    tid = _uuid.UUID(a)
    role_id = _seed_role(owner_session, tid, [])
    try:
        ctx: dict = {}
        assert enr.enqueue_enrichment(
            owner_session, tenant_id=tid, role_fact_id=role_id,
            journey_context=ctx, trigger="lazy_generation", now=_NOW)
        owner_session.commit()
        # a second attempt (fresh context) is refused — row already exists
        assert not enr.enqueue_enrichment(
            owner_session, tenant_id=tid, role_fact_id=role_id,
            journey_context={}, trigger="lazy_generation", now=_NOW)
    finally:
        owner_session.execute(_sql(
            "DELETE FROM role_enrichments WHERE tenant_id = :t"), {"t": a})
        owner_session.execute(_sql(
            "DELETE FROM profile_facts WHERE tenant_id = :t"), {"t": a})
        owner_session.commit()


def test_handle_answer_grounded_stores_pending_then_confirm_promotes(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    tid = _uuid.UUID(a)
    role_id = _seed_role(owner_session, tid, [])
    try:
        enr.enqueue_enrichment(
            owner_session, tenant_id=tid, role_fact_id=role_id,
            journey_context={}, trigger="lazy_generation", now=_NOW)
        r = _StubRenderer("Led a team of 5 and cut resolution time.")
        out = enr.handle_answer(
            owner_session, tenant_id=tid, role_fact_id=role_id,
            arabic_answer="قدت فريق ٥ وقللت وقت الحل", renderer=r, now=_NOW)
        assert out["status"] == "confirm"
        pend = _uuid.UUID(out["pending_fact_id"])
        status = owner_session.execute(_sql(
            "SELECT status FROM profile_facts WHERE id = :i"),
            {"i": str(pend)}).scalar_one()
        assert status == "EXTRACTED"          # pending until confirmed
        enr.confirm_answer(owner_session, tenant_id=tid, pending_fact_id=pend,
                           role_fact_id=role_id, now=_NOW)
        owner_session.commit()
        status2 = owner_session.execute(_sql(
            "SELECT status FROM profile_facts WHERE id = :i"),
            {"i": str(pend)}).scalar_one()
        assert status2 == "CUSTOMER_CONFIRMED"
        enrolled = owner_session.execute(_sql(
            "SELECT status FROM role_enrichments WHERE fact_id = :f"),
            {"f": str(role_id)}).scalar_one()
        assert enrolled == "ENRICHED"
    finally:
        owner_session.execute(_sql(
            "DELETE FROM role_enrichments WHERE tenant_id = :t"), {"t": a})
        owner_session.execute(_sql(
            "DELETE FROM profile_facts WHERE tenant_id = :t"), {"t": a})
        owner_session.commit()


def test_handle_answer_ungrounded_stores_nothing(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    tid = _uuid.UUID(a)
    role_id = _seed_role(owner_session, tid, [])
    try:
        r = _StubRenderer("Reduced downtime by 40%.")   # invented number
        out = enr.handle_answer(
            owner_session, tenant_id=tid, role_fact_id=role_id,
            arabic_answer="قللت الوقت بشكل كبير", renderer=r, now=_NOW)
        assert out["status"] == "reask"
        n = owner_session.execute(_sql(
            "SELECT count(*) FROM profile_facts WHERE tenant_id = :t"
            " AND category = 'achievement'"), {"t": a}).scalar_one()
        assert n == 0                          # nothing stored
    finally:
        owner_session.execute(_sql(
            "DELETE FROM profile_facts WHERE tenant_id = :t"), {"t": a})
        owner_session.commit()


# ── the interactive-button conversation (orchestrator level) ─────────────────

import json as _json  # noqa: E402

from career.onboarding import orchestrator as _orch  # noqa: E402
from career.whatsapp.client import FakeWhatsAppClient  # noqa: E402


def _seed_enrichment_conversation(
    session: Session, tenant_id: str, bullet: str = "Improved reporting."
) -> tuple:
    """tenant → sub + channel + ACTIVE journey with an OPEN enrichment
    session on a seeded thin role. Returns (deps, channel_id, role_id)."""
    tid = _uuid.UUID(tenant_id)
    role_id = _seed_role(session, tid, [])
    sub_id, channel_id = _uuid.uuid4(), _uuid.uuid4()
    session.execute(_sql(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t, 'basic',"
        " 'ACTIVE', :o, 149, 'SAR')"),
        {"i": str(sub_id), "t": tenant_id, "o": f"O-{_uuid.uuid4()}"})
    session.execute(_sql(
        "INSERT INTO customer_channels (id, tenant_id, subscription_id,"
        " provider, phone_e164, verified_at, opt_in_at, last_inbound_at)"
        " VALUES (:i, :t, :s, 'whatsapp', :p, :n, :n, :n)"),
        {"i": str(channel_id), "t": tenant_id, "s": str(sub_id),
         "p": "+966500000001", "n": _NOW})
    session.execute(_sql(
        "INSERT INTO role_enrichments (id, tenant_id, fact_id, status,"
        " trigger, asked_at) VALUES (:i, :t, :f, 'ASKED', 'lazy_generation',"
        " :n)"),
        {"i": str(_uuid.uuid4()), "t": tenant_id, "f": str(role_id), "n": _NOW})
    ctx = {"enrichment": {"open": True, "current": str(role_id), "queue": [],
                          "opened_at": _NOW.isoformat()}}
    session.execute(_sql(
        "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
        " channel_id, state, context) VALUES (:i, :t, :s, :c, 'ACTIVE',"
        " CAST(:x AS jsonb))"),
        {"i": str(_uuid.uuid4()), "t": tenant_id, "s": str(sub_id),
         "c": str(channel_id), "x": _json.dumps(ctx)})
    session.commit()
    deps = _orch.Deps(
        whatsapp_client=FakeWhatsAppClient(), scanner=object(),
        storage=object(), extractor=object(),
        achievement_renderer=_StubRenderer(bullet),
    )
    return deps, channel_id, role_id


def _cleanup_conversation(session: Session, tenant_id: str) -> None:
    for table in ("delivery_messages", "role_enrichments", "onboarding_sessions",
                  "customer_channels", "subscriptions", "profile_facts"):
        session.execute(_sql(
            f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
    session.commit()


def test_button_ids_route_answer_confirm_and_promote(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Answer → interactive confirm prompt (3 buttons) → BTN_OK tap →
    CUSTOMER_CONFIRMED + ENRICHED + thanks."""
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    deps, channel_id, role_id = _seed_enrichment_conversation(owner_session, a)
    try:
        handled = _orch.handle_enrichment(
            owner_session, channel_id=channel_id,
            text="قللت وقت التقارير", deps=deps, now=_NOW)
        assert handled
        sent = deps.whatsapp_client.sent
        assert sent[-1].kind == "interactive"
        assert len(sent[-1].buttons) == 3          # مضبوط/أعدّل/احذفها
        # tap arrives as the machine id
        assert _orch.handle_enrichment(
            owner_session, channel_id=channel_id, text=enr.BTN_OK,
            deps=deps, now=_NOW)
        owner_session.commit()
        status = owner_session.execute(_sql(
            "SELECT status FROM profile_facts WHERE tenant_id = :t"
            " AND category = 'achievement'"), {"t": a}).scalar_one()
        assert status == "CUSTOMER_CONFIRMED"
        enrolled = owner_session.execute(_sql(
            "SELECT status FROM role_enrichments WHERE fact_id = :f"),
            {"f": str(role_id)}).scalar_one()
        assert enrolled == "ENRICHED"
        assert "تسلم" in (deps.whatsapp_client.sent[-1].body or "")
    finally:
        _cleanup_conversation(owner_session, a)


def test_skip_button_id_closes_role_skipped(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    deps, channel_id, role_id = _seed_enrichment_conversation(owner_session, a)
    try:
        assert _orch.handle_enrichment(
            owner_session, channel_id=channel_id, text=enr.BTN_SKIP,
            deps=deps, now=_NOW)
        owner_session.commit()
        enrolled = owner_session.execute(_sql(
            "SELECT status FROM role_enrichments WHERE fact_id = :f"),
            {"f": str(role_id)}).scalar_one()
        assert enrolled == "SKIPPED"
        assert "سيرتك زينة" in (deps.whatsapp_client.sent[-1].body or "")
    finally:
        _cleanup_conversation(owner_session, a)


def test_edit_button_clears_pending_and_reprompts(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    deps, channel_id, _role = _seed_enrichment_conversation(owner_session, a)
    try:
        _orch.handle_enrichment(owner_session, channel_id=channel_id,
                                text="قللت وقت التقارير", deps=deps, now=_NOW)
        assert _orch.handle_enrichment(
            owner_session, channel_id=channel_id, text=enr.BTN_EDIT,
            deps=deps, now=_NOW)
        owner_session.commit()
        ctx = owner_session.execute(_sql(
            "SELECT context FROM onboarding_sessions WHERE tenant_id = :t"),
            {"t": a}).scalar_one()
        assert not (ctx.get("enrichment") or {}).get("pending_fact_id")
        assert "بكلماتك" in (deps.whatsapp_client.sent[-1].body or "")
    finally:
        _cleanup_conversation(owner_session, a)


# ── the icebreaker examples (كاسر التجمّد) ───────────────────────────────────


def test_scrub_rejects_digits_and_foreign_latin() -> None:
    from career.onboarding.achievement_render import scrub_examples

    raw = [
        "كنت مسؤول عن مراقبة الشبكة وحل الأعطال",     # clean → keep
        "دربت ٥ موظفين جدد",                           # digit → drop
        "رفعت الأداء 20 بالمية",                        # digit → drop
        "نظمت الشغل باستخدام Jira",                     # foreign Latin → drop
        "طورت تقارير SAP الشهرية",                      # SAP in payload → keep
    ]
    clean = scrub_examples(
        raw, role_payload_text="SAP Business Analyst — تحليل أنظمة"
    )
    assert clean == [
        "كنت مسؤول عن مراقبة الشبكة وحل الأعطال",
        "طورت تقارير SAP الشهرية",
    ]


def test_examples_message_is_numbered_and_bidi_pure() -> None:
    from career.onboarding.achievement_render import format_examples_message

    msg = format_examples_message(["أول", "ثاني", "ثالث"])
    assert "١) أول" in msg and "٢) ثاني" in msg and "٣) ثالث" in msg
    assert "اختر رقم" in msg


def test_pick_example_maps_arabic_and_western_digits() -> None:
    from career.onboarding import enrichment as enr

    state = {"examples": ["أ", "ب", "ج"]}
    assert enr.pick_example(state, "١") == "أ"
    assert enr.pick_example(state, "2") == "ب"
    assert enr.pick_example(state, "٣") == "ج"
    assert enr.pick_example(state, "٤") is None
    assert enr.pick_example({}, "1") is None


def test_picked_example_becomes_the_answer(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Reply «١» → the stored example text is rendered + confirm prompt."""
    a, _ = two_tenants
    deps, channel_id, _role = _seed_enrichment_conversation(
        owner_session, a, bullet="Monitored the network and resolved faults."
    )
    try:
        owner_session.execute(_sql(
            "UPDATE onboarding_sessions SET context = jsonb_set(context,"
            " '{enrichment,examples}',"
            " '[\"كنت مسؤول عن مراقبة الشبكة وحل الأعطال\"]') WHERE"
            " tenant_id = :t"), {"t": a})
        owner_session.commit()
        assert _orch.handle_enrichment(
            owner_session, channel_id=channel_id, text="١", deps=deps, now=_NOW)
        owner_session.commit()
        src = owner_session.execute(_sql(
            "SELECT payload->>'arabic_source' FROM profile_facts WHERE"
            " tenant_id = :t AND category = 'achievement'"), {"t": a}
        ).scalar_one()
        assert src == "كنت مسؤول عن مراقبة الشبكة وحل الأعطال"
        assert deps.whatsapp_client.sent[-1].kind == "interactive"
    finally:
        _cleanup_conversation(owner_session, a)


# ── the hourly sweep: 72h auto-close + 3-day fallback nudge ──────────────────

from datetime import timedelta as _td  # noqa: E402


def test_stale_open_session_auto_closes_silently(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    deps, _channel, role_id = _seed_enrichment_conversation(owner_session, a)
    try:
        # age the open session past the TTL
        stale = (_NOW - enr.ENRICH_SESSION_TTL - _td(hours=1)).isoformat()
        owner_session.execute(_sql(
            "UPDATE onboarding_sessions SET context = jsonb_set(context,"
            " '{enrichment,opened_at}', to_jsonb(CAST(:s AS text))) WHERE"
            " tenant_id = :t"), {"s": stale, "t": a})
        owner_session.commit()
        wa = FakeWhatsAppClient()
        counts = enr.run_hourly_sweep(
            owner_session, whatsapp_client=wa, examples_writer=None, now=_NOW)
        owner_session.commit()
        assert counts["auto_closed"] == 1
        assert wa.sent == []                      # silent — no message
        ctx = owner_session.execute(_sql(
            "SELECT context FROM onboarding_sessions WHERE tenant_id = :t"),
            {"t": a}).scalar_one()
        assert (ctx.get("enrichment") or {}).get("open") is False
        status = owner_session.execute(_sql(
            "SELECT status FROM role_enrichments WHERE fact_id = :f"),
            {"f": str(role_id)}).scalar_one()
        assert status == "SKIPPED"
    finally:
        _cleanup_conversation(owner_session, a)


def _seed_sweep_candidate(
    session: Session, tenant_id: str, *, active_days: int,
    window_open: bool,
) -> _uuid.UUID:
    """ACTIVE journey (no enrichment context) + thin role + channel."""
    tid = _uuid.UUID(tenant_id)
    role_id = _seed_role(session, tid, [])
    sub_id, channel_id = _uuid.uuid4(), _uuid.uuid4()
    session.execute(_sql(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t, 'basic',"
        " 'ACTIVE', :o, 149, 'SAR')"),
        {"i": str(sub_id), "t": tenant_id, "o": f"O-{_uuid.uuid4()}"})
    session.execute(_sql(
        "INSERT INTO customer_channels (id, tenant_id, subscription_id,"
        " provider, phone_e164, verified_at, opt_in_at, last_inbound_at)"
        " VALUES (:i, :t, :s, 'whatsapp', :p, :n, :n, :li)"),
        {"i": str(channel_id), "t": tenant_id, "s": str(sub_id),
         "p": f"+9665{_uuid.uuid4().int % 10**8:08d}", "n": _NOW,
         "li": _NOW - _td(hours=1) if window_open else None})
    session.execute(_sql(
        "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
        " channel_id, state, completed_at) VALUES (:i, :t, :s, :c, 'ACTIVE',"
        " :done)"),
        {"i": str(_uuid.uuid4()), "t": tenant_id, "s": str(sub_id),
         "c": str(channel_id), "done": _NOW - _td(days=active_days)})
    session.commit()
    return role_id


def test_sweep_nudges_old_active_thin_role_when_window_open(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    role_id = _seed_sweep_candidate(owner_session, a, active_days=4,
                                    window_open=True)
    try:
        wa = FakeWhatsAppClient()
        counts = enr.run_hourly_sweep(
            owner_session, whatsapp_client=wa, examples_writer=None, now=_NOW)
        owner_session.commit()
        assert counts["swept"] == 1
        assert wa.sent[-1].kind == "interactive"     # opening + skip buttons
        row = owner_session.execute(_sql(
            "SELECT status, trigger FROM role_enrichments WHERE fact_id = :f"),
            {"f": str(role_id)}).one()
        assert row.status == "ASKED"
        assert row.trigger == "post_activation_sweep"
        # second run is a no-op — once-ever holds
        wa2 = FakeWhatsAppClient()
        counts2 = enr.run_hourly_sweep(
            owner_session, whatsapp_client=wa2, examples_writer=None, now=_NOW)
        assert counts2["swept"] == 0 and wa2.sent == []
    finally:
        _cleanup_conversation(owner_session, a)


def test_sweep_respects_age_gate_and_closed_window(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    from career.onboarding import enrichment as enr

    a, b = two_tenants
    _seed_sweep_candidate(owner_session, a, active_days=1, window_open=True)
    _seed_sweep_candidate(owner_session, b, active_days=4, window_open=False)
    try:
        wa = FakeWhatsAppClient()
        counts = enr.run_hourly_sweep(
            owner_session, whatsapp_client=wa, examples_writer=None, now=_NOW)
        assert counts["swept"] == 0 and wa.sent == []   # too young / closed
    finally:
        _cleanup_conversation(owner_session, a)
        _cleanup_conversation(owner_session, b)


# ── AUDIT ك-5: recency ordering regression ───────────────────────────────────


def test_thin_roles_picks_most_recent_ended_roles(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    """Three ended thin roles with distinct end dates → the TWO most recent
    are candidates, the oldest is excluded (was inverted before the fix)."""
    import json as _j

    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    tid = _uuid.UUID(a)
    ids = {}
    for title, end in (("Old", "2015-01"), ("Mid", "2020-06"), ("New", "2024-12")):
        fid = _uuid.uuid4()
        owner_session.execute(_sql(
            "INSERT INTO profile_facts (id, tenant_id, category, payload,"
            " status, source) VALUES (:id, :t, 'experience',"
            " CAST(:p AS jsonb), 'CUSTOMER_CONFIRMED', 'test')"),
            {"id": str(fid), "t": a,
             "p": _j.dumps({"title": title, "employer": "X",
                            "start_date": "2010-01", "end_date": end,
                            "achievements": []})})
        ids[title] = fid
    owner_session.commit()
    try:
        thin = enr.thin_roles(owner_session, tenant_id=tid)
        picked = [str((f.payload or {}).get("title")) for f in thin]
        assert picked == ["New", "Mid"]          # most recent first, Old excluded
    finally:
        owner_session.execute(_sql(
            "DELETE FROM profile_facts WHERE tenant_id = :t"), {"t": a})
        owner_session.commit()


def test_current_role_sorts_before_ended_roles(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    import json as _j

    from career.onboarding import enrichment as enr

    a, _ = two_tenants
    tid = _uuid.UUID(a)
    for title, end in (("Ended", "2024-12"), ("Current", "")):
        owner_session.execute(_sql(
            "INSERT INTO profile_facts (id, tenant_id, category, payload,"
            " status, source) VALUES (:id, :t, 'experience',"
            " CAST(:p AS jsonb), 'CUSTOMER_CONFIRMED', 'test')"),
            {"id": str(_uuid.uuid4()), "t": a,
             "p": _j.dumps({"title": title, "employer": "X",
                            "start_date": "2020-01", "end_date": end,
                            "achievements": []})})
    owner_session.commit()
    try:
        thin = enr.thin_roles(owner_session, tenant_id=tid)
        assert [str((f.payload or {}).get("title")) for f in thin] == \
            ["Current", "Ended"]
    finally:
        owner_session.execute(_sql(
            "DELETE FROM profile_facts WHERE tenant_id = :t"), {"t": a})
        owner_session.commit()


def test_examples_schema_has_no_unsupported_array_constraints() -> None:
    """LIVE BUG (29 July): structured outputs reject minItems>1 and maxItems.
    With them present EVERY examples call 400'd and the swallowed error left
    the icebreaker dead in production. Keep the schema constraint-free."""
    from career.onboarding.achievement_render import _EXAMPLES_SCHEMA

    arr = _EXAMPLES_SCHEMA["properties"]["examples"]
    assert "maxItems" not in arr
    assert arr.get("minItems", 0) in (0, 1)
