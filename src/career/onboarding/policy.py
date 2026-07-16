"""Search policy + activation — the end of the onboarding journey (§05).

The policy derives from the three authorities built earlier in C5 — the
approved path assessment (C5.8), the customer profile (C5.4), and the plan's
entitlements (C3 seed) — is rendered as ONE Arabic summary card, and becomes
active only on «تأكيد وبدء البحث». Policies are versioned: every draft gets
the next version and a new confirmation supersedes the previous active one
(the C6 gate reads exactly one active version per tenant).

Activation is the final transition: journey READY_FOR_ACTIVATION → ACTIVE
with ``completed_at`` stamped as the 30-day anchor — the countdown starts at
completion, NOT at payment (§05) — and the subscription moves
ONBOARDING → ACTIVE with its period stamped through the validated transition
authority (subscription_events recorded). The 7-day claim deadline from
payment is a pure, configurable rule (§16 pending — default 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import (
    CustomerProfile,
    OnboardingSession,
    PlanEntitlement,
    SearchPolicy,
    Subscription,
)
from career.onboarding import fsm
from career.onboarding.paths import DEFAULT_FAMILIES, active_assessment
from career.salla import subscriptions as sub_states
from career.salla.subscriptions import transition

#: §16 pending — the default per the whitepaper text (بمهلة 7 أيام من الدفع).
DEFAULT_CLAIM_DEADLINE_DAYS = 7

_SUBSCRIPTION_DAYS = 30

_REMOTE_AR = {"onsite": "حضوري", "remote": "عن بعد", "hybrid": "هجين", "any": "الكل"}
_FAMILY_LABELS = {f.key: f.label_ar for f in DEFAULT_FAMILIES}


class MissingPrerequisite(Exception):
    """A required earlier step (approved paths, profile, active policy…) is absent."""


class PolicyNotFound(Exception):
    """The policy id does not exist for this tenant."""


T = TypeVar("T")


def _one_or_missing(session: Session, model: type[T], tenant_id: uuid.UUID, what: str) -> T:
    row = session.execute(
        select(model).where(model.tenant_id == tenant_id)  # type: ignore[attr-defined]
    ).scalars().first()
    if row is None:
        raise MissingPrerequisite(what)
    return row


def build_draft_policy(session: Session, *, tenant_id: uuid.UUID) -> SearchPolicy:
    """Derive the next-version draft from the approved assessment, the
    profile, and the plan entitlement snapshot."""
    assessment = active_assessment(session, tenant_id=tenant_id)
    if assessment is None or not assessment.approved:
        raise MissingPrerequisite("approved career-path assessment")
    profile = _one_or_missing(session, CustomerProfile, tenant_id, "customer profile")
    subscription = _one_or_missing(session, Subscription, tenant_id, "subscription")

    entitlement = session.execute(
        select(PlanEntitlement).where(PlanEntitlement.plan_code == subscription.plan_code)
    ).scalar_one_or_none()
    daily_limit = entitlement.daily_job_limit if entitlement is not None else None

    next_version = (
        session.execute(
            select(func.coalesce(func.max(SearchPolicy.version), 0)).where(
                SearchPolicy.tenant_id == tenant_id
            )
        ).scalar_one()
        + 1
    )

    draft = SearchPolicy(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        version=next_version,
        status="draft",
        approved_paths=dict(assessment.approved),
        cities={
            "cities": [profile.city] if profile.city else [],
            "region": profile.region,  # CHANGELOG v1.1 §9 — gate matching
            "willing_to_relocate": bool(profile.willing_to_relocate),
        },
        min_salary_sar=profile.expected_salary_sar,  # D4: soft target, not a gate
        unknown_salary_policy="balanced",  # D4 default; صارمة/واسعة خيار العميل
        remote_policy=profile.remote_preference,
        sectors_preferred={"sectors": []},
        sectors_avoided={"sectors": []},
        banned_companies={"companies": []},
        daily_job_limit=daily_limit,
    )
    session.add(draft)
    session.flush()
    return draft


# ── the Arabic summary card ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SummaryCard:
    text_ar: str
    confirm_button_ar: str


def render_summary_card(draft: SearchPolicy) -> SummaryCard:
    approved = draft.approved_paths or {}
    path_labels = "، ".join(
        _FAMILY_LABELS.get(key, key)
        for key in (approved.get("primary"), approved.get("secondary"), approved.get("stretch"))
        if key
    )
    cities = "، ".join((draft.cities or {}).get("cities", [])) or "غير محددة"
    salary = (
        f"{draft.min_salary_sar:.0f} ريال (رقم مستهدف — ما نحجب الوظائف غير المعلنة)"
        if draft.min_salary_sar is not None
        else "غير محدد"
    )
    remote = _REMOTE_AR.get(draft.remote_policy or "", "غير محدد")
    relocate = "نعم" if (draft.cities or {}).get("willing_to_relocate") else "لا"
    limit = draft.daily_job_limit if draft.daily_job_limit is not None else "حسب الباقة"

    text = (
        "📋 ملخص سياسة البحث:\n"
        f"• المسارات المعتمدة: {path_labels}\n"
        f"• المدن: {cities} (الانتقال: {relocate})\n"
        f"• الراتب المستهدف: {salary}\n"
        f"• نمط العمل: {remote}\n"
        f"• حد الفرص اليومي: {limit}\n"
        "لو كل شي تمام، اضغط الزر ونبدأ البحث لك من الليلة."
    )
    return SummaryCard(text_ar=text, confirm_button_ar="تأكيد وبدء البحث")


# ── confirmation and the single active version ───────────────────────────────


def confirm_policy(
    session: Session, *, tenant_id: uuid.UUID, policy_id: uuid.UUID
) -> SearchPolicy:
    draft = session.execute(
        select(SearchPolicy).where(
            SearchPolicy.tenant_id == tenant_id, SearchPolicy.id == policy_id
        )
    ).scalar_one_or_none()
    if draft is None:
        raise PolicyNotFound(str(policy_id))
    for earlier in session.execute(
        select(SearchPolicy).where(
            SearchPolicy.tenant_id == tenant_id, SearchPolicy.status == "active"
        )
    ).scalars():
        earlier.status = "superseded"
    draft.status = "active"
    draft.confirmed_at = func.now()
    session.flush()
    return draft


def active_policy(session: Session, *, tenant_id: uuid.UUID) -> SearchPolicy | None:
    """The single version the C6 gate reads."""
    return session.execute(
        select(SearchPolicy).where(
            SearchPolicy.tenant_id == tenant_id, SearchPolicy.status == "active"
        )
    ).scalar_one_or_none()


# ── activation: journey complete, countdown starts ───────────────────────────


def activate(session: Session, *, tenant_id: uuid.UUID, now: datetime) -> None:
    """READY_FOR_ACTIVATION → ACTIVE, everywhere it matters, atomically:

    - the journey's ``completed_at`` = ``now`` — the 30-day countdown anchor
      (starts at completion, not payment — §05);
    - the subscription ONBOARDING → ACTIVE via the validated transition
      authority, period stamped [now, now+30d].
    """
    if active_policy(session, tenant_id=tenant_id) is None:
        raise MissingPrerequisite("active search policy")
    journey = _one_or_missing(session, OnboardingSession, tenant_id, "onboarding session")
    fsm.validate_transition(journey.state, "ACTIVE")

    subscription = _one_or_missing(session, Subscription, tenant_id, "subscription")
    transition(
        session,
        subscription,
        sub_states.ACTIVE,
        event_type="onboarding_completed",
    )
    subscription.current_period_start = now
    subscription.current_period_end = now + timedelta(days=_SUBSCRIPTION_DAYS)

    journey.state = "ACTIVE"
    journey.state_entered_at = func.now()
    journey.completed_at = now
    session.flush()


# ── the claim deadline (pure, configurable — §16 pending) ────────────────────


def claim_deadline_passed(
    paid_at: datetime,
    *,
    now: datetime,
    deadline_days: int = DEFAULT_CLAIM_DEADLINE_DAYS,
) -> bool:
    """True when the 7-day window to claim/start onboarding has lapsed."""
    return now > paid_at + timedelta(days=deadline_days)
