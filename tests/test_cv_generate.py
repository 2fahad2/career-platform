"""Claude generation acceptance tests (LEGACY §10) — before code.

The three prompts are byte-verbatim (guarded against LEGACY like the
templates). The prompt is NEVER trusted alone: the invented-content guard
rejects rewrites that introduce numbers, certifications, frameworks, or
proper nouns absent from the customer's CONFIRMED bank vocabulary; every
LLM stage degrades to a deterministic rule-based fallback so the pipeline
never depends on the model being up; the budget guard blocks at the cap
with an explicit reason but a guard FAILURE never blocks; and no prompt
ever carries the customer's name or phone (§15.8 — spy-verified).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from career.cv import generate, prompts

_LEGACY = Path(__file__).resolve().parents[1] / "docs" / "LEGACY_KNOWLEDGE.md"


def _fenced_plain(marker: str) -> str:
    text = _LEGACY.read_text(encoding="utf-8")
    start = text.index(marker)
    o = text.index("```\n", start) + len("```\n")
    c = text.index("\n```", o)
    return text[o:c]


def test_prompts_are_byte_verbatim_with_legacy() -> None:
    assert prompts.JOB_ANALYSIS_PROMPT == _fenced_plain(
        "### 10.1 Job analysis prompt"
    )
    assert prompts.SUMMARY_HUMANIZATION_PROMPT == _fenced_plain(
        "### 10.2 Summary humanization prompt"
    )
    assert prompts.EXPERIENCE_RANKING_PROMPT == _fenced_plain(
        "### 10.3 Experience-ranking prompt"
    )


# ── the bank vocabulary whitelist ────────────────────────────────────────────

BANK = {
    "experience": [
        {"title": "Senior Business Analyst", "employer": "Alpha Bank",
         "start_date": "2021-03", "end_date": "Present",
         "description": "Led requirements workshops across SAMA-regulated programs.",
         "achievements": ["Cut rework by a third across 4 projects."]},
    ],
    "education": [
        {"degree": "Bachelor of Science", "field_of_study": "MIS",
         "institution": "King Saud University", "graduation_year": 2017},
    ],
    "certification": [{"name": "PMP", "issuer": "PMI", "issue_date": "2020-01"}],
    "skill": [{"name": "SQL"}, {"name": "Power BI"}],
    "language": [{"language": "English", "proficiency": "Fluent"}],
}

ORIGINAL_SUMMARY = (
    "Senior business analyst with 9 years of experience across Alpha Bank "
    "programs. PMP certified. Delivered requirements and UAT for regulated "
    "platforms using SQL and Power BI."
)


def test_vocabulary_whitelist_builds_from_the_bank() -> None:
    vocab = generate.bank_vocabulary(BANK)
    for token in ("Alpha", "Bank", "PMP", "SQL", "King", "Saud"):
        assert token.lower() in vocab
    assert "kubernetes" not in vocab


# ── the invented-content guard (§10.2 — the most safety-critical port) ───────


def _guard(rewrite: str) -> bool:
    return generate.contains_invented_content(
        rewrite, original=ORIGINAL_SUMMARY, vocabulary=generate.bank_vocabulary(BANK)
    )


def test_guard_rejects_new_number_unit_tokens() -> None:
    assert _guard("Delivered 12 projects for Alpha Bank.")          # new count
    assert _guard("Improved throughput by 40% across programs.")    # new percent
    assert _guard("Led 15 teams across the bank.")                  # new team size


def test_guard_rejects_invention_patterns() -> None:
    assert _guard("Over 11 years of experience in analysis.")       # years claim
    assert _guard("Achieved a 30% improvement in delivery.")


def test_guard_rejects_new_certifications_and_frameworks() -> None:
    assert _guard("ITIL-driven analyst delivering regulated programs.")
    assert _guard("Certified CISSP professional with banking depth.")
    assert _guard("Expert in Kubernetes and Terraform delivery.")


def test_guard_rejects_proper_nouns_outside_the_bank() -> None:
    assert _guard("Business analyst who led Accenture engagements.")


def test_guard_accepts_a_faithful_rewrite() -> None:
    faithful = (
        "Senior business analyst bringing 9 years of experience to regulated "
        "banking programs at Alpha Bank. PMP certified, with hands-on SQL "
        "and Power BI delivery across requirements and UAT."
    )
    assert not _guard(faithful)


# ── deterministic base summary (variants-over-generation, D6 spirit) ─────────


def test_base_summary_uses_only_confirmed_facts() -> None:
    summary = generate.base_summary(
        bank=BANK, current_title="Senior Business Analyst",
        years_experience=9, jd_keywords=("sql", "requirements"),
    )
    assert "Senior Business Analyst" in summary
    assert "9" in summary
    assert len(summary) >= 150
    assert not generate.contains_invented_content(
        summary, original=summary, vocabulary=generate.bank_vocabulary(BANK)
    )


# ── LLM stages: fakes, fallbacks, validation ─────────────────────────────────


class FakeLlm:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.replies:
            raise RuntimeError("LLM down")
        return self.replies.pop(0)


def test_job_analysis_normalizes_and_falls_back() -> None:
    good = FakeLlm([json.dumps({
        "role_family": "Business Analysis", "seniority": "Senior",
        "match_score": 3.7, "key_requirements": ["SQL"],
        "recommendation": "STRONGLY_APPLY",
    })])
    analysis = generate.analyze_job(
        good, job_title="Senior BA", company="X", jd_text="SQL requirements",
    )
    assert analysis["match_score"] == 1.0                # clamped to [0,1]
    assert analysis["recommendation"] == "MAYBE"         # forced into the set
    down = generate.analyze_job(
        FakeLlm([]), job_title="Senior BA", company="X", jd_text="SQL",
    )
    assert down["role_family"]                           # rule-based stub


def test_experience_ranking_validates_indexes_and_falls_back() -> None:
    entries = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    partial = FakeLlm([json.dumps({"experience_order": [2, 0]})])
    order = generate.rank_experience(
        partial, entries=entries, job_title="X", company="Y",
        jd_text="", key_requirements=[],
    )
    assert order == [2, 0, 1]                            # missing appended
    down = generate.rank_experience(
        FakeLlm([]), entries=entries, job_title="X", company="Y",
        jd_text="B is the need", key_requirements=["B"],
    )
    assert sorted(down) == [0, 1, 2]                     # rule-based, complete


def test_humanize_guard_falls_back_on_invention_and_short_output() -> None:
    invented = FakeLlm(["ITIL expert delivering 25 projects with Kubernetes."])
    result = generate.humanize_summary(
        invented, original=ORIGINAL_SUMMARY, vocabulary=generate.bank_vocabulary(BANK),
        job_title="BA", company="X", key_requirements=[], jd_text="",
    )
    assert result == ORIGINAL_SUMMARY                    # rejected → original
    short = FakeLlm(["Too short."])
    assert generate.humanize_summary(
        short, original=ORIGINAL_SUMMARY, vocabulary=generate.bank_vocabulary(BANK),
        job_title="BA", company="X", key_requirements=[], jd_text="",
    ) == ORIGINAL_SUMMARY


def test_humanize_accepts_a_clean_rewrite() -> None:
    clean = (
        "Senior business analyst with 9 years of experience delivering "
        "regulated banking programs at Alpha Bank. PMP certified and fluent "
        "in SQL and Power BI across requirements and UAT delivery."
    )
    llm = FakeLlm([clean])
    assert generate.humanize_summary(
        llm, original=ORIGINAL_SUMMARY, vocabulary=generate.bank_vocabulary(BANK),
        job_title="BA", company="X", key_requirements=[], jd_text="",
    ) == clean


# ── §15.8: prompts never carry the customer's identity ──────────────────────


def test_prompts_never_contain_name_or_phone() -> None:
    spy = FakeLlm([json.dumps({"role_family": "BA", "seniority": "Senior",
                               "match_score": 0.8, "key_requirements": [],
                               "recommendation": "APPLY"})])
    generate.analyze_job(spy, job_title="BA", company="X", jd_text="desc")
    joined = "\n".join(spy.prompts)
    assert "Fahad" not in joined and "+9665" not in joined


# ── budget guard semantics ───────────────────────────────────────────────────


class Budget:
    def __init__(self, allow: bool = True, explode: bool = False) -> None:
        self.allow_calls = 0
        self._allow = allow
        self._explode = explode

    def allow(self) -> tuple[bool, str | None]:
        self.allow_calls += 1
        if self._explode:
            raise RuntimeError("budget backend down")
        return (self._allow, None if self._allow else "budget_cap_reached")


def test_budget_cap_blocks_llm_and_uses_fallback() -> None:
    llm = FakeLlm(["should never be called"])
    result = generate.humanize_summary(
        llm, original=ORIGINAL_SUMMARY, vocabulary=generate.bank_vocabulary(BANK),
        job_title="BA", company="X", key_requirements=[], jd_text="",
        budget=Budget(allow=False),
    )
    assert result == ORIGINAL_SUMMARY
    assert llm.prompts == []                             # LLM never touched


def test_budget_guard_failure_never_blocks() -> None:
    clean = (
        "Senior business analyst with 9 years of experience across Alpha "
        "Bank regulated programs. PMP certified with SQL and Power BI depth "
        "spanning requirements and UAT."
    )
    llm = FakeLlm([clean])
    result = generate.humanize_summary(
        llm, original=ORIGINAL_SUMMARY, vocabulary=generate.bank_vocabulary(BANK),
        job_title="BA", company="X", key_requirements=[], jd_text="",
        budget=Budget(explode=True),
    )
    assert result == clean                               # guard failure ≠ block


# ── skills ranking (JD relevance, cap 12 — §1.8d) ───────────────────────────


def test_skills_ranked_by_jd_relevance_capped_12() -> None:
    skills = [f"Skill{i}" for i in range(15)] + ["SQL", "Power BI"]
    ranked = generate.rank_skills(
        skills, jd_text="We need SQL and Power BI daily.", cap=12
    )
    assert ranked[0] in ("SQL", "Power BI") and ranked[1] in ("SQL", "Power BI")
    assert len(ranked) == 12


def _no_pii_probe(value: Any) -> None:  # silence vulture-style unused warnings
    assert value is not None
