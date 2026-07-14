"""Salary parsing and gate scoring (LEGACY §2.1, §2.3).

Faithful port with ONE approved deviation (D4): the monthly threshold is
injected per tenant (`min_monthly_sar`) instead of a hardcoded 24,000. All
fail-closed behaviors are preserved verbatim:

- period markers are PHRASES only, never the bare word "year" ("5+ years of
  experience" is never a salary);
- a large number is NEVER assumed monthly by magnitude — no reliable local
  period marker → that amount is untrusted;
- annual → monthly is /12 exactly once, Decimal + ROUND_HALF_UP;
- materially contradictory trusted representations → the whole parse fails
  closed (returns None), no silent winner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# ── Gate outcome vocabulary (LEGACY §2.1) ────────────────────────────────────
# (noqa S105: these are gate-outcome enums, not passwords)
SALARY_GATE_PASS_CONFIRMED = "PASS_CONFIRMED"  # noqa: S105
SALARY_GATE_PASS_LIKELY_HIGH = "PASS_LIKELY_HIGH"  # noqa: S105
SALARY_GATE_PASS_POSSIBLE_HIGH = "PASS_POSSIBLE_HIGH"  # noqa: S105
SALARY_GATE_FAIL_LIKELY_LOW = "FAIL_LIKELY_LOW"
SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT = "FAIL_JUNIOR_OR_SUPPORT"
SALARY_GATE_UNKNOWN = "UNKNOWN_INSUFFICIENT_EVIDENCE"

SALARY_INFERENCE_DISCLAIMER = (
    "Salary is inferred unless explicitly stated; inferred salary is not guaranteed."
)

# Junior/support disqualifier tokens — title contains any → immediate fail.
JUNIOR_SUPPORT_TITLE_TOKENS: tuple[str, ...] = (
    "helpdesk", "help desk", "desktop support", "l1 support", "l2 support",
    "level 1 support", "level 2 support", "service desk agent", "junior", "intern",
    "trainee", "cabling", "field technician", "hardware technician",
)

# ── Explicit-amount parsing ──────────────────────────────────────────────────
_SALARY_RANGE_PATTERNS = [
    r'(?:sar|sr)\s*([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:–|-|—|to)\s*([\d,]+(?:\.\d+)?)\s*(k\b)?',
    r'([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:–|-|—|to)\s*([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:sar|sr)\b',
]
_SALARY_SINGLE_PATTERNS = [
    r'(?:sar|sr)\s*([\d,]+(?:\.\d+)?)\s*(k\b)?',
    r'([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:sar|sr)\b',
]

# Period markers are PHRASES only — never the bare word "year".
_ANNUAL_MARKER_RX = re.compile(
    r'per\s*annum|\bannum\b|\bannual(?:ly)?\b|\byearly\b|per\s*year'
    r'|/\s*year|/\s*yr\b|\ba\s*year\b|\bp\.?\s*a\.?\b')
_MONTHLY_MARKER_RX = re.compile(
    r'per\s*month|/\s*month|/\s*mo\b|\bmonthly\b|\ba\s*month\b|\bpcm\b|\bp\.?\s*m\.?\b')

_SALARY_CONSISTENCY_TOL_SAR = 1.0


def _detect_salary_period(context: str) -> str | None:
    if _MONTHLY_MARKER_RX.search(context):
        return "monthly"
    if _ANNUAL_MARKER_RX.search(context):
        return "annual"
    return None  # ambiguous → caller fails closed


def _to_monthly_sar(value: float, period: str) -> float:
    if period == "annual":
        return float((Decimal(str(value)) / Decimal(12)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP))
    return float(value)


def _amount_with_k(num: str, k_group: str | None) -> float:
    val = float(num.replace(",", ""))
    if k_group:
        val *= 1000.0
    return val


def _period_for_match(text_l: str, m: re.Match[str]) -> str | None:
    """Prefer the trailing marker (15-char forward window), fall back to a
    20-char backward lookback."""
    forward = _detect_salary_period(text_l[m.end(): m.end() + 15])
    if forward is not None:
        return forward
    return _detect_salary_period(text_l[max(0, m.start() - 20): m.start()])


def parse_explicit_salary(text: object) -> tuple[float, float] | None:
    """Parse explicit SAR amounts into a trusted monthly (lo, hi), or None.

    Ranges are tried before singles; spans captured by a range are never
    re-counted as lone values. Amounts without a reliable local period marker
    are untrusted. Contradictory trusted representations → None (fail closed).
    """
    if not text or not isinstance(text, str):
        return None
    tl = text.lower()
    trusted: list[tuple[float, float]] = []
    range_spans: list[tuple[int, int]] = []

    for pat in _SALARY_RANGE_PATTERNS:
        for m in re.finditer(pat, tl):
            period = _period_for_match(tl, m)
            if period is None:
                continue
            lo = _to_monthly_sar(_amount_with_k(m.group(1), m.group(2)), period)
            hi = _to_monthly_sar(_amount_with_k(m.group(3), m.group(4)), period)
            if lo > hi:
                lo, hi = hi, lo
            trusted.append((lo, hi))
            range_spans.append(m.span())

    for pat in _SALARY_SINGLE_PATTERNS:
        for m in re.finditer(pat, tl):
            if any(s <= m.start() < e or s < m.end() <= e for s, e in range_spans):
                continue  # already captured by a range match
            period = _period_for_match(tl, m)
            if period is None:
                continue
            v = _to_monthly_sar(_amount_with_k(m.group(1), m.group(2)), period)
            trusted.append((v, v))

    if not trusted:
        return None
    base_lo, base_hi = trusted[0]
    for lo_m, hi_m in trusted[1:]:
        if (abs(lo_m - base_lo) > _SALARY_CONSISTENCY_TOL_SAR
                or abs(hi_m - base_hi) > _SALARY_CONSISTENCY_TOL_SAR):
            return None
    return (base_lo, base_hi)


# ── Evidence scoring (LEGACY §2.1 gate scoring) ──────────────────────────────

_TIER_EVIDENCE_PTS = {1: 30, 2: 20, 3: 10, 4: 0}

_EXEC_TOKENS = ("head of", "director", "vp", "chief")
_MANAGER_TOKENS = ("manager", "lead", "principal", "head")
_SENIOR_TOKENS = ("senior", "consultant", "specialist", "partner")
_ANALYST_TOKENS = ("analyst", "advisor")

_REGULATED_SIGNALS = ("sama", "central bank", "neom", "aramco", "sabic", "pif",
                      "bank", "financial", "regulatory", "compliance", "vision 2030")
_JD_SENIORITY_SIGNALS = ("team of", "team size", "budget", "board", "executive")
_YEARS_RX = re.compile(r"(\d{1,2})\s*\+?\s*years?")

# Estimate-band ratios derived from the legacy 24k-calibrated bands
# (24–35k, 18–28k, 10–20k) so per-tenant thresholds scale proportionally (D4).
_EST_LIKELY_HIGH = (1.0, 35 / 24)
_EST_POSSIBLE = (18 / 24, 28 / 24)
_EST_LOW = (10 / 24, 20 / 24)


@dataclass(frozen=True)
class SalaryGateResult:
    outcome: str
    confidence: int
    estimated_range: tuple[int, int] | None
    reasons: tuple[str, ...]


def _seniority_evidence(title_l: str) -> int:
    if any(t in title_l for t in _EXEC_TOKENS):
        return 20
    if any(t in title_l for t in _MANAGER_TOKENS):
        return 15
    if any(t in title_l for t in _SENIOR_TOKENS):
        return 10
    if any(t in title_l for t in _ANALYST_TOKENS):
        return 5
    return 0


def _band(min_monthly_sar: float, ratios: tuple[float, float]) -> tuple[int, int]:
    return (round(min_monthly_sar * ratios[0]), round(min_monthly_sar * ratios[1]))


def score_salary_gate(
    title: str,
    *,
    min_monthly_sar: float,
    company: str = "",
    jd_text: str | None = None,
    company_tier: int = 3,
    source_quality_score: int | None = None,
) -> SalaryGateResult:
    """Order of decisions (LEGACY §2.1): junior disqualifier → explicit amount
    vs the tenant threshold → accumulated evidence score."""
    title_l = (title or "").lower()
    reasons: list[str] = []

    # 1. Junior/support token in title → immediate fail (conf 95).
    for tok in JUNIOR_SUPPORT_TITLE_TOKENS:
        if tok in title_l:
            return SalaryGateResult(
                SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT, 95, None, (f"junior_token:{tok}",))

    # 2. Explicit SAR amount parsed from JD (conf 95).
    parsed = parse_explicit_salary(jd_text) if jd_text else None
    if parsed is not None:
        lo, hi = parsed
        est = (round(lo), round(hi))
        if lo >= min_monthly_sar:
            return SalaryGateResult(
                SALARY_GATE_PASS_CONFIRMED, 95, est, (f"explicit_salary:{lo:.0f}",))
        return SalaryGateResult(
            SALARY_GATE_FAIL_LIKELY_LOW, 95, est, (f"explicit_salary_below_min:{lo:.0f}",))

    # 3. Evidence score.
    evidence = _TIER_EVIDENCE_PTS.get(company_tier, 0)
    if evidence:
        reasons.append(f"tier{company_tier}:+{evidence}")

    sen = _seniority_evidence(title_l)
    if sen:
        evidence += sen
        reasons.append(f"seniority:+{sen}")

    blob = f"{(company or '').lower()} {(jd_text or '').lower()}"
    if any(sig in blob for sig in _REGULATED_SIGNALS):
        evidence += 10
        reasons.append("regulated:+10")

    if jd_text:
        jd_l = jd_text.lower()
        if any(sig in jd_l for sig in _JD_SENIORITY_SIGNALS):
            evidence += 8
            reasons.append("jd_seniority_signal:+8")
        years = max((int(m.group(1)) for m in _YEARS_RX.finditer(jd_l)), default=0)
        if years >= 7:
            evidence += 10
            reasons.append("years>=7:+10")
        elif years >= 5:
            evidence += 5
            reasons.append("years>=5:+5")

    if source_quality_score is not None and source_quality_score <= 25:
        evidence -= 5
        reasons.append("aggregator_source:-5")

    if evidence >= 35:
        return SalaryGateResult(
            SALARY_GATE_PASS_LIKELY_HIGH, min(85, evidence),
            _band(min_monthly_sar, _EST_LIKELY_HIGH), tuple(reasons))
    if evidence >= 20:
        return SalaryGateResult(
            SALARY_GATE_PASS_POSSIBLE_HIGH, min(60, evidence),
            _band(min_monthly_sar, _EST_POSSIBLE), tuple(reasons))
    if evidence >= 10:
        return SalaryGateResult(
            SALARY_GATE_FAIL_LIKELY_LOW, min(60, evidence),
            _band(min_monthly_sar, _EST_LOW), tuple(reasons))
    return SalaryGateResult(SALARY_GATE_UNKNOWN, 0, None, tuple(reasons))
