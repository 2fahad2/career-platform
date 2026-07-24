"""Pre-render validation — LEGACY §1.9 ValidationService semantics.

Issues BLOCK (the CV never renders); warnings inform (never block). The
Arabic-leak guard is the critical issue: the platform legitimately holds
Arabic everywhere (it speaks Arabic to the customer), and this gate is what
keeps it out of the English CV. Reason codes only — never CV text — so logs
stay PII-free (§15.13).
"""

from __future__ import annotations

import re

from career.cv.schemas import TailoredCV

MIN_SUMMARY_LENGTH = 150
MIN_EXPERIENCE_ENTRIES = 1
MIN_ACHIEVEMENTS_PER_JOB = 2
MIN_SKILLS = 5
MAX_ESTIMATED_PAGES = 1.15

_WEAK_PHRASES = (
    "experienced professional", "diverse background", "proven track record",
    "results-driven", "team player", "hard worker", "go-getter",
    "self-starter", "dynamic individual",
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: The §1.9 Unicode blocks — CV must be English-only.
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")


def _arabic_leak_fields(cv: TailoredCV) -> list[str]:
    """AUDIT ك-13: EVERY field the template renders is scanned — the bank
    stores facts in their source language by design, so headline, education,
    certifications and languages could all leak Arabic onto the English PDF
    (only summary/experience/skills were checked before)."""
    leaks: list[str] = []
    if _ARABIC_RE.search(cv.tailored_summary):
        leaks.append("summary")
    if _ARABIC_RE.search(cv.job_title or "") or _ARABIC_RE.search(
        cv.master_cv.headline or ""
    ):
        leaks.append("headline")
    for exp in cv.selected_experience:
        blob = " ".join([exp.title, exp.company, *exp.achievements,
                         *exp.responsibilities])
        if _ARABIC_RE.search(blob):
            leaks.append("experience")
            break
    if any(_ARABIC_RE.search(s) for s in cv.selected_skills):
        leaks.append("skills")
    for edu in cv.master_cv.education:
        if _ARABIC_RE.search(
            " ".join([edu.degree, edu.field_of_study or "", edu.institution])
        ):
            leaks.append("education")
            break
    for cert in cv.master_cv.certifications:
        if _ARABIC_RE.search(f"{cert.name} {cert.issuer}"):
            leaks.append("certifications")
            break
    if any(_ARABIC_RE.search(lang) for lang in cv.master_cv.languages):
        leaks.append("languages")
    return leaks


def _estimated_pages(cv: TailoredCV) -> float:
    chars = len(cv.tailored_summary)
    for exp in cv.selected_experience:
        chars += len(exp.title) + len(exp.company)
        chars += sum(len(a) for a in exp.achievements)
    chars += sum(len(s) for s in cv.selected_skills)
    return chars / 3000


def validate_pre_render(cv: TailoredCV) -> tuple[bool, list[str], list[str]]:
    """(valid, issues, warnings) — reason codes only, never content."""
    issues: list[str] = []
    warnings: list[str] = []

    summary = cv.tailored_summary.strip()
    if len(summary) < MIN_SUMMARY_LENGTH:
        issues.append("summary_too_short")
    if len(summary.split()) < 10:
        issues.append("summary_under_10_words")
    if "lorem ipsum" in summary.lower():
        issues.append("summary_placeholder_text")

    if len(cv.selected_experience) < MIN_EXPERIENCE_ENTRIES:
        issues.append("experience_missing")
    if len(cv.selected_skills) < MIN_SKILLS:
        issues.append("skills_too_few")

    contact = cv.master_cv.contact
    if not _EMAIL_RE.match(contact.email or ""):
        issues.append("email_invalid")
    if not (contact.phone or "").strip():
        issues.append("phone_missing")

    for field in _arabic_leak_fields(cv):
        issues.append(f"arabic_characters_detected:{field}")

    weak_hits = sum(1 for p in _WEAK_PHRASES if p in summary.lower())
    if weak_hits >= 2:
        warnings.append(f"weak_phrases:{weak_hits}")
    for index, exp in enumerate(cv.selected_experience):
        if len(exp.achievements) < MIN_ACHIEVEMENTS_PER_JOB:
            warnings.append(f"low_achievements:role_{index}")
    if _estimated_pages(cv) > MAX_ESTIMATED_PAGES:
        warnings.append("estimated_pages_high")
    linkedin = contact.linkedin or ""
    if linkedin and "linkedin.com/in/" not in linkedin:
        warnings.append("linkedin_url_shape")

    return (not issues, issues, warnings)
