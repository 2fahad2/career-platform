"""Bank → render-schema normalization — LEGACY §1.9 rules on OUR sources.

D6: there is no master_cv.json. The inputs are the customer's CONFIRMED
achievement bank (category → payloads, exactly as C5 stores them) and the
contact block (assembled locally at PDF time — §15.8: LLM calls never see
it). Every §1.9 rule that decides what literally appears on the PDF is kept:
display_company order, the Security+ display exception, location defaults,
current-role detection forcing end_date=None, first-wins skill dedupe in
insertion order, "Language (Proficiency)" formatting. The strict pydantic
schema downstream makes the v2 raw-JSON-in-the-PDF bug impossible.
"""

from __future__ import annotations

from typing import Any

from career.cv.schemas import (
    Certification,
    ContactInfo,
    Education,
    Experience,
    MasterCV,
)
from career_core.sentences import split_complete_sentences

_DEFAULT_LOCATION = "Riyadh, Saudi Arabia"
_PRESENT_MARKERS = {"present", "current", "now"}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _experience(payload: dict[str, Any]) -> Experience:
    # §1.9: display_company or employer or company — in that order.
    company = (
        _text(payload.get("display_company"))
        or _text(payload.get("employer"))
        or _text(payload.get("company"))
    )
    end_raw = _text(payload.get("end_date"))
    current = bool(payload.get("is_current") or payload.get("current")) or (
        end_raw.lower() in _PRESENT_MARKERS
    )
    description = _text(payload.get("description"))
    return Experience(
        title=_text(payload.get("title")),
        company=company,
        location=_text(payload.get("location")) or _DEFAULT_LOCATION,
        start_date=_text(payload.get("start_date")),
        end_date=None if current else (end_raw or None),
        current=current,
        achievements=[_text(a) for a in payload.get("achievements") or [] if _text(a)],
        # the bank stores prose; complete sentences become the truthful
        # responsibility bullets (a verbatim subset, §1.6 discipline)
        responsibilities=split_complete_sentences(description) if description else [],
        technologies=[
            _text(t)
            for t in (payload.get("skills_used") or payload.get("technologies") or [])
            if _text(t)
        ],
    )


def _certification(payload: dict[str, Any]) -> Certification:
    name = _text(payload.get("short_name")) or _text(payload.get("name"))
    # §1.9 display exception: bare "Security+" reads wrong without the issuer.
    if name.lower() == "security+":
        name = _text(payload.get("full_name")) or "CompTIA Security+"
    return Certification(
        name=name,
        issuer=_text(payload.get("issuer")),
        date=_text(payload.get("issue_date")) or _text(payload.get("date")) or "N/A",
    )


def _education(payload: dict[str, Any]) -> Education:
    year = payload.get("graduation_year")
    graduation = (
        _text(payload.get("graduation_date")) or (_text(year) if year else "")
    )
    return Education(
        degree=_text(payload.get("degree")),
        field_of_study=_text(payload.get("field_of_study")) or None,
        institution=_text(payload.get("institution")),
        location=_text(payload.get("location")) or _DEFAULT_LOCATION,
        graduation_date=graduation,
    )


def _language(payload: dict[str, Any]) -> str:
    language = _text(payload.get("language"))
    proficiency = _text(payload.get("proficiency"))
    return f"{language} ({proficiency})" if proficiency else language


def build_master_cv(
    *,
    contact: dict[str, Any],
    bank: dict[str, list[dict[str, Any]]],
    headline: str | None,
    summary: str,
) -> MasterCV:
    """Assemble the render-side MasterCV from confirmed bank payloads."""
    seen: set[str] = set()
    skills: list[str] = []
    for item in bank.get("skill", []):
        name = _text(item.get("name"))
        key = name.lower()
        if name and key not in seen:      # first-wins, insertion order (§1.9)
            seen.add(key)
            skills.append(name)

    return MasterCV(
        contact=ContactInfo(
            name=_text(contact.get("name")),
            email=_text(contact.get("email")),
            phone=_text(contact.get("phone")),
            location=_text(contact.get("location")) or _DEFAULT_LOCATION,
            linkedin=_text(contact.get("linkedin")) or None,
            github=_text(contact.get("github")) or None,
        ),
        headline=_text(headline) or None,
        summary=_text(summary),
        experience=[_experience(p) for p in bank.get("experience", [])],
        education=[_education(p) for p in bank.get("education", [])],
        skills=skills,
        certifications=[_certification(p) for p in bank.get("certification", [])],
        languages=[
            lang for p in bank.get("language", []) if (lang := _language(p))
        ],
    )
