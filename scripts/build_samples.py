"""Build the public «عينة» artefacts — a sample CV and a sample analysis report.

The store copy promises that anyone who sends the word «عينة» on WhatsApp gets
a real example back before paying. These are those files.

Everything here is a FICTIONAL persona (invented name, invented employers) —
no real customer data ever becomes marketing material (§15.13 spirit). The
CV goes through the SAME render path production uses, so the sample is an
honest representation of what a buyer receives, not a designer's mockup.

Run:  .venv/bin/python scripts/build_samples.py
Out:  data/samples/sample-cv.pdf  ·  data/samples/sample-analysis.pdf
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from career.cv.render import render_cv_pdf
from career.cv.schemas import (
    Certification,
    ContactInfo,
    Education,
    Experience,
    MasterCV,
    TailoredCV,
)

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "samples"

# ── the fictional persona ────────────────────────────────────────────────────
# Deliberately mid-career and realistic: strong enough to impress, ordinary
# enough that a reader sees themselves in it.
_CONTACT = ContactInfo(
    name="Faisal A. Al-Otaibi",
    email="faisal.sample@example.com",
    phone="+966 5X XXX XXXX",
    location="Riyadh, Saudi Arabia",
    linkedin="linkedin.com/in/sample-profile",
)

_EXPERIENCE = [
    Experience(
        title="Senior Business Analyst",
        company="Najd Digital Solutions",
        location="Riyadh, Saudi Arabia",
        start_date="2022-03",
        end_date=None,
        current=True,
        achievements=[
            "Led requirements gathering across 6 business units for a core "
            "banking migration, delivering signed-off specifications ahead of "
            "each release gate.",
            "Introduced a structured intake process that cut rework on change "
            "requests and shortened the analysis cycle for new features.",
            "Facilitated workshops between operations and engineering, "
            "translating regulatory obligations into testable acceptance "
            "criteria.",
            "Mentored two junior analysts through their first end-to-end "
            "delivery, both now owning their own workstreams.",
        ],
        responsibilities=[
            "Owned the analysis workstream for the enterprise transformation "
            "programme, reporting directly to the delivery director.",
        ],
        technologies=["SQL", "Jira", "Confluence", "Power BI", "BPMN"],
    ),
    Experience(
        title="Business Analyst",
        company="Sahab Retail Group",
        location="Jeddah, Saudi Arabia",
        start_date="2019-06",
        end_date="2022-02",
        current=False,
        achievements=[
            "Mapped the order-to-cash process end to end and documented the "
            "hand-offs that had never been written down before.",
            "Built the reporting layer used by category managers for weekly "
            "performance reviews.",
            "Coordinated user acceptance testing across three regional "
            "warehouses, tracking defects to closure before go-live.",
            "Standardised requirement templates later adopted by the wider "
            "PMO.",
        ],
        responsibilities=[
            "Supported the ERP rollout across retail operations as the "
            "business-side analyst.",
        ],
        technologies=["SAP", "Excel", "Visio", "SQL"],
    ),
]

_MASTER = MasterCV(
    contact=_CONTACT,
    headline="Senior Business Analyst · Banking & Retail Transformation",
    summary=(
        "Business analyst with six years across banking and retail "
        "transformation programmes in Saudi Arabia. Comfortable owning the "
        "analysis workstream end to end: discovery workshops, written "
        "specifications, acceptance criteria, and the follow-through that "
        "keeps delivery honest."
    ),
    experience=_EXPERIENCE,
    education=[
        Education(
            degree="Bachelor of Science",
            field_of_study="Management Information Systems",
            institution="King Saud University",
            location="Riyadh, Saudi Arabia",
            graduation_date="2019",
        )
    ],
    skills=[
        "Requirements Engineering", "Process Mapping (BPMN)",
        "Stakeholder Management", "SQL", "Power BI",
        "User Acceptance Testing", "Agile Delivery", "Data Analysis",
    ],
    certifications=[
        Certification(
            name="Certificate in Business Analysis",
            issuer="Tuwaiq Academy",
            date="2021",
        )
    ],
    languages=["Arabic (Native)", "English (Professional)"],
)

_TAILORED = TailoredCV(
    master_cv=_MASTER,
    job_title="Senior Business Analyst",
    company="Sample Employer",
    tailored_summary=_MASTER.summary,
    selected_experience=_EXPERIENCE,
    selected_skills=list(_MASTER.skills),
    selected_projects=[],
    modifications="sample artefact — rendered through the production path",
)




# ── the sample analysis report (the 29-SAR product) ─────────────────────────
# Built from the SAME evaluation engine production runs, over facts that
# mirror the fictional persona above — so the sample scores are real output,
# not numbers typed by hand.


def _sample_facts() -> list[Any]:
    """ProfileFact-shaped stand-ins for the fictional persona's bank."""
    import uuid

    from career.db.models import ProfileFact

    def fact(category: str, payload: dict[str, Any]) -> ProfileFact:
        return ProfileFact(
            id=uuid.uuid4(), tenant_id=uuid.uuid4(), category=category,
            payload=payload, status="CUSTOMER_CONFIRMED", source="sample",
        )

    facts = []
    for exp in _EXPERIENCE:
        facts.append(fact("experience", {
            "title": exp.title, "employer": exp.company,
            "start_date": exp.start_date, "end_date": exp.end_date or "",
            "achievements": list(exp.achievements),
            "skills_used": list(exp.technologies),
        }))
    for skill in _MASTER.skills:
        facts.append(fact("skill", {"name": skill}))
    facts.append(fact("education", {
        "degree": "Bachelor of Science",
        "field_of_study": "Management Information Systems",
        "institution": "King Saud University", "graduation_year": "2019",
    }))
    facts.append(fact("certification", {
        "name": "Certificate in Business Analysis", "issuer": "Tuwaiq Academy",
    }))
    return facts


def build_analysis_sample() -> Path:
    from career.funnel.evaluation import evaluate
    from career.funnel.report import render_report_pdf

    report = evaluate(
        _sample_facts(), requested_path="business_analysis", contact_found=True
    )
    out = OUT_DIR / "sample-analysis.pdf"
    render_report_pdf(report, report_date="2026-07-29", output_path=out)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cv_path = OUT_DIR / "sample-cv.pdf"
    render_cv_pdf(_TAILORED, cv_path)
    print(f"sample CV       → {cv_path} ({cv_path.stat().st_size / 1024:.0f} KB)")
    rep_path = build_analysis_sample()
    print(f"sample analysis → {rep_path} ({rep_path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
