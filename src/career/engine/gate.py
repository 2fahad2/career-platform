"""The per-tenant gate + decision log (whitepaper §06, D4/D10).

Deterministic rules decide eligibility; ranking (C6.7) may only reorder what
passed. The gate composes the ported career_core authorities per tenant:

- **Role weights derive from the approved paths** (D10): primary/secondary/
  stretch family tokens get descending weights calibrated to the legacy map's
  ceiling (30) — Arabic tokens ride along so Arabic ads match too.
- **Salary** (D4): the tenant's soft target feeds the evidence gate; the
  unknown-salary policy wraps the legacy decide_send semantics — صارمة blocks
  UNKNOWN outright؛ متوازنة IS decide_send (strong company+seniority signals
  pass)؛ واسعة passes UNKNOWN with an explicit demotion flag for C6.7.
- **Location**: policy cities (Arabic↔English), remote jobs honor the remote
  policy, relocation opens everything; a MISSING location is doubt, not a
  block. Every verdict is recorded with the §06 record shape so the system
  can always answer «ليش أرسلتوها لي؟», and boundary blocks are captured as
  near-misses (راتب بفارق بسيط، تطابق قرب الحد، tier4 الحدّية، الموقع) for
  the zero-day report.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy.orm import Session

from career.db.models import TenantJobDecision
from career.onboarding.paths import DEFAULT_FAMILIES, PathFamily
from career_core.gate import (
    COMPANY_TIERS_VERSION,
    classify_company_tier,
    company_quality_score,
    compute_fit_score,
    compute_role_match_score,
    decide_send,
    source_quality_score,
)
from career_core.salary import (
    SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT,
    SALARY_GATE_FAIL_LIKELY_LOW,
    SALARY_GATE_PASS_CONFIRMED,
    SALARY_GATE_PASS_LIKELY_HIGH,
    SALARY_GATE_PASS_POSSIBLE_HIGH,
    SALARY_GATE_UNKNOWN,
    score_salary_gate,
)

GATE_POLICY_VERSION = "v1"

_MATCH_FLOOR = 70
_NEAR_MATCH_WINDOW = 10          # تطابق 76 والحد 80 → near-miss (§06)
_NEAR_SALARY_RATIO = 0.85        # explicit salary within 15% of the target

#: whitepaper §06 salary-status vocabulary, mapped from the legacy outcomes.
_SALARY_STATUS = {
    SALARY_GATE_PASS_CONFIRMED: "EXPLICIT_CONFIRMED",
    SALARY_GATE_PASS_LIKELY_HIGH: "INFERRED_HIGH",
    SALARY_GATE_PASS_POSSIBLE_HIGH: "INFERRED_MEDIUM",
    SALARY_GATE_FAIL_LIKELY_LOW: "INFERRED_LOW",
    SALARY_GATE_UNKNOWN: "UNKNOWN",
    SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT: "UNKNOWN",
}

_SLOT_WEIGHTS = {"primary": 30, "secondary": 24, "stretch": 18}

#: Arabic policy city ↔ English job-ad city (lowercase compare tokens).
#: Region-level equivalence (CHANGELOG v1.1 §9): live postings often carry
#: the administrative region («Eastern Province») instead of a city.
_REGION_EQUIV: dict[str, tuple[str, ...]] = {
    "الرياض": ("riyadh province", "riyadh region", "منطقة الرياض"),
    "مكة المكرمة": ("makkah province", "makkah region", "منطقة مكة"),
    "الشرقية": ("eastern province", "eastern region", "الشرقية", "المنطقة الشرقية"),
    "المدينة المنورة": ("madinah province", "madinah region", "منطقة المدينة"),
    "القصيم": ("qassim", "القصيم"),
    "عسير": ("asir", "عسير"),
    "تبوك": ("tabuk", "تبوك"),
    "حائل": ("hail", "حائل"),
    "جازان": ("jazan", "jizan", "جازان"),
}

_CITY_EQUIV: dict[str, tuple[str, ...]] = {
    "الرياض": ("riyadh", "الرياض"),
    "جدة": ("jeddah", "جدة"),
    "الدمام": ("dammam", "khobar", "dhahran", "الدمام", "الخبر", "الظهران"),
    "مكة المكرمة": ("makkah", "mecca", "مكة"),
    "المدينة المنورة": ("madinah", "medina", "المدينة"),
}


@dataclass(frozen=True)
class TenantGatePolicy:
    """The gate-relevant slice of the active search policy (versioned, D4)."""

    approved_paths: dict[str, Any]
    cities_ar: tuple[str, ...]
    willing_to_relocate: bool
    remote_policy: str | None
    min_salary_sar: float | None
    unknown_salary_policy: str  # strict | balanced | wide
    daily_job_limit: int | None
    region_ar: str | None = None  # CHANGELOG v1.1 §9


@dataclass(frozen=True)
class PostingFacts:
    """The gate-relevant slice of a pool posting."""

    title: str
    company: str
    url: str
    location: str | None
    jd_text: str | None


@dataclass(frozen=True)
class GateVerdict:
    decision: str            # PASS | BLOCK (the §06 record vocabulary)
    near_miss: bool
    reasons: dict[str, Any]
    gate_policy_version: str = GATE_POLICY_VERSION


# ── D10: role weights from the approved paths ────────────────────────────────


def build_role_map(
    approved_paths: dict[str, Any],
    registry: tuple[PathFamily, ...] = DEFAULT_FAMILIES,
) -> dict[str, int]:
    by_key = {f.key: f for f in registry}
    role_map: dict[str, int] = {}
    for slot, weight in _SLOT_WEIGHTS.items():
        family = by_key.get(approved_paths.get(slot) or "")
        if family is None:
            continue
        for token in family.title_tokens:
            role_map[token] = max(role_map.get(token, 0), weight)
    return role_map


# ── location ─────────────────────────────────────────────────────────────────


#: Country-level locations are AMBIGUOUS, not mismatch evidence — live lesson
#: (16 Jul, first real run): 13/41 postings carried a bare country location,
#: one titled "… - Riyadh, KSA", and were wrongly blocked.
_COUNTRY_LEVEL_LOCATIONS = frozenset({
    "saudi arabia", "السعودية", "ksa", "kingdom of saudi arabia",
    "المملكة العربية السعودية",
})


def _location_verdict(policy: TenantGatePolicy, location: str | None) -> tuple[str, str]:
    """(PASS|BLOCK|UNKNOWN, detail). A missing OR country-level location is
    doubt, not a block; 'Anywhere' is how the live source spells remote."""
    if not location:
        return "UNKNOWN", "location_missing"
    loc_l = location.lower()
    is_remote = "remote" in loc_l or loc_l.strip() == "anywhere"
    if is_remote and (policy.remote_policy or "") in ("remote", "hybrid", "any"):
        return "PASS", "remote_ok"
    if loc_l.strip() in _COUNTRY_LEVEL_LOCATIONS:
        return "UNKNOWN", "location_country_level"
    for city_ar in policy.cities_ar:
        for token in _CITY_EQUIV.get(city_ar, (city_ar.lower(),)):
            if token in loc_l:
                return "PASS", f"city:{city_ar}"
    if policy.region_ar:
        for token in _REGION_EQUIV.get(
            policy.region_ar, (policy.region_ar.lower(),)
        ):
            if token in loc_l:
                return "PASS", f"region:{policy.region_ar}"
    if policy.willing_to_relocate:
        return "PASS", "relocation_ok"
    return "BLOCK", f"location_mismatch:{location}"


# ── the gate ─────────────────────────────────────────────────────────────────


@lru_cache(maxsize=64)
def _fit_vocab(paths_key: tuple[str, ...]) -> tuple[dict[str, int], tuple[str, ...]]:
    """Signals + strong tokens derived from the tenant's APPROVED families
    (locked decision: nothing specialization-specific pinned in code).
    Generic Saudi-market tokens stay — they are market, not specialization."""
    from career.onboarding.paths import DEFAULT_FAMILIES

    by_key = {f.key: f for f in DEFAULT_FAMILIES}
    signals: dict[str, int] = {
        "saudi": 3, "ksa": 3, "riyadh": 3, "vision 2030": 5,
        "pmp": 5, "governance": 5, "stakeholder": 4,
    }
    tokens: list[str] = []
    for key in paths_key:
        fam = by_key.get(key)
        if fam is None:
            continue
        for alias in fam.query_aliases:
            signals[alias.lower()] = 9
            tokens.append(alias.lower())
    return signals, tuple(tokens)


def _paths_key(approved_paths: dict[str, str | None]) -> tuple[str, ...]:
    return tuple(sorted({v for v in approved_paths.values() if v}))


def _tenant_fit_signals(approved_paths: dict[str, str | None]) -> dict[str, int]:
    return _fit_vocab(_paths_key(approved_paths))[0]


def _tenant_strong_tokens(approved_paths: dict[str, str | None]) -> tuple[str, ...]:
    return _fit_vocab(_paths_key(approved_paths))[1]


def evaluate(policy: TenantGatePolicy, posting: PostingFacts) -> GateVerdict:
    reasons: dict[str, Any] = {}
    near_reasons: list[str] = []

    tier, tier_reason = classify_company_tier(posting.company, posting.url)
    cq = company_quality_score(tier)
    sq = source_quality_score(posting.url)
    role_map = build_role_map(policy.approved_paths)
    role_score = compute_role_match_score(
        posting.title, posting.jd_text, role_map=role_map
    )
    salary = score_salary_gate(
        posting.title,
        min_monthly_sar=float(policy.min_salary_sar or 0.0),
        company=posting.company,
        jd_text=posting.jd_text,
        company_tier=tier,
        source_quality_score=sq,
    )
    fit = compute_fit_score(
        posting.title, posting.company, posting.location or "",
        signals=_tenant_fit_signals(policy.approved_paths),
        strong_tokens=_tenant_strong_tokens(policy.approved_paths),
        hard_reject=(),   # locked decision: no pre-pinned rejects — the role
                          # axis handles family matching
    )
    location_state, location_detail = _location_verdict(policy, posting.location)

    reasons.update({
        "role_score": role_score,
        "salary_outcome": salary.outcome,
        "salary_status": _SALARY_STATUS.get(salary.outcome, "UNKNOWN"),
        "salary_estimate": list(salary.estimated_range) if salary.estimated_range else None,
        "company_tier": tier,
        "company_tier_reason": tier_reason,
        "company_tiers_version": COMPANY_TIERS_VERSION,
        "cq_score": cq,
        "sq_score": sq,
        "location": location_state if location_state != "PASS" else "PASS",
        "location_detail": location_detail,
        "final_score": fit.score,
        "demoted": False,
    })

    # 1) location is a deterministic eligibility axis (§06)
    if location_state == "BLOCK":
        near_reasons.append("location")  # الموقع — near-miss by definition (§06)
        reasons["gate_reason"] = location_detail
        reasons["near_reasons"] = near_reasons
        return GateVerdict("BLOCK", True, reasons)

    # 2) D4 strict policy: UNKNOWN salary never passes
    if policy.unknown_salary_policy == "strict" and salary.outcome == SALARY_GATE_UNKNOWN:
        reasons["gate_reason"] = "unknown_salary_strict_policy"
        reasons["near_reasons"] = near_reasons
        return GateVerdict("BLOCK", False, reasons)

    # 3) the legacy decision authority (== the balanced semantics)
    decision = decide_send(
        salary.outcome,
        role_match_score=role_score,
        company_tier=tier,
        cq_score=cq,
        sq_score=sq,
        match_floor=_MATCH_FLOOR,
    )
    reasons["gate_reason"] = decision.reason

    # 4) D4 letter (conformance audit 16 Jul): INFERRED_MEDIUM is an
    # UNADVERTISED salary with positive evidence — «لا تُحجب افتراضيًا».
    # It passes at the match floor under every policy (strict bans only
    # UNKNOWN); the ranking's salary-certainty key orders it below
    # LIKELY/CONFIRMED, so no demotion flag is needed. The inherited
    # match>=90+cq>=85 rule stays verbatim for pure UNKNOWN.
    if (
        decision.decision == "BLOCK"
        and salary.outcome == SALARY_GATE_PASS_POSSIBLE_HIGH
        and decision.reason.startswith("possible_high")
        and role_score >= _MATCH_FLOOR
    ):
        reasons["gate_reason"] = "possible_high_passes_at_floor"
        reasons["near_reasons"] = near_reasons
        return GateVerdict("PASS", False, reasons)

    # 5) D4 wide policy: an UNKNOWN-salary block becomes a demoted pass
    if (
        policy.unknown_salary_policy == "wide"
        and decision.decision == "BLOCK"
        and salary.outcome == SALARY_GATE_UNKNOWN
        and decision.reason.startswith("unknown_salary")
        and role_score >= _MATCH_FLOOR
    ):
        reasons["demoted"] = True
        reasons["gate_reason"] = "unknown_salary_wide_demoted"
        reasons["near_reasons"] = near_reasons
        return GateVerdict("PASS", False, reasons)

    if decision.decision == "SEND":
        reasons["near_reasons"] = near_reasons
        return GateVerdict("PASS", False, reasons)

    # 6) near-miss capture for boundary blocks (§06)
    if (
        decision.reason.startswith("role_match_too_low")
        and role_score >= _MATCH_FLOOR - _NEAR_MATCH_WINDOW
    ):
        near_reasons.append(f"role_match_near:{role_score}<{_MATCH_FLOOR}")
    if decision.reason.startswith("salary_likely_below_min") and salary.estimated_range:
        lo = salary.estimated_range[0]
        if policy.min_salary_sar and lo >= _NEAR_SALARY_RATIO * float(policy.min_salary_sar):
            near_reasons.append(f"salary_near:{lo}")
    if decision.reason.startswith("tier4_needs"):
        near_reasons.append("tier4_boundary")

    reasons["near_reasons"] = near_reasons
    return GateVerdict("BLOCK", bool(near_reasons), reasons)


# ── persistence: the decision log ────────────────────────────────────────────


@dataclass(frozen=True)
class _Unset:
    pass


def persist_decision(
    owner_session: Session,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    job_posting_id: uuid.UUID,
    verdict: GateVerdict,
    rank: dict[str, Any] | None = None,
    ranking_policy_version: str | None = None,
) -> TenantJobDecision:
    row = TenantJobDecision(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        run_id=run_id,
        job_posting_id=job_posting_id,
        decision=verdict.decision,
        near_miss=verdict.near_miss,
        reasons=verdict.reasons,
        gate_policy_version=verdict.gate_policy_version,
        rank=rank,
        ranking_policy_version=ranking_policy_version,
    )
    owner_session.add(row)
    owner_session.flush()
    return row
