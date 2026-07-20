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
