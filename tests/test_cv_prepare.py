"""Normalization + one-page enforcement + pre-render validation — before code.

LEGACY §1.9 rules ported onto OUR sources (D6: the confirmed achievement bank
+ customer profile, never a master_cv.json): display_company order, the
Security+ display rule, defaults, current-role detection, first-wins skill
dedupe, language formatting. §1.5 caps (400/4/4/2/14) + full per-role pacing
[4,4,4,4] with achievements verbatim first and JD-keyword-ranked
responsibilities filling the rest — NO invention, every bullet originates
from the bank. §1.6 the complete-sentence authority (career_core). The
pre-render validator blocks on issues (Arabic leak, bad email, short summary)
and only warns on style.
"""

from __future__ import annotations

from career.cv import enforce, normalize, validate
from career.cv.schemas import Experience, TailoredCV

CONTACT = {
    "name": "Fahad Placeholder",
    "email": "fahad@example.com",
    "phone": "+966500000000",
    "location": "Riyadh, Saudi Arabia",
}


def _bank(**overrides: list[dict]) -> dict[str, list[dict]]:
    bank: dict[str, list[dict]] = {
        "experience": [
            {"title": "Senior Business Analyst", "employer": "Alpha Bank",
             "start_date": "2021-03", "end_date": "Present",
             "description": "Led requirements workshops. Managed UAT cycles.",
             "achievements": ["Cut rework by a third."]},
            {"title": "Business Analyst", "employer": "Beta Co",
             "start_date": "2018-01", "end_date": "2021-02",
             "description": None,
             "achievements": ["Delivered ERP rollout."]},
        ],
        "education": [
            {"degree": "Bachelor of Science", "field_of_study": "MIS",
             "institution": "King Saud University", "graduation_year": 2017},
        ],
        "certification": [
            {"name": "PMP", "issuer": "PMI", "issue_date": "2020-01"},
            {"name": "Security+", "issuer": "CompTIA", "issue_date": None},
        ],
        "skill": [{"name": "SQL"}, {"name": "Power BI"}, {"name": "SQL"}],
        "language": [
            {"language": "Arabic", "proficiency": "Native"},
            {"language": "English", "proficiency": None},
        ],
    }
    bank.update(overrides)
    return bank


# ── §1.9 normalization from the bank ─────────────────────────────────────────


def test_experience_normalization_rules() -> None:
    master = normalize.build_master_cv(
        contact=CONTACT, bank=_bank(), headline="BA Leader", summary="Base."
    )
    first = master.experience[0]
    assert first.company == "Alpha Bank"                # employer → company
    assert first.current is True                        # "Present" detected
    assert first.end_date is None                       # forced for current
    assert first.location == "Riyadh, Saudi Arabia"     # default when absent
    # description → complete-sentence responsibilities (truthful subset)
    assert first.responsibilities == [
        "Led requirements workshops.", "Managed UAT cycles.",
    ]
    assert first.achievements == ["Cut rework by a third."]
    second = master.experience[1]
    assert second.current is False and second.end_date == "2021-02"
    assert second.responsibilities == []                # None description


def test_display_company_wins_over_employer() -> None:
    bank = _bank()
    bank["experience"][0]["display_company"] = "Client Brand"
    master = normalize.build_master_cv(
        contact=CONTACT, bank=bank, headline=None, summary="Base."
    )
    assert master.experience[0].company == "Client Brand"


def test_certification_display_rules() -> None:
    master = normalize.build_master_cv(
        contact=CONTACT, bank=_bank(), headline=None, summary="Base."
    )
    names = [c.name for c in master.certifications]
    assert "PMP" in names
    assert "CompTIA Security+" in names                 # the Security+ rule
    security = next(c for c in master.certifications if "Security" in c.name)
    assert security.date == "N/A"                       # missing issue_date


def test_education_year_and_missing_fields() -> None:
    master = normalize.build_master_cv(
        contact=CONTACT, bank=_bank(), headline=None, summary="Base."
    )
    edu = master.education[0]
    assert edu.graduation_date == "2017"                # int year → str
    assert edu.location == "Riyadh, Saudi Arabia"       # default


def test_skills_first_wins_and_languages_formatted() -> None:
    master = normalize.build_master_cv(
        contact=CONTACT, bank=_bank(), headline=None, summary="Base."
    )
    assert master.skills == ["SQL", "Power BI"]         # dedupe, order kept
    assert master.languages == ["Arabic (Native)", "English"]


# ── §1.5 one-page enforcement ────────────────────────────────────────────────


def _tailored(n_roles: int = 5, bullets_per_role: int = 6) -> TailoredCV:
    master = normalize.build_master_cv(
        contact=CONTACT, bank=_bank(), headline=None, summary="Base."
    )
    roles = [
        Experience(
            title=f"Role {i}", company=f"Co {i}", location="Riyadh",
            start_date="2020-01", end_date="2021-01",
            achievements=[f"Achievement {i}.{j}." for j in range(2)],
            responsibilities=[
                "Managed stakeholder governance reviews.",
                "Ran ERP data migration cycles.",
                "Wrote requirements and user stories.",
                "Handled office supplies restocking.",
            ][: bullets_per_role - 2],
        )
        for i in range(n_roles)
    ]
    return TailoredCV(
        master_cv=master, job_title="Business Analyst", company="Target",
        tailored_summary=(
            "Seasoned business analyst with governance depth. "
            "Delivered measurable improvements across regulated programs. "
            "Known for stakeholder alignment and disciplined execution. "
            "Led ERP and data initiatives end to end. "
            "Focused on outcomes over activity and clarity over noise. "
            "Comfortable across business and technology boundaries. "
            "Trusted by executives for honest reporting."
        ),
        selected_experience=roles,
        selected_skills=[f"Skill {i}" for i in range(20)],
        modifications="test",
    )


def test_enforce_caps_and_designer_pacing() -> None:
    result = enforce.enforce_one_page(
        _tailored(), jd_keywords=("requirements", "erp", "stakeholder")
    )
    assert len(result.selected_experience) == 4          # max entries
    assert len(result.selected_skills) == 14             # max skills
    counts = [len(e.achievements) for e in result.selected_experience]
    assert counts == [4, 4, 4, 4]                        # full pacing [4,4,4,4]
    assert len(result.tailored_summary) <= 400
    # achievements come first, verbatim
    top = result.selected_experience[0]
    assert top.achievements[0].startswith("Achievement 0")
    # fill slots are JD-keyword-ranked responsibilities: the irrelevant
    # office-supplies line must lose to requirements/ERP/stakeholder lines
    assert "Handled office supplies restocking." not in top.achievements


def test_enforce_never_invents_bullets() -> None:
    thin = _tailored(n_roles=1, bullets_per_role=2)      # 2 achievements only
    result = enforce.enforce_one_page(thin, jd_keywords=())
    assert result.selected_experience[0].achievements == [
        "Achievement 0.0.", "Achievement 0.1.",
    ]                                                    # fewer, never padded


def test_enforce_summary_keeps_complete_sentences() -> None:
    result = enforce.enforce_one_page(_tailored(), jd_keywords=())
    s = result.tailored_summary
    assert s.endswith(".") and "..." not in s


# ── pre-render validation (issues block, warnings pass) ─────────────────────


def test_arabic_leak_is_an_issue() -> None:
    cv = _tailored()
    leaked = cv.model_copy(update={"selected_skills": ["تحليل الأعمال"]})
    valid, issues, _ = validate.validate_pre_render(leaked)
    assert not valid
    assert any("arabic" in i for i in issues)


def test_short_or_thin_summary_is_an_issue() -> None:
    cv = _tailored().model_copy(update={"tailored_summary": "Too short."})
    valid, issues, _ = validate.validate_pre_render(cv)
    assert not valid
    assert any("summary" in i for i in issues)


def test_weak_phrases_and_low_achievements_only_warn() -> None:
    cv = _tailored()
    weak = cv.model_copy(update={"tailored_summary": (
        "Results-driven team player delivering business analysis programs "
        "across regulated banking environments with sustained measurable "
        "outcomes and disciplined stakeholder governance for over a decade."
    )})
    valid, issues, warnings = validate.validate_pre_render(weak)
    assert valid and not issues
    assert any("weak" in w for w in warnings)


def test_healthy_cv_passes_clean() -> None:
    cv = enforce.enforce_one_page(_tailored(), jd_keywords=("erp",))
    valid, issues, _ = validate.validate_pre_render(cv)
    assert valid and issues == []
