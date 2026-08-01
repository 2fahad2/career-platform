"""Can THIS machine draw the documents a paying customer receives?

Every other render test asks whether our code is correct. This one asks
whether the host is *equipped* — a different question, and the one that was
missing when CI spent weeks validating PDFs drawn in a font we do not ship.

Why these assertions and not another determinism check:

A byte-determinism test compares two renders made by the same process on the
same machine, so a machine that resolves "Liberation Sans" to a fallback face
produces two identical *wrong* documents and passes. That was measured: with
fontconfig restricted to DejaVu, the whole suite stayed green while the CV was
drawn in a face no approved sample ever used. Determinism cannot see a
substitution, because substitution is perfectly deterministic.

So the assertion here is the one that can: the fonts the templates NAME must
resolve to themselves, and the document we actually render must carry those
faces embedded in it. Both layers read the real artifacts — the font stacks
are parsed out of the rendered HTML, not restated here — so renaming a font in
a template moves this test with it instead of leaving it guarding a stale name.

Only two documents reach a customer: the tailored CV (`render_cv_pdf`, via
`cv/publish.py` and `cv/daily_run.py`) and the Arabic analysis report
(`render_report_pdf`, via `funnel/flow.py`). Both are rendered by the host
venv the systemd units run, not inside the api container, so the host font set
is what governs them. The cover-letter template has no production caller and
is deliberately out of scope.

A failure here means: do not deliver from this machine until the fonts named
in docs/RUNBOOK-DISASTER-RECOVERY.md step two are installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from career.cv import render as cv_render
from career.cv.schemas import (
    Certification,
    ContactInfo,
    Education,
    Experience,
    MasterCV,
    TailoredCV,
)
from career.funnel import report as funnel_report
from career.funnel.evaluation import PathVerdict, Report

# CSS generic families are never real files — fontconfig resolves them to
# whatever it likes and nothing is asserted about them.
_GENERIC = {"sans-serif", "serif", "monospace", "cursive", "fantasy", "system-ui"}


def _demo_cv() -> TailoredCV:
    """Placeholder-only CV — same shape as a delivered one, no real identity."""
    master = MasterCV(
        contact=ContactInfo(
            name="John Placeholder", email="john.placeholder@example.com",
            phone="+000 00 000 0000", location="Riyadh, Saudi Arabia",
        ),
        headline="IT Operations & Business Analysis Leader",
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
        tailored_summary="Business analysis leader with governance depth.",
        selected_experience=[
            Experience(title="Senior IT Operations Manager",
                       company="Example Financial Group", location="Riyadh",
                       start_date="2021-03", end_date=None, current=True,
                       achievements=["Cut incident backlog."]),
        ],
        selected_skills=["IT Service Management"],
        modifications="render-environment probe — placeholder data only",
    )


_REPORT = Report(
    overall=78, clarity=85, achievements=70, fit=81, readability=75,
    notes=("قوِّ إنجازاتك بأرقام قابلة للقياس.",),
    requested=PathVerdict("business_analyst", "محلل أعمال", 81),
    alternative=PathVerdict("it_operations", "عمليات تقنية المعلومات", 62),
    next_steps=("استمر في مسار محلل أعمال.",),
)


def _declared_first_choice_families(html: str) -> list[str]:
    """The first real family of every font stack in a rendered document.

    Read from the HTML the renderer is actually handed, so the test tracks the
    template instead of duplicating it.
    """
    families: list[str] = []
    for decl in re.findall(r"font-family:\s*([^;{}]+)", html):
        for raw in decl.split(","):
            name = raw.strip().strip('"').strip("'")
            if not name or name.lower() in _GENERIC:
                continue
            families.append(name)
            break
    assert families, "no font-family declaration found — the template lost its font stack"
    return families


def _embedded_basefonts(pdf_path: Path) -> set[str]:
    """The font faces actually embedded in a rendered PDF.

    PDF subset names look like `/VRMYHF+Liberation-Sans`; the six-letter tag
    is random per render, so only the part after `+` is meaningful.
    """
    from pypdf import PdfReader

    faces: set[str] = set()
    for page in PdfReader(str(pdf_path)).pages:
        resources = page.get("/Resources")
        if not resources:
            continue
        fonts = resources.get_object().get("/Font")
        if not fonts:
            continue
        fonts = fonts.get_object()
        for key in fonts:
            base = fonts[key].get_object().get("/BaseFont")
            if base:
                faces.add(str(base).lstrip("/").split("+", 1)[-1])
    return faces


def _resolves_to_itself(fc_match: str, family: str) -> tuple[bool, str]:
    """fontconfig always answers, so 'is it installed?' is 'did I get it back?'"""
    resolved = subprocess.run(  # noqa: S603 — absolute path, fixed argv, no shell
        [fc_match, "-f", "%{family}", family],
        capture_output=True, text=True, timeout=30, check=False,
    ).stdout
    wanted = family.casefold()
    got = [part.strip().casefold() for part in resolved.split(",")]
    return wanted in got, resolved


def _cv_html() -> str:
    return cv_render.render_cv_html(_demo_cv())


def _report_html() -> str:
    return funnel_report.render_report_html(_REPORT, report_date="2026-07-17")


# ── layer 1: the machine has the fonts the templates name ────────────────────


@pytest.mark.parametrize("label,render_html", [
    ("tailored CV", _cv_html),
    ("Arabic analysis report", _report_html),
])
def test_first_choice_font_of_each_customer_document_is_installed(
    label: str, render_html: Callable[[], str],
) -> None:
    fc_match = shutil.which("fc-match")
    if fc_match is None:
        pytest.skip("fontconfig absent — the embedding tests below still apply")
    for family in _declared_first_choice_families(render_html()):
        ok, resolved = _resolves_to_itself(fc_match, family)
        assert ok, (
            f"{label}: the template asks for {family!r} and this machine "
            f"answers {resolved!r}. Every {label} rendered here is drawn in a "
            f"substitute face. Install the fonts in step two of "
            f"docs/RUNBOOK-DISASTER-RECOVERY.md."
        )


# ── layer 2: the document we actually produce carries those faces ────────────


def test_delivered_cv_embeds_the_font_the_template_asks_for(tmp_path: Path) -> None:
    """Resolvable is not enough — prove the drawn document really used it."""
    html = cv_render.render_cv_html(_demo_cv())
    wanted = _declared_first_choice_families(html)[0].replace(" ", "-").casefold()

    out = tmp_path / "render_env_cv.pdf"
    cv_render.render_cv_pdf(_demo_cv(), out)
    faces = _embedded_basefonts(out)

    assert any(face.casefold().startswith(wanted) for face in faces), (
        f"the CV template asks for {wanted!r} but the rendered PDF embeds "
        f"{sorted(faces)} — this machine cannot draw what the customer receives"
    )


def test_delivered_arabic_report_embeds_its_arabic_face_and_real_arabic_text(
    tmp_path: Path,
) -> None:
    """The Arabic report is the one document where a fallback breaks shaping.

    Two assertions, because either alone is passable by a broken render: the
    Arabic face is embedded, and Arabic actually survives into the text layer.
    """
    from pypdf import PdfReader

    html = funnel_report.render_report_html(_REPORT, report_date="2026-07-17")
    wanted = _declared_first_choice_families(html)[0].replace(" ", "-").casefold()

    out = tmp_path / "render_env_report.pdf"
    funnel_report.render_report_pdf(_REPORT, report_date="2026-07-17",
                                    output_path=out)
    faces = _embedded_basefonts(out)

    assert any(face.casefold().startswith(wanted) for face in faces), (
        f"the Arabic report asks for {wanted!r} but the rendered PDF embeds "
        f"{sorted(faces)} — Arabic shaping is being done by a substitute face"
    )

    text = PdfReader(str(out)).pages[0].extract_text()
    assert "محلل أعمال" in text, (
        "the requested career path did not survive into the report text layer"
    )
