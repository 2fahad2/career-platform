"""Claude generation with deterministic guards — LEGACY §10 ported (D1/D10).

The prompt is never trusted alone (§11 «LLMs invent frameworks/certs/metrics
from the JD»): every rewrite passes the invented-content guard against the
customer's CONFIRMED bank vocabulary; every LLM stage has a rule-based
fallback so the pipeline never depends on the model being up; the budget
guard blocks at the cap with an explicit reason while a guard FAILURE never
blocks (legacy semantics); and prompts carry no customer identity — §15.8
placeholders are injected locally at PDF assembly, far downstream of here.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Protocol

from career.cv.close import TokenCounter
from career.cv.prompts import (
    EXPERIENCE_RANKING_PROMPT,
    JOB_ANALYSIS_PROMPT,
    SUMMARY_HUMANIZATION_PROMPT,
)
from career.cv.schemas import TailoredCV

logger = logging.getLogger("career.cv")


class LlmClient(Protocol):
    def complete(self, prompt: str) -> str: ...


class BudgetGuard(Protocol):
    def allow(self) -> tuple[bool, str | None]: ...


def _budget_allows(budget: BudgetGuard | None) -> bool:
    """Blocked ONLY by an explicit (False, reason) — a guard failure never
    blocks generation (LEGACY §10 client behavior)."""
    if budget is None:
        return True
    try:
        allowed, reason = budget.allow()
    except Exception:  # noqa: BLE001 — guard failure must not block
        logger.warning("budget guard failed open", exc_info=True)
        return True
    if not allowed:
        logger.info("generation blocked by budget: %s", reason)
    return allowed


# ── the bank vocabulary whitelist ────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.&/-]*")

#: Neutral words that are legitimate at sentence starts — deliberately small;
#: anything factual must come from the bank itself.
_NEUTRAL_TOKENS = frozenset(
    t.lower()
    for t in (
        "The", "A", "An", "And", "With", "Across", "For", "In", "On", "At",
        "Senior", "Junior", "Lead", "Business", "Analyst", "Analysis",
        "Experienced", "Delivered", "Delivering", "Led", "Leading", "Built",
        "Building", "Improved", "Improving", "Managed", "Managing", "Brings",
        "Bringing", "Known", "Focused", "Trusted", "Proven", "Hands-on",
        "Expert", "Certified", "Professional", "Core", "Strengths",
    )
)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def bank_vocabulary(bank: dict[str, list[dict[str, Any]]]) -> set[str]:
    """Every factual token in the confirmed bank — employers, titles, tools,
    certifications, institutions, languages (§10.2 whitelist source)."""
    vocab: set[str] = set()
    for payloads in bank.values():
        for payload in payloads:
            for value in payload.values():
                if isinstance(value, str):
                    vocab |= _tokens(value)
                elif isinstance(value, list):
                    vocab |= _tokens(" ".join(str(v) for v in value))
    return vocab


# ── the §10.2 invented-content guard (verbatim rules) ────────────────────────

# trailing (?!\w) instead of \b: after "%" or "$" a \b never fires (both are
# non-word chars), silently letting "40% across" through — the documented
# intent is the unit token ending, which (?!\w) captures for all units.
_NUMBER_UNIT_RE = re.compile(
    r"\b\d+[\d,\.]*\s*(?:years?|%|\$|million|billion|k|projects?|teams?|"
    r"clients?|users?)(?!\w)",
    re.IGNORECASE,
)
_INVENTION_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b\d+\+?\s*years?\s+(?:of\s+)?experience\b",
        r"\bover\s+\d+\b",
        r"\bmore\s+than\s+\d+\b",
        r"\b\d+%\s+(?:increase|improvement|reduction|growth)\b",
    )
)
_CERT_RE = re.compile(
    r"\b(?:pmp|cissp|cisa|cism|aws|azure|gcp|ccna|ccnp|itil|prince2|"
    r"scrum master|csm|psm)\b",
    re.IGNORECASE,
)
_KNOWN_FRAMEWORKS = (
    "itil", "prince2", "cobit", "togaf", "six sigma", "lean", "agile",
    "scrum", "kanban", "safe", "devops", "devsecops", "iso 27001",
    "iso 9001", "nist", "sox", "gdpr", "hipaa", "pmbok", "waterfall",
    "kaizen", "bpmn", "archimate", "terraform", "kubernetes", "docker",
    "ansible", "jenkins", "splunk", "servicenow", "jira", "confluence",
    "tableau", "power bi", "snowflake", "databricks", "airflow", "sap",
    "oracle erp", "salesforce", "dynamics 365",
)
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][A-Za-z0-9+#.&/-]{2,}\b")


def contains_invented_content(
    rewrite: str, *, original: str, vocabulary: set[str]
) -> bool:
    """True when the rewrite asserts anything the original + bank don't."""
    original_l = original.lower()
    rewrite_l = rewrite.lower()

    for match in _NUMBER_UNIT_RE.findall(rewrite):
        if match.lower() not in original_l:
            return True
    for pattern in _INVENTION_PATTERNS:
        for match in pattern.findall(rewrite):
            if match.lower() not in original_l:
                return True
    for cert in _CERT_RE.findall(rewrite):
        if cert.lower() not in original_l:
            return True
    for framework in _KNOWN_FRAMEWORKS:
        if framework in rewrite_l and framework not in original_l:
            return True
    original_tokens = _tokens(original)
    for noun in _PROPER_NOUN_RE.findall(rewrite):
        noun_l = noun.lower()
        if (
            noun_l not in original_tokens
            and noun_l not in vocabulary
            and noun_l not in _NEUTRAL_TOKENS
        ):
            return True
    return False


# ── deterministic base summary (variants-over-generation, D6) ────────────────


def base_summary(
    *,
    bank: dict[str, list[dict[str, Any]]],
    current_title: str | None,
    years_experience: int | None,
    jd_keywords: tuple[str, ...],
) -> str:
    """Rule-based summary built ONLY from confirmed facts — the pre-LLM
    variant (§10.5 spirit) and the fallback when the rewrite is rejected."""
    employers = [
        p.get("display_company") or p.get("employer") or ""
        for p in bank.get("experience", [])
    ]
    employers = [e for e in employers if e]
    certs = [p.get("name") or "" for p in bank.get("certification", [])]
    certs = [c for c in certs if c]
    skills = [p.get("name") or "" for p in bank.get("skill", [])]
    ranked_skills = rank_skills(
        [s for s in skills if s], jd_text=" ".join(jd_keywords), cap=4
    )

    parts: list[str] = []
    title = (current_title or "Professional").strip()
    if years_experience:
        parts.append(
            f"{title} with {years_experience} years of experience"
            + (f" across {', '.join(employers[:2])}" if employers else "")
            + "."
        )
    else:
        parts.append(
            f"{title}" + (f" at {employers[0]}" if employers else "") + "."
        )
    if certs:
        parts.append(f"Certified: {', '.join(certs[:3])}.")
    if ranked_skills:
        parts.append(f"Core strengths: {', '.join(ranked_skills)}.")
    achievements = [
        a
        for p in bank.get("experience", [])
        for a in (p.get("achievements") or [])
    ]
    if achievements:
        parts.append(str(achievements[0]))
    return " ".join(parts)


# ── prompt filling (verbatim text; placeholders replaced, never .format) ─────


def _fill(template: str, mapping: dict[str, str]) -> str:
    filled = template
    for key, value in mapping.items():
        filled = filled.replace("{" + key + "}", value)
    return filled


def _parse_json_object(raw: str) -> dict[str, Any]:
    """LEGACY defensive parse: direct → strip fences → first {...} object."""
    text = raw.strip()
    for candidate in (
        text,
        re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE),
    ):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            continue
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    raise ValueError("no JSON object in LLM reply")


# ── §10.1 job analysis ───────────────────────────────────────────────────────

_RECOMMENDATIONS = frozenset({"APPLY", "MAYBE", "SKIP"})


def _stub_analysis(job_title: str) -> dict[str, Any]:
    title_l = job_title.lower()
    if "analy" in title_l:
        family = "Business Analysis"
    elif "project" in title_l or "pmo" in title_l:
        family = "Project Management"
    elif "it" in title_l or "operations" in title_l:
        family = "IT Operations"
    else:
        family = "General"
    if any(t in title_l for t in ("senior", "lead", "sr.")):
        seniority = "Senior"
    elif "manager" in title_l or "head" in title_l:
        seniority = "Manager"
    elif "junior" in title_l or "intern" in title_l:
        seniority = "Entry-level"
    else:
        seniority = "Unknown"
    return {
        "role_family": family, "seniority": seniority, "match_score": 0.5,
        "key_requirements": [], "recommendation": "MAYBE",
    }


def analyze_job(
    llm: LlmClient,
    *,
    job_title: str,
    company: str,
    jd_text: str,
    budget: BudgetGuard | None = None,
) -> dict[str, Any]:
    if _budget_allows(budget):
        prompt = _fill(JOB_ANALYSIS_PROMPT, {
            "job_title": job_title, "company": company,
            "job_description": jd_text,
        })
        try:
            data = _parse_json_object(llm.complete(prompt))
            if not data.get("role_family") or not data.get("seniority"):
                raise ValueError("missing role_family/seniority")
            score = float(data.get("match_score", 0.0))
            data["match_score"] = min(1.0, max(0.0, score))
            if data.get("recommendation") not in _RECOMMENDATIONS:
                data["recommendation"] = "MAYBE"
            reqs = data.get("key_requirements")
            data["key_requirements"] = (
                [str(r) for r in reqs] if isinstance(reqs, list) else []
            )
            return data
        except Exception:  # noqa: BLE001 — every stage degrades to rules
            logger.warning("job analysis fell back to the rule stub")
    return _stub_analysis(job_title)


# ── §10.3 experience ranking (ranking only, never rewriting) ─────────────────


def _rule_rank(entries: list[dict[str, Any]], jd_text: str) -> list[int]:
    jd_tokens = _tokens(jd_text)

    def score(index: int) -> int:
        return len(_tokens(json.dumps(entries[index])) & jd_tokens)

    return sorted(range(len(entries)), key=lambda i: (-score(i), i))


def rank_experience(
    llm: LlmClient,
    *,
    entries: list[dict[str, Any]],
    job_title: str,
    company: str,
    jd_text: str,
    key_requirements: list[str],
    budget: BudgetGuard | None = None,
) -> list[int]:
    if _budget_allows(budget):
        prompt = _fill(EXPERIENCE_RANKING_PROMPT, {
            "job_title": job_title, "company": company,
            "key_requirements": ", ".join(key_requirements),
            "job_description": jd_text,
            "json.dumps(payload)": json.dumps(entries, ensure_ascii=False),
        })
        try:
            data = _parse_json_object(llm.complete(prompt))
            raw_order = data.get("experience_order")
            if not isinstance(raw_order, list) or not raw_order:
                raise ValueError("empty order")
            order: list[int] = []
            for item in raw_order:
                if not isinstance(item, int):
                    raise ValueError("non-int index")
                if 0 <= item < len(entries) and item not in order:
                    order.append(item)
            for index in range(len(entries)):    # nothing is ever dropped
                if index not in order:
                    order.append(index)
            return order
        except Exception:  # noqa: BLE001
            logger.warning("experience ranking fell back to rules")
    return _rule_rank(entries, jd_text)


# ── §10.2 summary humanization (the most safety-critical stage) ──────────────

_MIN_REWRITE_CHARS = 100
_MAX_REWRITE_CHARS = 1000
_TRIM_TO_CHARS = 420


def humanize_summary(
    llm: LlmClient,
    *,
    original: str,
    vocabulary: set[str],
    job_title: str,
    company: str,
    key_requirements: list[str],
    jd_text: str,
    budget: BudgetGuard | None = None,
) -> str:
    """The rewrite is used ONLY when every §10.2 validation passes; anything
    else returns the original (deterministic, already truthful)."""
    if not _budget_allows(budget):
        return original
    prompt = _fill(SUMMARY_HUMANIZATION_PROMPT, {
        "job_title": job_title, "company": company,
        "key_requirements": ", ".join(key_requirements),
        "job_description": jd_text, "summary": original,
    })
    try:
        rewrite = llm.complete(prompt).strip()
    except Exception:  # noqa: BLE001
        logger.warning("humanization LLM unavailable — keeping the original")
        return original
    if not (_MIN_REWRITE_CHARS <= len(rewrite) <= _MAX_REWRITE_CHARS):
        return original
    rewrite = rewrite[:_TRIM_TO_CHARS].strip()
    if len(rewrite) < _MIN_REWRITE_CHARS:
        return original
    if contains_invented_content(rewrite, original=original, vocabulary=vocabulary):
        logger.info("humanized summary rejected: invented content")
        return original
    return rewrite


# ── skills ranking (§1.8d — JD relevance, visual-fit cap) ───────────────────


def rank_skills(skills: list[str], *, jd_text: str, cap: int = 12) -> list[str]:
    jd_l = jd_text.lower()

    def score(skill: str) -> int:
        return 1 if skill.lower() in jd_l else 0

    ranked = sorted(
        dict.fromkeys(skills), key=lambda s: -score(s)
    )
    return ranked[:cap]


# ── the full tailoring composition (§1.8 call chain, D6 sources) ─────────────

#: §15.8: generation never sees the customer's identity — the real contact is
#: injected locally at publish time (C7.4), far downstream of every LLM call.
PLACEHOLDER_CONTACT: dict[str, str] = {
    "name": "CANDIDATE",
    "email": "candidate@example.com",
    "phone": "+000000000000",
    "location": "Riyadh, Saudi Arabia",
}


class TailoringBlocked(Exception):
    """The §1.6 summary gate refused the CV before any file was written."""


class GenerationFailed(Exception):
    """The LLM transport failed in a way the caller must see honestly."""


def _forbidden_claim_hit(cv: TailoredCV, claims: Sequence[str]) -> bool:
    """§15.5: the customer's forbidden list is honored literally — any
    forbidden phrase appearing in generated text blocks the CV."""
    normalized = [c.strip().casefold() for c in claims if c and c.strip()]
    if not normalized:
        return False
    parts: list[str] = [cv.tailored_summary, cv.master_cv.headline or ""]
    parts.extend(cv.selected_skills)
    for exp in cv.selected_experience:
        parts.extend(exp.achievements)
        parts.extend(exp.technologies)
    haystack = "\n".join(parts).casefold()
    return any(claim in haystack for claim in normalized)


def tailor_cv(
    llm: LlmClient,
    *,
    bank: dict[str, list[dict[str, Any]]],
    current_title: str | None,
    years_experience: int | None,
    job_title: str,
    company: str,
    jd_text: str,
    budget: BudgetGuard | None = None,
    forbidden_claims: Sequence[str] = (),
) -> TailoredCV:
    """The §1.8 chain: analyze → rank → pace → skills → variant → humanize →
    enforce → the §1.6 gate → forbidden-claims + pre-render guards. Every LLM
    stage degrades to rules; the PDF look is identical either way because
    template + caps are deterministic."""
    from career.cv import enforce, normalize
    from career.cv.validate import validate_pre_render
    from career_core.sentences import validate_summary_quality

    vocabulary = bank_vocabulary(bank)
    analysis = analyze_job(
        llm, job_title=job_title, company=company, jd_text=jd_text,
        budget=budget,
    )
    key_requirements = list(analysis.get("key_requirements") or [])
    jd_keywords = tuple(
        {*(k.lower() for k in key_requirements), *_tokens(jd_text)}
    )

    base = base_summary(
        bank=bank, current_title=current_title,
        years_experience=years_experience, jd_keywords=jd_keywords,
    )
    master = normalize.build_master_cv(
        contact=PLACEHOLDER_CONTACT, bank=bank,
        headline=current_title, summary=base,
    )

    entries_payload = [
        {"title": e.title, "company": e.company,
         "achievements": e.achievements, "technologies": e.technologies}
        for e in master.experience
    ]
    order = rank_experience(
        llm, entries=entries_payload, job_title=job_title, company=company,
        jd_text=jd_text, key_requirements=key_requirements, budget=budget,
    )
    selected = [master.experience[i] for i in order]

    summary = humanize_summary(
        llm, original=base, vocabulary=vocabulary, job_title=job_title,
        company=company, key_requirements=key_requirements, jd_text=jd_text,
        budget=budget,
    )

    cv = TailoredCV(
        master_cv=master,
        job_title=job_title,
        company=company,
        tailored_summary=summary,
        selected_experience=selected,
        selected_skills=rank_skills(master.skills, jd_text=jd_text, cap=12),
        modifications=f"tailored for {job_title} @ {company}",
    )
    cv = enforce.enforce_one_page(cv, jd_keywords=jd_keywords)

    ok, reason = validate_summary_quality(cv.tailored_summary)
    if not ok:
        raise TailoringBlocked(str(reason))   # blocks BEFORE any file (§1.6)

    # §15.5: the forbidden list is honored literally — reason code only,
    # never the claim text (it may quote customer content).
    if _forbidden_claim_hit(cv, forbidden_claims):
        raise TailoringBlocked("forbidden_claim")

    # §1.10 pre-render guard (audit fix: existed but was never wired) —
    # Arabic-leak/structure issues block; warnings are advisory only.
    valid, issues, _warnings = validate_pre_render(cv)
    if not valid:
        raise TailoringBlocked(",".join(issues))
    return cv


# ── the real Claude client (D1 — injectable, mirrors C5.6's proven shape) ────

_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 2048


class AnthropicLlmClient(TokenCounter):
    """Claude-only transport (D1). SDK client injectable — tests exercise the
    exact request shape with zero network; retries are the SDK's built-in.
    Token totals accumulate on the instance so the caller can meter the §14
    cost fuel per tailoring (audit fix: usage was recorded without numbers)."""

    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        model: str = _MODEL,
    ) -> None:
        if client is None:  # pragma: no cover — exercised live by the canary
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self._model = model
        self.reset_token_counters()

    def complete(self, prompt: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
        self.absorb_usage(response)
        if response.stop_reason != "end_turn":
            raise GenerationFailed(f"stop_reason:{response.stop_reason}")
        for block in response.content:
            candidate = getattr(block, "text", None)
            if getattr(block, "type", "") == "text" and isinstance(candidate, str):
                return candidate
        raise GenerationFailed("no_text_block")
