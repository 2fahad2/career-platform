"""Career-path assessment acceptance tests (whitepaper §05) — before the code.

Three layers: requested (what the customer wants) → suggested (what the system
sees: numeric score + strengths + gaps + closest 3 paths) → approved
(Primary/Secondary/Stretch with consent). Insisting on a weak path requires an
explicit, acknowledged override — with an optional ~80/20 realistic/stretch
split. Scoring is deterministic, reads ONLY the confirm-gated bank, and path
families are data (injectable — D5/D10 spirit: no logic-hardwired lists).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sql_text

from career.db.session import tenant_session
from career.onboarding import confirmation, paths

BA_BANK = {
    "experiences": [
        {"title": "Senior Business Analyst", "employer": "Acme"},
        {"title": "Technical Business Analyst", "employer": "Beta Co"},
    ],
    "skills": ["SQL", "Power BI", "Stakeholder Management"],
    "certifications": [{"name": "CBAP", "issuer": "IIBA"}],
}


def _seed_bank(session, tenant_id: uuid.UUID, bank: dict = BA_BANK) -> None:
    for exp in bank.get("experiences", []):
        confirmation.add_conversation_fact(
            session, tenant_id=tenant_id, category="experience", payload=exp
        )
    for skill in bank.get("skills", []):
        confirmation.add_conversation_fact(
            session, tenant_id=tenant_id, category="skill", payload={"name": skill}
        )
    for cert in bank.get("certifications", []):
        confirmation.add_conversation_fact(
            session, tenant_id=tenant_id, category="certification", payload=cert
        )


# ── deterministic scoring against the bank ───────────────────────────────────


def test_matching_path_scores_high_and_mismatched_low(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_bank(s, tid)
        bank = confirmation.achievement_bank(s, tenant_id=tid)
    ba = paths.score_path(paths.family("business_analyst"), bank)
    pilot_like = paths.score_path(paths.family("software_engineering"), bank)
    assert ba.score >= 70
    assert pilot_like.score <= 25
    assert ba.score > pilot_like.score


def test_scoring_is_deterministic(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_bank(s, tid)
        bank = confirmation.achievement_bank(s, tenant_id=tid)
    first = paths.score_path(paths.family("business_analyst"), bank)
    second = paths.score_path(paths.family("business_analyst"), bank)
    assert first == second


def test_strengths_and_gaps_are_concrete_arabic(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_bank(s, tid)
        bank = confirmation.achievement_bank(s, tenant_id=tid)
    strong = paths.score_path(paths.family("business_analyst"), bank)
    assert strong.strengths and any("Senior Business Analyst" in x for x in strong.strengths)
    weak = paths.score_path(paths.family("software_engineering"), bank)
    assert weak.gaps  # a weak fit explains what is missing
    assert all(isinstance(g, str) and g.strip() for g in weak.gaps)


def test_requested_path_resolves_arabic_and_english() -> None:
    assert paths.resolve_requested("محلل أعمال").key == "business_analyst"
    assert paths.resolve_requested("Business Analyst").key == "business_analyst"
    assert paths.resolve_requested("مدير مشاريع").key == "project_manager"
    assert paths.resolve_requested("طيار حربي") is None  # honest: unknown path


# ── the persisted assessment: requested → suggested (closest 3) ──────────────


def test_assess_persists_draft_with_closest_three(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_bank(s, tid)
        assessment = paths.assess(s, tenant_id=tid, requested_path="محلل أعمال")
        assert assessment.status == "draft"
        assert assessment.fit_score is not None and assessment.fit_score >= 70
        suggested = assessment.suggested["closest"]
        assert len(suggested) == 3
        # sorted best-first, and the best is the requested/matching family
        scores = [item["score"] for item in suggested]
        assert scores == sorted(scores, reverse=True)
        assert suggested[0]["path"] == "business_analyst"


def test_unknown_requested_path_gets_honest_low_score(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_bank(s, tid)
        assessment = paths.assess(s, tenant_id=tid, requested_path="طيار حربي")
        assert assessment.fit_score is not None
        assert assessment.fit_score <= paths.WEAK_FIT_THRESHOLD


# ── approval: Primary/Secondary/Stretch, override for weak paths ─────────────


def test_approval_stores_the_three_slots(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_bank(s, tid)
        assessment = paths.assess(s, tenant_id=tid, requested_path="محلل أعمال")
        paths.approve(
            s, tenant_id=tid, assessment_id=assessment.id,
            primary="business_analyst", secondary="it_operations",
            stretch="project_manager",
        )
    with tenant_session(a) as s:
        row = s.execute(
            sql_text(
                "SELECT status, approved, approved_at, customer_override "
                "FROM career_path_assessments"
            )
        ).one()
    assert row.status == "approved"
    assert row.approved == {
        "primary": "business_analyst", "secondary": "it_operations",
        "stretch": "project_manager",
    }
    assert row.approved_at is not None
    assert row.customer_override is False


def test_weak_primary_requires_explicit_override(two_tenants: tuple[str, str]) -> None:
    """§05: إن أصر على مسار ضعيف — customer_override مع الإبلاغ الصريح."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_bank(s, tid)
        assessment = paths.assess(s, tenant_id=tid, requested_path="مهندس برمجيات")
        with pytest.raises(paths.WeakPathRequiresOverride):
            paths.approve(
                s, tenant_id=tid, assessment_id=assessment.id,
                primary="software_engineering",
            )
        paths.approve(
            s, tenant_id=tid, assessment_id=assessment.id,
            primary="software_engineering",
            customer_override=True, stretch_ratio_percent=20,
        )
    with tenant_session(a) as s:
        row = s.execute(
            sql_text(
                "SELECT customer_override, override_acknowledged_at, "
                "stretch_ratio_percent FROM career_path_assessments"
            )
        ).one()
    assert row.customer_override is True
    assert row.override_acknowledged_at is not None  # الإبلاغ الصريح موثق
    assert row.stretch_ratio_percent == 20


def test_new_approval_supersedes_the_previous_one(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_bank(s, tid)
        first = paths.assess(s, tenant_id=tid, requested_path="محلل أعمال")
        paths.approve(s, tenant_id=tid, assessment_id=first.id, primary="business_analyst")
        second = paths.assess(s, tenant_id=tid, requested_path="محلل أعمال")
        paths.approve(s, tenant_id=tid, assessment_id=second.id, primary="business_analyst")
    with tenant_session(a) as s:
        rows = s.execute(
            sql_text("SELECT id, status FROM career_path_assessments ORDER BY created_at")
        ).all()
    statuses = [r.status for r in rows]
    assert statuses.count("approved") == 1
    assert statuses.count("superseded") == 1
    # the approved one is the latest — via the single downstream authority
    with tenant_session(a) as s:
        approved = paths.active_assessment(s, tenant_id=tid)
        assert approved is not None and str(approved.id) == str(second.id)


def test_unknown_assessment_or_family_is_loud(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        with pytest.raises(paths.AssessmentNotFound):
            paths.approve(s, tenant_id=tid, assessment_id=uuid.uuid4(), primary="business_analyst")
    with pytest.raises(paths.UnknownFamily):
        paths.family("astronaut")
