"""Gate & ranking (LEGACY §4) — deterministic rules decide eligibility.

Faithful port of tier classification (§4.1), source-quality scoring (§4.2),
the six-dimension role-match score (§4.3), the SEND/BLOCK decision table
(§4.4) and the discovery-time fit score (§4.5). Per D10 the role weights are
injectable (legacy defaults preserved); per D4 the salary threshold lives in
career_core.salary and the BLOCK reason says "min" instead of "24k".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from career_core.salary import (
    SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT,
    SALARY_GATE_FAIL_LIKELY_LOW,
    SALARY_GATE_PASS_CONFIRMED,
    SALARY_GATE_PASS_LIKELY_HIGH,
    SALARY_GATE_PASS_POSSIBLE_HIGH,
    SALARY_GATE_UNKNOWN,
)

MAX_GATE_SEND = 8

# ── §4.1 Company TIER classification ─────────────────────────────────────────
TIER1_KEYWORDS = frozenset({
    "sama", "saudi central bank", "central bank of saudi", "neom", "saudi aramco",
    "aramco", "sabic", "qiddiya", "gosi", "zatca", "zakat", "public investment fund",
    "pif", "stc", "saudi telecom", "riyad bank", "national commercial bank", "ncb", "snb",
    "alrajhi", "al rajhi", "sabb", "ministry", "وزارة", "هيئة", "مؤسسة النقد",
    "maaden", "saudi electricity", "hrdf", "diriyah", "roshn", "amaala", "taqnia",
    "elm", "stc pay", "national center",
})
TIER2_KEYWORDS = frozenset({
    "delivery hero", "hungerstation", "motorola solutions", "baker hughes",
    "accenture", "deloitte", "kpmg", "pwc", "ernst", "mckinsey", "bcg",
    "bain", "ibm", "oracle", "sap ", "microsoft", "google", "cisco", "dxc",
    "cognizant", "tcs", "infosys", "wipro", "capgemini", "ericsson", "huawei",
    "nokia", "aws", "amazon", "schlumberger", "halliburton", "siemens",
    "booz allen", "mobily", "zain", "etihad etisalat", "alfanar",
    "advanced electronics", "penta consulting", "jasara",
})
_RECRUITER_SIGNALS = ("solutions llc", "staffing", "recruitment", "talent", "manpower",
                      "outsourc", " msp", "consult")

_TIER_TO_QUALITY = {1: 95, 2: 75, 3: 55, 4: 25}


def classify_company_tier(company: str, url: str = "") -> tuple[int, str]:
    company_l = (company or "").lower()
    for kw in TIER1_KEYWORDS:
        if kw in company_l:
            return 1, f"tier1:{kw}"
    for kw in TIER2_KEYWORDS:
        if kw in company_l:
            return 2, f"tier2:{kw}"
    for sig in _RECRUITER_SIGNALS:
        if sig in company_l:
            return 3, f"tier3_recruiter:{sig}"
    if len((company or "").strip()) < 4:
        return 4, "tier4_name_too_short"
    return 3, "tier3_default"


def company_quality_score(tier: int) -> int:
    return _TIER_TO_QUALITY.get(tier, 25)


# ── §4.2 Source-quality scoring (by URL domain) ──────────────────────────────
HIGH_SOURCE_DOMAINS = frozenset({          # direct ATS → 90
    "greenhouse.io", "lever.co", "myworkdayjobs.com",
    "successfactors", "taleo.net", "smartrecruiters.com",
    "bamboohr.com", "workable.com", "recruitee.com", "icims.com",
})
GOOD_SOURCE_DOMAINS = frozenset({          # known board → 65
    "linkedin.com", "bayt.com", "gulftalent.com", "naukrigulf.com", "indeed.com",
})
LOW_SOURCE_DOMAINS = frozenset({           # aggregator → 20
    "jooble.org", "bebee.com", "jobleads.com", "theirstack.com",
    "careerjet", "neuvoo", "adzuna", "ziprecruiter", "simplyhired", "talentify",
    "founditgulf.com", "nationalpostdoc.org",
})


def source_quality_score(url: str | None) -> int:
    if not url:
        return 0
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return 0
    if not host:
        return 0
    for dom in HIGH_SOURCE_DOMAINS:
        if dom in host:
            return 90
    for dom in GOOD_SOURCE_DOMAINS:
        if dom in host:
            return 65
    for dom in LOW_SOURCE_DOMAINS:
        if dom in host:
            return 20
    return 50


# ── §4.3 Role match score (0–100) ────────────────────────────────────────────
_DEV_TOKENS = ("software engineer", "software developer", "full stack",
               "frontend developer", "backend developer", "mobile developer",
               "react developer", "devops engineer", "java developer",
               "python developer", "ios developer", "android developer")
_SUPPORT_TOKENS = ("helpdesk", "help desk", "desktop support", "l1 support",
                   "l2 support", "service desk agent", "field technician",
                   "cabling technician")

DEFAULT_ROLE_MAP: dict[str, int] = {
    "it manager": 30, "it operations manager": 30, "technology manager": 28,
    "it service delivery": 30, "service delivery manager": 28,
    "business analyst": 28, "technical business analyst": 30,
    "business technology": 28, "it governance": 28,
    "digital transformation": 25, "technology operations": 25,
    "it operations": 25, "it lead": 20, "operations manager": 18,
    "information technology manager": 28, "it director": 25,
    "technology partner": 25, "business process analyst": 22,
}

_EXEC_SENIORITY = ("head of", "director", "vp", "chief")                     # 18
_SENIOR_SENIORITY = ("senior", "sr.", "lead", "manager", "principal", "head")  # 20
_CONSULTANT_SENIORITY = ("consultant", "specialist", "partner", "advisor")     # 15
_ANALYST_SENIORITY = ("analyst", "coordinator")                                # 10

_DOMAIN_FIT_GROUPS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (15, ("it operations", "service delivery", "sla", "itil", "servicenow")),
    (14, ("business analyst", "requirements", "user stories", "uat")),
    (13, ("governance", "compliance", "sama", "regulatory", "audit")),
    (12, ("digital transformation", "change management")),
)

DEFAULT_SKILL_HITS: dict[str, int] = {
    "itil": 3, "pmp": 3, "pmi": 2, "prince2": 2, "servicenow": 3, "stakeholder": 2,
    "requirements": 2, "agile": 2, "jira": 1, "governance": 2, "sla": 2, "incident": 1,
    "change management": 2, "business process": 2,
}

_BRIDGE_BUSINESS = ("business", "stakeholder", "requirements", "strategy")
_BRIDGE_TECH = ("technology", "digital", "systems", "service delivery",
                "operations", "infrastructure")  # "it" handled with word boundary
_IT_WORD_RX = re.compile(r"\bit\b")
_GOV_SIDE = ("governance", "sla", "compliance", "regulatory", "sama")
_DELIVERY_SIDE = ("delivery", "incident", "problem", "change", "reporting")


def compute_role_match_score(
    title: str,
    jd_text: str | None = None,
    *,
    role_map: dict[str, int] | None = None,
    skill_hits: dict[str, int] | None = None,
) -> int:
    """Six additive dimensions, capped at 100. Hard reject → 0 (dev/support title)."""
    title_l = (title or "").lower()
    if any(t in title_l for t in _DEV_TOKENS) or any(t in title_l for t in _SUPPORT_TOKENS):
        return 0
    blob = f"{title_l} {(jd_text or '').lower()}"
    rm = role_map if role_map is not None else DEFAULT_ROLE_MAP
    sh = skill_hits if skill_hits is not None else DEFAULT_SKILL_HITS

    role_score = max((pts for phrase, pts in rm.items() if phrase in title_l), default=0)

    if any(t in title_l for t in _EXEC_SENIORITY):
        seniority = 18
    elif any(t in title_l for t in _SENIOR_SENIORITY):
        seniority = 20
    elif any(t in title_l for t in _CONSULTANT_SENIORITY):
        seniority = 15
    elif any(t in title_l for t in _ANALYST_SENIORITY):
        seniority = 10
    else:
        seniority = 5

    domain = max((pts for pts, toks in _DOMAIN_FIT_GROUPS
                  if any(t in blob for t in toks)), default=0)

    skills = min(15, sum(pts for tok, pts in sh.items() if tok in blob))

    bridge = 0
    if any(t in blob for t in _BRIDGE_BUSINESS):
        bridge += 5
    if any(t in blob for t in _BRIDGE_TECH) or _IT_WORD_RX.search(blob):
        bridge += 5

    gov = 0
    if any(t in blob for t in _GOV_SIDE):
        gov += 5
    if any(t in blob for t in _DELIVERY_SIDE):
        gov += 5

    return min(100, role_score + seniority + domain + skills + min(10, bridge) + min(10, gov))


# ── §4.4 SEND / BLOCK decision ───────────────────────────────────────────────

@dataclass(frozen=True)
class GateDecision:
    decision: str  # "SEND" | "BLOCK"
    reason: str


def decide_send(
    salary_outcome: str,
    *,
    role_match_score: int,
    company_tier: int,
    cq_score: int,
    sq_score: int,
    match_floor: int = 70,
) -> GateDecision:
    if salary_outcome == SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT:
        return GateDecision("BLOCK", "junior_or_support_role")
    if role_match_score < match_floor:
        return GateDecision("BLOCK", f"role_match_too_low:{role_match_score}")
    if salary_outcome == SALARY_GATE_FAIL_LIKELY_LOW:
        # Legacy said "salary_likely_below_24k"; threshold is per-tenant now (D4).
        return GateDecision("BLOCK", "salary_likely_below_min")
    if salary_outcome in (SALARY_GATE_PASS_CONFIRMED, SALARY_GATE_PASS_LIKELY_HIGH):
        if company_tier == 4:
            if salary_outcome == SALARY_GATE_PASS_CONFIRMED and role_match_score >= 90:
                return GateDecision("SEND", "tier4_confirmed_strong_match")
            return GateDecision("BLOCK", "tier4_needs_confirmed_salary_and_match>=90")
        return GateDecision("SEND", "salary_pass")
    if salary_outcome == SALARY_GATE_PASS_POSSIBLE_HIGH:
        if role_match_score >= 90 and cq_score >= 85:
            return GateDecision("SEND", "possible_high_strong_signals")
        return GateDecision("BLOCK", "possible_high_needs_match>=90_cq>=85")
    if salary_outcome == SALARY_GATE_UNKNOWN:
        if role_match_score >= 90 and cq_score >= 85 and sq_score >= 70:
            return GateDecision("SEND", "unknown_salary_strong_signals")
        return GateDecision("BLOCK", "unknown_salary_needs_match>=90_cq>=85_sq>=70")
    return GateDecision("BLOCK", f"unrecognized_salary_outcome:{salary_outcome}")


_GATE_ORDER = {
    SALARY_GATE_PASS_CONFIRMED: 0,
    SALARY_GATE_PASS_LIKELY_HIGH: 1,
    SALARY_GATE_PASS_POSSIBLE_HIGH: 2,
    SALARY_GATE_UNKNOWN: 3,
}


def gate_sort_key(
    salary_outcome: str, cq_score: int, role_match_score: int
) -> tuple[int, int, int]:
    """Ranking key for gate-passed jobs: salary certainty, then company quality,
    then role match. Cap the sorted list at MAX_GATE_SEND."""
    return (_GATE_ORDER.get(salary_outcome, 9), -cq_score, -role_match_score)


# ── §4.5 Discovery-time fit score (pre-gate, title/company/location only) ────
_FIT_HARD_REJECT = ("software engineer", "software developer", "full stack",
                    "frontend developer", "backend developer", "mobile developer",
                    "react developer", "node.js developer", "java developer",
                    "python developer", "ios developer", "android developer",
                    "devops engineer")

STRONG_TITLE_TOKENS = ("it manager", "it operations", "service delivery",
                       "business analyst", "business technology", "digital transformation",
                       "technology operations", "it governance", "it service",
                       "technical business analyst", "technology manager", "it lead",
                       "operations manager")

# Insertion order = scan order; reason string shows the first 5 matches.
FIT_SCORE_SIGNALS: dict[str, int] = {
    "itil": 8, "it operations": 10, "service delivery": 8,
    "incident management": 7, "change management": 6, "problem management": 6,
    "sla": 6, "servicenow": 6, "monitoring": 4, "noc": 4,
    "business analyst": 9, "requirements": 6, "user stories": 6, "stakeholder": 6,
    "business process": 5, "gap analysis": 5, "agile": 4, "scrum": 3, "jira": 4, "uat": 4,
    "governance": 7, "compliance": 6, "risk": 5, "audit": 4,
    "regulatory": 6, "sama": 9, "central bank": 7,
    "digital transformation": 8, "technology partner": 6,
    "business technology": 7, "pmp": 5, "pmi": 4, "prince2": 4,
    "it manager": 10, "technology manager": 8, "operations manager": 6,
    "team lead": 4, "head of": 5,
    "saudi": 3, "ksa": 3, "riyadh": 3, "vision 2030": 5, "neom": 3,
    "sabic": 3, "aramco": 3,
}


def fit_label(score: int) -> str:
    if score >= 70:
        return "STRONG"
    if score >= 50:
        return "GOOD"
    if score >= 30:
        return "FAIR"
    return "LOW"


@dataclass(frozen=True)
class FitResult:
    score: int
    label: str
    reason: str


def compute_fit_score(
    title: str,
    company: str = "",
    location: str = "",
    *,
    signals: dict[str, int] | None = None,
) -> FitResult:
    title_l = (title or "").lower()
    if any(t in title_l for t in _FIT_HARD_REJECT):
        return FitResult(0, "LOW", "hard_reject_dev_or_support_title")
    blob = f"{title_l} {(company or '').lower()} {(location or '').lower()}"
    sig = signals if signals is not None else FIT_SCORE_SIGNALS

    score = 0
    for tok in STRONG_TITLE_TOKENS:  # ONE title-family bonus max
        if tok in title_l:
            score += 20
            break

    matched: list[str] = []
    for tok, pts in sig.items():
        if tok in blob:
            score += pts
            matched.append(tok)

    score = min(100, score)
    return FitResult(score, fit_label(score), ", ".join(matched[:5]))
