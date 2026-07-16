"""Search-policy + activation acceptance tests (whitepaper §05) — before code.

The policy derives from the three authorities built earlier — the approved
path assessment, the customer profile, and the plan's entitlements — is
presented as one Arabic summary card, and only «تأكيد وبدء البحث» makes it
active (versioned; a new confirmation supersedes). Activation completes the
journey: onboarding → ACTIVE with completed_at as the 30-day anchor (the
countdown starts at completion, NOT at payment), and the subscription moves
ONBOARDING → ACTIVE with the period stamped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text

from career.db.session import tenant_session
from career.onboarding import collection, confirmation, fsm, paths, policy

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _seed_everything(session, tenant_id: uuid.UUID) -> None:
    """Subscription(ONBOARDING) + onboarding session + profile + approved paths."""
    session.execute(
        sql_text(
            "INSERT INTO subscriptions "
            "(id, tenant_id, plan_code, status, salla_order_id, amount_sar, currency) "
            "VALUES (:id, :tid, 'basic', 'ONBOARDING', :oid, 149, 'SAR')"
        ),
        {"id": str(uuid.uuid4()), "tid": str(tenant_id), "oid": f"ORD-{uuid.uuid4()}"},
    )
    sub_id = session.execute(sql_text("SELECT id FROM subscriptions")).scalar_one()
    session.execute(
        sql_text(
            "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id, state) "
            "VALUES (:id, :tid, :sid, 'SEARCH_POLICY_REVIEW')"
        ),
        {"id": str(uuid.uuid4()), "tid": str(tenant_id), "sid": str(sub_id)},
    )
    for key, value in (
        ("cv_full_name", "Fahad A"),
        ("city", "الرياض"),
        ("expected_salary_sar", Decimal("12000")),
        ("remote_preference", "hybrid"),
        ("willing_to_relocate", True),
    ):
        collection.apply_answer(session, tenant_id=tenant_id, key=key, value=value)
    confirmation.add_conversation_fact(
        session, tenant_id=tenant_id, category="experience",
        payload={"title": "Senior Business Analyst", "employer": "Acme"},
    )
    confirmation.add_conversation_fact(
        session, tenant_id=tenant_id, category="skill", payload={"name": "SQL"}
    )
    assessment = paths.assess(session, tenant_id=tenant_id, requested_path="محلل أعمال")
    paths.approve(
        session, tenant_id=tenant_id, assessment_id=assessment.id,
        primary="business_analyst", secondary="it_operations",
    )


# ── the draft derives from the three authorities ─────────────────────────────


def test_draft_policy_derives_from_profile_paths_and_plan(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_everything(s, tid)
        draft = policy.build_draft_policy(s, tenant_id=tid)
        assert draft.status == "draft"
        assert draft.version == 1
        assert draft.approved_paths == {
            "primary": "business_analyst", "secondary": "it_operations", "stretch": None,
        }
        assert draft.cities == {"cities": ["الرياض"], "region": None,
                                "willing_to_relocate": True}
        assert draft.min_salary_sar == Decimal("12000")
        assert draft.unknown_salary_policy == "balanced"  # D4 default
        assert draft.remote_policy == "hybrid"
        assert draft.daily_job_limit == 1  # basic plan entitlement snapshot


def test_draft_requires_an_approved_assessment(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        with pytest.raises(policy.MissingPrerequisite):
            policy.build_draft_policy(s, tenant_id=tid)


def test_versions_increment(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_everything(s, tid)
        first = policy.build_draft_policy(s, tenant_id=tid)
        second = policy.build_draft_policy(s, tenant_id=tid)
    assert (first.version, second.version) == (1, 2)


# ── the Arabic summary card ──────────────────────────────────────────────────


def test_summary_card_names_every_dimension(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_everything(s, tid)
        draft = policy.build_draft_policy(s, tenant_id=tid)
        card = policy.render_summary_card(draft)
    assert "محلل أعمال" in card.text_ar          # approved path, human label
    assert "الرياض" in card.text_ar               # city
    assert "12000" in card.text_ar                # target salary
    assert "هجين" in card.text_ar                 # remote policy in Arabic
    assert "1" in card.text_ar                    # daily limit
    assert card.confirm_button_ar == "تأكيد وبدء البحث"


# ── confirmation: draft → active, superseding earlier actives ────────────────


def test_confirm_activates_and_supersedes(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_everything(s, tid)
        v1 = policy.build_draft_policy(s, tenant_id=tid)
        policy.confirm_policy(s, tenant_id=tid, policy_id=v1.id)
        v2 = policy.build_draft_policy(s, tenant_id=tid)
        policy.confirm_policy(s, tenant_id=tid, policy_id=v2.id)
        active = policy.active_policy(s, tenant_id=tid)
        assert active is not None and active.id == v2.id
    with tenant_session(a) as s:
        statuses = [
            r.status
            for r in s.execute(sql_text("SELECT status FROM search_policies")).all()
        ]
    assert statuses.count("active") == 1
    assert statuses.count("superseded") == 1


# ── activation: the end of the journey, and the 30-day anchor ────────────────


def test_activate_completes_journey_and_stamps_the_period(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_everything(s, tid)
        draft = policy.build_draft_policy(s, tenant_id=tid)
        policy.confirm_policy(s, tenant_id=tid, policy_id=draft.id)
        # walk the journey to the gate state
        s.execute(
            sql_text("UPDATE onboarding_sessions SET state = 'READY_FOR_ACTIVATION'")
        )
        policy.activate(s, tenant_id=tid, now=NOW)
    with tenant_session(a) as s:
        onboarding = s.execute(
            sql_text("SELECT state, completed_at FROM onboarding_sessions")
        ).one()
        sub = s.execute(
            sql_text(
                "SELECT status, current_period_start, current_period_end "
                "FROM subscriptions"
            )
        ).one()
        events = s.execute(
            sql_text("SELECT event_type, from_status, to_status FROM subscription_events")
        ).all()
    assert onboarding.state == "ACTIVE"
    assert onboarding.completed_at == NOW                      # countdown anchor
    assert sub.status == "ACTIVE"
    assert sub.current_period_start == NOW
    assert sub.current_period_end == NOW + timedelta(days=30)  # from completion,
    assert ("onboarding_completed", "ONBOARDING", "ACTIVE") in [    # not payment
        (e.event_type, e.from_status, e.to_status) for e in events
    ]


def test_activate_refuses_without_active_policy_or_wrong_state(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_everything(s, tid)
        # no confirmed policy yet
        with pytest.raises(policy.MissingPrerequisite):
            policy.activate(s, tenant_id=tid, now=NOW)
        draft = policy.build_draft_policy(s, tenant_id=tid)
        policy.confirm_policy(s, tenant_id=tid, policy_id=draft.id)
        # journey is in SEARCH_POLICY_REVIEW, not READY_FOR_ACTIVATION
        with pytest.raises(fsm.InvalidTransition):
            policy.activate(s, tenant_id=tid, now=NOW)


# ── the claim deadline (7 days from payment — §16 pending, configurable) ─────


def test_claim_deadline_is_pure_and_configurable() -> None:
    paid_at = NOW
    assert not policy.claim_deadline_passed(paid_at, now=NOW + timedelta(days=6))
    assert policy.claim_deadline_passed(paid_at, now=NOW + timedelta(days=7, seconds=1))
    assert not policy.claim_deadline_passed(
        paid_at, now=NOW + timedelta(days=10), deadline_days=14
    )
