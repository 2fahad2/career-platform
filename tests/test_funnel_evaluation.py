"""CV-analysis evaluation (whitepaper §04) — before code.

Deterministic and Arabic: the five numeric scores (overall / path clarity /
achievement strength / requested-path fit / machine readability), the top-5
rule-based notes, the requested-vs-alternative comparison with BOTH scores,
and the three next steps. Same facts → same report, no LLM, no network —
the notes must be genuinely useful, never filler.
"""

from __future__ import annotations

import uuid
from typing import Any

from career.db.models import ProfileFact
from career.funnel import evaluation


def _fact(category: str, payload: dict[str, Any]) -> ProfileFact:
    return ProfileFact(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), category=category,
        payload=payload, status="EXTRACTED", source="cv_extraction",
    )


def _strong_facts() -> list[ProfileFact]:
    return [
        _fact("experience", {
            "title": "Senior Business Analyst", "employer": "Alpha Bank",
            "start_date": "2021-03", "end_date": "Present",
            "description": "Led requirements and UAT across squads.",
            "achievements": ["Cut rework by 30% across 4 releases.",
                             "Raised UAT first-pass rate to 92%."],
        }),
        _fact("experience", {
            "title": "Business Analyst", "employer": "Beta Co",
            "start_date": "2018-01", "end_date": "2021-02",
            "description": "Requirements and process mapping.",
            "achievements": ["Mapped 40 processes in BPMN."],
        }),
        _fact("education", {"degree": "Bachelor of Science",
                            "field_of_study": "MIS",
                            "institution": "KSU", "graduation_year": 2017}),
        _fact("certification", {"name": "PMI-PBA", "issuer": "PMI",
                                "issue_date": "2022-05"}),
        *[_fact("skill", {"name": n}) for n in (
            "SQL", "Power BI", "BPMN", "Requirements", "UAT",
            "Stakeholder Management", "User Stories", "Process Mapping",
        )],
        _fact("language", {"language": "English", "proficiency": "Fluent"}),
    ]


def _weak_facts() -> list[ProfileFact]:
    return [
        _fact("experience", {
            "title": "Sales Assistant", "employer": "Shop",
            "start_date": None, "end_date": None,
            "description": None, "achievements": [],
        }),
        _fact("skill", {"name": "Excel"}),
    ]


# ── the five scores ──────────────────────────────────────────────────────────


def test_scores_are_bounded_and_deterministic() -> None:
    report_a = evaluation.evaluate(
        _strong_facts(), requested_path="محلل أعمال", contact_found=True
    )
    report_b = evaluation.evaluate(
        _strong_facts(), requested_path="محلل أعمال", contact_found=True
    )
    assert report_a == report_b                          # same facts, same report
    for score in (report_a.overall, report_a.clarity, report_a.achievements,
                  report_a.fit, report_a.readability):
        assert 0 <= score <= 100


def test_strong_profile_scores_high_weak_scores_low() -> None:
    strong = evaluation.evaluate(
        _strong_facts(), requested_path="محلل أعمال", contact_found=True
    )
    weak = evaluation.evaluate(
        _weak_facts(), requested_path="محلل أعمال", contact_found=False
    )
    assert strong.fit >= 60 and weak.fit < 40
    assert strong.achievements > weak.achievements
    assert strong.readability > weak.readability
    assert strong.overall > weak.overall


def test_clarity_rewards_focused_history() -> None:
    focused = evaluation.evaluate(
        _strong_facts(), requested_path="محلل أعمال", contact_found=True
    )
    scattered = _strong_facts() + [
        _fact("experience", {"title": "Truck Driver", "employer": "X",
                             "start_date": "2015-01", "end_date": "2016-01",
                             "achievements": []}),
        _fact("experience", {"title": "Chef", "employer": "Y",
                             "start_date": "2014-01", "end_date": "2015-01",
                             "achievements": []}),
    ]
    assert focused.clarity > evaluation.evaluate(
        scattered, requested_path="محلل أعمال", contact_found=True
    ).clarity


# ── the top-5 notes: Arabic, prioritized, genuinely useful ──────────────────


def test_notes_are_at_most_five_arabic_and_relevant() -> None:
    weak = evaluation.evaluate(
        _weak_facts(), requested_path="محلل أعمال", contact_found=False
    )
    assert 1 <= len(weak.notes) <= 5
    joined = " ".join(weak.notes)
    assert any("؀" <= ch <= "ۿ" for ch in joined)   # Arabic
    assert "أرقام" in joined or "إنجاز" in joined             # measurable-results note
    assert "تواريخ" in joined                                 # missing dates note


def test_strong_profile_gets_fewer_or_softer_notes() -> None:
    strong = evaluation.evaluate(
        _strong_facts(), requested_path="محلل أعمال", contact_found=True
    )
    weak = evaluation.evaluate(
        _weak_facts(), requested_path="محلل أعمال", contact_found=False
    )
    assert len(strong.notes) <= len(weak.notes)


# ── requested vs alternative: both scores, honest verdict ───────────────────


def test_comparison_carries_both_scores() -> None:
    report = evaluation.evaluate(
        _strong_facts(), requested_path="محلل أعمال", contact_found=True
    )
    assert report.requested.label_ar                     # e.g. محلل أعمال
    assert 0 <= report.requested.score <= 100
    assert report.alternative is not None
    assert report.alternative.key != report.requested.key
    assert 0 <= report.alternative.score <= 100
    # a BA profile asking for BA: the requested must beat the alternative
    assert report.requested.score >= report.alternative.score


def test_mismatched_request_flags_the_alternative() -> None:
    report = evaluation.evaluate(
        _strong_facts(), requested_path="مدير مشاريع", contact_found=True
    )
    assert report.alternative is not None
    assert report.alternative.score > report.requested.score
    steps = " ".join(report.next_steps)
    assert "بديل" in steps or report.alternative.label_ar in steps


# ── the three next steps ─────────────────────────────────────────────────────


def test_next_steps_are_exactly_three_and_end_with_the_upgrade() -> None:
    report = evaluation.evaluate(
        _strong_facts(), requested_path="محلل أعمال", contact_found=True
    )
    assert len(report.next_steps) == 3
    assert "البحث اليومي" in report.next_steps[-1]        # the upgrade CTA


def test_report_serializes_to_plain_json_dict() -> None:
    report = evaluation.evaluate(
        _strong_facts(), requested_path="محلل أعمال", contact_found=True
    )
    payload = report.as_dict()
    assert payload["overall"] == report.overall
    assert isinstance(payload["notes"], list)
    assert payload["requested"]["score"] == report.requested.score
