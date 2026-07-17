"""Fact confirmation — the gate into the achievement bank (whitepaper §05, §15.5).

Extraction is not truth. Every extracted fact is walked one-by-one through a
three-button verdict (صحيح / تعديل / احذفها):

- CUSTOMER_CONFIRMED  — enters the bank as-is;
- CUSTOMER_CORRECTED  — enters the bank with the corrected payload, keeping
  the original payload as an audit trail;
- CUSTOMER_REJECTED   — excluded forever AND recorded in forbidden_claims so
  the CV generator can never make that claim (§15.5);
- OPERATOR_VERIFIED   — the manual-review path, also bank material.

:func:`achievement_bank` is the single authority downstream consumers (C7 CV
generation, C5.8 path assessment) read — nothing EXTRACTED or rejected ever
leaves it. Facts the customer states directly in conversation enter as
CUSTOMER_CONFIRMED (they just asserted them). Questions are asked only about
the missing and the conflicting (§05) — :func:`pending_gaps` names what is
genuinely absent, nothing more.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import ForbiddenClaim, ProfileFact

#: The only statuses whose facts constitute the achievement bank (§15.5).
BANK_STATUSES: frozenset[str] = frozenset(
    {"CUSTOMER_CONFIRMED", "CUSTOMER_CORRECTED", "OPERATOR_VERIFIED"}
)

#: Categories the service cannot work without — the "missing" questions (§05).
_ESSENTIAL_CATEGORIES: tuple[str, ...] = ("experience", "skill")

#: Confirmation walks categories in this order (most identity-defining first).
_CATEGORY_ORDER: tuple[str, ...] = (
    "experience", "education", "certification", "skill", "language", "achievement",
)
_CATEGORY_RANK = {c: i for i, c in enumerate(_CATEGORY_ORDER)}


class FactNotFound(Exception):
    """The fact id does not exist for this tenant."""


class InvalidFactTransition(Exception):
    """A verdict was applied to a fact that is not awaiting one."""


# ── prompt rendering (buttons as data, same idiom as collection) ─────────────


@dataclass(frozen=True)
class Option:
    id: str
    label_ar: str


@dataclass(frozen=True)
class FactPrompt:
    fact_id: uuid.UUID
    prompt_ar: str
    options: tuple[Option, ...]


_VERDICT_OPTIONS: tuple[Option, ...] = (
    Option("confirm", "صحيح ✅"),
    Option("correct", "يحتاج تعديل ✏️"),
    Option("reject", "احذفها ❌"),
)

_CATEGORY_INTRO_AR: dict[str, str] = {
    "experience": "لقينا هذي الخبرة في سيرتك",
    "education": "لقينا هذا المؤهل في سيرتك",
    "certification": "لقينا هذي الشهادة في سيرتك",
    "skill": "لقينا هذي المهارة في سيرتك",
    "language": "لقينا هذي اللغة في سيرتك",
    "achievement": "لقينا هذا الإنجاز في سيرتك",
}


def _fact_summary(fact: ProfileFact) -> str:
    p = fact.payload or {}
    if fact.category == "experience":
        parts = [p.get("title"), p.get("employer")]
        return " — ".join(str(x) for x in parts if x)
    if fact.category == "education":
        parts = [p.get("degree"), p.get("institution")]
        return " — ".join(str(x) for x in parts if x)
    if fact.category == "certification":
        parts = [p.get("name"), p.get("issuer")]
        return " — ".join(str(x) for x in parts if x)
    if fact.category == "skill":
        return str(p.get("name") or "")
    if fact.category == "language":
        return str(p.get("language") or "")
    return str(p.get("text") or p)


def render_fact_prompt(fact: ProfileFact) -> FactPrompt:
    intro = _CATEGORY_INTRO_AR.get(fact.category, "لقينا هذي المعلومة في سيرتك")
    return FactPrompt(
        fact_id=fact.id,
        prompt_ar=f"{intro}:\n«{_fact_summary(fact)}»\nهل هي صحيحة؟",
        options=_VERDICT_OPTIONS,
    )


# ── the cursor ───────────────────────────────────────────────────────────────


def next_fact_to_confirm(session: Session, *, tenant_id: uuid.UUID) -> ProfileFact | None:
    """The first fact still awaiting a verdict, in category order — presence
    of EXTRACTED rows drives the cursor, so the flow resumes anywhere."""
    rows = session.execute(
        select(ProfileFact).where(
            ProfileFact.tenant_id == tenant_id,
            ProfileFact.status == "EXTRACTED",
        )
    ).scalars().all()
    if not rows:
        return None
    return min(rows, key=lambda f: (_CATEGORY_RANK.get(f.category, 99), f.created_at, f.id.hex))


def is_confirmation_complete(session: Session, *, tenant_id: uuid.UUID) -> bool:
    return next_fact_to_confirm(session, tenant_id=tenant_id) is None


# ── verdicts ─────────────────────────────────────────────────────────────────


def _get_awaiting(session: Session, tenant_id: uuid.UUID, fact_id: uuid.UUID) -> ProfileFact:
    fact = session.execute(
        select(ProfileFact).where(
            ProfileFact.tenant_id == tenant_id, ProfileFact.id == fact_id
        )
    ).scalar_one_or_none()
    if fact is None:
        raise FactNotFound(str(fact_id))
    if fact.status != "EXTRACTED":
        raise InvalidFactTransition(
            f"fact {fact_id} is {fact.status}, not awaiting a verdict"
        )
    return fact


def confirm_fact(session: Session, *, tenant_id: uuid.UUID, fact_id: uuid.UUID) -> ProfileFact:
    fact = _get_awaiting(session, tenant_id, fact_id)
    fact.status = "CUSTOMER_CONFIRMED"
    fact.confirmed_at = func.now()
    session.flush()
    return fact


def correct_fact(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    fact_id: uuid.UUID,
    corrected_payload: dict[str, Any],
) -> ProfileFact:
    fact = _get_awaiting(session, tenant_id, fact_id)
    fact.original_payload = dict(fact.payload or {})  # audit trail (§05)
    fact.payload = dict(corrected_payload)
    fact.status = "CUSTOMER_CORRECTED"
    fact.confirmed_at = func.now()
    session.flush()
    return fact


def reject_fact(session: Session, *, tenant_id: uuid.UUID, fact_id: uuid.UUID) -> ProfileFact:
    """Reject AND remember: the rejected claim becomes a forbidden claim the
    generator must never make (§15.5)."""
    fact = _get_awaiting(session, tenant_id, fact_id)
    fact.status = "CUSTOMER_REJECTED"
    session.add(
        ForbiddenClaim(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            claim=_fact_summary(fact),
            source="customer_rejected",
            source_fact_id=fact.id,
        )
    )
    session.flush()
    return fact


def operator_verify(session: Session, *, tenant_id: uuid.UUID, fact_id: uuid.UUID) -> ProfileFact:
    fact = _get_awaiting(session, tenant_id, fact_id)
    fact.status = "OPERATOR_VERIFIED"
    fact.confirmed_at = func.now()
    session.flush()
    return fact


# ── conversation-sourced facts ───────────────────────────────────────────────


def add_conversation_fact(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    category: str,
    payload: dict[str, Any],
) -> ProfileFact:
    """A fact the customer stated directly — customer-confirmed by definition."""
    fact = ProfileFact(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        category=category,
        payload=dict(payload),
        status="CUSTOMER_CONFIRMED",
        source="conversation",
        confirmed_at=func.now(),
    )
    session.add(fact)
    session.flush()
    return fact


# ── the bank: the single downstream authority (§15.5) ────────────────────────


def achievement_bank(session: Session, *, tenant_id: uuid.UUID) -> list[ProfileFact]:
    """Every fact downstream consumers may use — confirm-gated, nothing else."""
    return list(
        session.execute(
            select(ProfileFact)
            .where(
                ProfileFact.tenant_id == tenant_id,
                ProfileFact.status.in_(sorted(BANK_STATUSES)),
            )
            .order_by(ProfileFact.created_at, ProfileFact.id)
        ).scalars().all()
    )


def pending_gaps(session: Session, *, tenant_id: uuid.UUID) -> list[str]:
    """Essential categories with no usable fact yet — ask only about these
    (§05: الأسئلة فقط عن الناقص). EXTRACTED counts as present: it is awaiting
    a verdict, not missing."""
    present = {
        row
        for row in session.execute(
            select(ProfileFact.category).where(
                ProfileFact.tenant_id == tenant_id,
                ProfileFact.status != "CUSTOMER_REJECTED",
            )
        ).scalars()
    }
    return [c for c in _ESSENTIAL_CATEGORIES if c not in present]


# ── batch confirmation (CHANGELOG §10 — the royal-canary decision) ───────────


def facts_awaiting(session: Session, *, tenant_id: uuid.UUID) -> list[ProfileFact]:
    """Every EXTRACTED fact, in the same stable order the one-by-one flow
    used — the batch summary numbers follow this order."""
    return list(session.execute(
        select(ProfileFact)
        .where(ProfileFact.tenant_id == tenant_id,
               ProfileFact.status == "EXTRACTED")
        .order_by(ProfileFact.category, ProfileFact.created_at, ProfileFact.id)
    ).scalars().all())


def render_batch_summary(facts: list[ProfileFact]) -> str:
    """One numbered Arabic message for the whole extraction (§15.5 intact:
    confirm-all grants CUSTOMER_CONFIRMED per item; corrections keep the
    original as the audit trail)."""
    lines = ["هذا ما قرأناه من سيرتك — راجعه سريعًا:"]
    for index, fact in enumerate(facts, start=1):
        lines.append(f"{index}. {_fact_summary(fact)}")
    lines.append("")
    lines.append("لو فيه بند غلط اختر «تعديل بند» — وإلا اضغط «تأكيد الكل».")
    return "\n".join(lines)


def confirm_all(session: Session, *, tenant_id: uuid.UUID) -> int:
    """CUSTOMER_CONFIRMED for every still-EXTRACTED fact. Returns the count."""
    facts = facts_awaiting(session, tenant_id=tenant_id)
    for fact in facts:
        fact.status = "CUSTOMER_CONFIRMED"
    session.flush()
    return len(facts)
