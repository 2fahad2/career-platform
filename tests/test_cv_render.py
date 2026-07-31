"""CV render core acceptance tests (LEGACY §1.1–§1.4, §1.10) — before code.

The centerpiece: the v5 CV template and the cover-letter template are
BYTE-VERBATIM constants, guarded by extracting the fenced blocks straight out
of LEGACY_KNOWLEDGE.md and comparing — the template can never drift from the
documented source of truth. fmt_month is the verbatim filter. Rendering goes
through Jinja with AUTOESCAPE ON (deliberately safer than legacy: summaries
and skills pass through an LLM/bank — markup in them must never execute) and
WeasyPrint to a real PDF verified §1.10-style: exactly one A4 page, sections
in order, dates via fmt_month.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from career.cv import render as cv_render
from career.cv import template as cv_template
from career.cv.schemas import (
    Certification,
    ContactInfo,
    Education,
    Experience,
    MasterCV,
    TailoredCV,
)

_LEGACY = Path(__file__).resolve().parents[1] / "docs" / "LEGACY_KNOWLEDGE.md"


def _fenced_block(section_marker: str, language: str) -> str:
    """Extract the first ```<language> fenced block after a section heading."""
    text = _LEGACY.read_text(encoding="utf-8")
    start = text.index(section_marker)
    fence_open = text.index(f"```{language}\n", start) + len(f"```{language}\n")
    fence_close = text.index("\n```", fence_open)
    return text[fence_open:fence_close]


# ── byte-verbatim template guards (the §15.15 discipline: doc precedes code) ──


def test_cv_template_is_byte_verbatim_with_legacy() -> None:
    assert cv_template.CV_TEMPLATE == _fenced_block(
        "### 1.2 The CV template", "html"
    )


def test_cover_letter_template_is_byte_verbatim_with_legacy() -> None:
    assert cv_template.COVER_LETTER_TEMPLATE == _fenced_block(
        "### 1.7 Cover letter template", "html"
    )


# ── fmt_month (§1.3 verbatim) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2021-03", "Mar 2021"),
        ("2013-06", "Jun 2013"),
        (None, "Present"),
        ("", "Present"),
        ("Present", "Present"),
        ("current", "Present"),
        ("NOW", "Present"),
        ("2021-13", "2021-13"),          # invalid month → untouched passthrough
        ("June 2013", "June 2013"),      # unparseable → untouched
        ("2020", "2020"),                # no month part → untouched
        ("2020-01-15", "Jan 2020"),      # day tolerated, month wins
    ],
)
def test_fmt_month(value: str | None, expected: str) -> None:
    assert cv_render.fmt_month(value) == expected


# ── safe scratch names (§1.4) ────────────────────────────────────────────────


def test_safe_name() -> None:
    assert cv_render.safe_name("Senior BA / Analyst") == "Senior_BA_Analyst"
    assert cv_render.safe_name("a  b\\c") == "a_b_c"
    assert cv_render.safe_name("***") == "untitled"


# ── schemas are strict (§1.1) ────────────────────────────────────────────────


def test_schema_rejects_invalid_email() -> None:
    with pytest.raises(ValidationError):
        ContactInfo(name="X", email="not-an-email", phone="+9665",
                    location="Riyadh")


# ── the demo render (§1.10): one page, ordered sections, filtered dates ──────


def _demo_cv() -> TailoredCV:
    master = MasterCV(
        contact=ContactInfo(
            name="John Placeholder", email="john.placeholder@example.com",
            phone="+000 00 000 0000", location="Riyadh, Saudi Arabia",
            linkedin="linkedin.com/in/placeholder",
        ),
        headline="IT Operations & Business Analysis Leader | PMP | PMI-PBA",
        summary="Placeholder summary.",
        education=[Education(degree="Bachelor of Science",
                             field_of_study="Information Systems",
                             institution="Example University", location="Riyadh",
                             graduation_date="2013-06")],
        certifications=[Certification(name="PMP", issuer="PMI", date="2020-01")],
        languages=["Arabic (Native)", "English (Fluent)"],
    )
    return TailoredCV(
        master_cv=master, job_title="IT Operations Manager",
        company="Demo Employer",
        tailored_summary=(
            "Business analysis leader with governance depth across regulated "
            "environments. Delivered measurable service improvements."
        ),
        selected_experience=[
            Experience(title="Senior IT Operations Manager",
                       company="Example Financial Group", location="Riyadh",
                       start_date="2021-03", end_date=None, current=True,
                       achievements=["Cut incident backlog.",
                                     "Raised SLA adherence.",
                                     "Led ITSM migration.",
                                     "Built governance reporting."]),
        ],
        selected_skills=["IT Service Management", "ITIL v4", "Stakeholder Management"],
        modifications="demo render — placeholder data only",
    )


def test_html_render_sections_in_v5_order_and_dates_filtered() -> None:
    html = cv_render.render_cv_html(_demo_cv())
    positions = [
        html.index("John Placeholder"),
        html.index("IT Operations &amp; Business Analysis Leader"),
        html.index("john.placeholder@example.com"),
        html.index("Business analysis leader"),
        html.index("Professional Experience"),
        html.index("Education"),
        html.index(">Skills<"),
        html.index("Certifications:"),
        html.index("Languages:"),
    ]
    assert positions == sorted(positions)            # §1.2 top→bottom order
    assert "Mar 2021 - Present" in html              # fmt_month + current role
    assert "Jun 2013" in html
    assert "Projects" not in html                    # optional section absent


def test_render_escapes_hostile_markup() -> None:
    """Safer-than-legacy deliberately: bank/LLM text must never inject HTML."""
    cv = _demo_cv()
    hostile = cv.model_copy(update={
        "tailored_summary": '<script>alert(1)</script> honest text.',
    })
    html = cv_render.render_cv_html(hostile)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_pdf_renders_exactly_one_page(tmp_path: Path) -> None:
    from pypdf import PdfReader

    out = tmp_path / "demo_template_check.pdf"
    cv_render.render_cv_pdf(_demo_cv(), out)
    reader = PdfReader(str(out))
    assert len(reader.pages) == 1                    # the §1.5 contract
    text = reader.pages[0].extract_text()
    assert "John Placeholder" in text
    assert "Mar 2021 - Present" in text


def test_cover_letter_renders_paragraphs(tmp_path: Path) -> None:
    from pypdf import PdfReader

    out = tmp_path / "cover_letter_check.pdf"
    cv_render.render_cover_letter_pdf(
        _demo_cv(),
        letter_text="First paragraph.\n\nSecond paragraph.",
        current_date="16 July 2026",
        output_path=out,
    )
    reader = PdfReader(str(out))
    text = reader.pages[0].extract_text()
    assert "First paragraph." in text
    assert "Second paragraph." in text
    assert "16 July 2026" in text
    assert "Demo Employer" in text


def test_pdf_render_is_deterministic_for_identical_input(tmp_path: Path) -> None:
    """Golden-file discipline: same input → byte-identical PDF.

    The first render in a process warms the font stack (fontconfig cache,
    WeasyPrint's font objects). On a cold machine that warm-up alone changed
    the bytes, which is how this test failed on CI while passing on a
    developer box that had rendered all day. The warm-up render below makes
    the comparison measure OUR determinism — dict ordering, timestamps,
    anything we introduce — instead of the runner's cache state.
    """
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    cv_render.render_cv_pdf(_demo_cv(), tmp_path / "warmup.pdf")
    cv_render.render_cv_pdf(_demo_cv(), a)
    cv_render.render_cv_pdf(_demo_cv(), b)
    assert a.read_bytes() == b.read_bytes()
