"""Rendering — LEGACY §1.3/§1.4 on the byte-verbatim v5 template.

One deliberate, documented improvement over legacy: Jinja AUTOESCAPE is ON.
Summaries and skills pass through an LLM and the customer bank — markup in
them must render as text, never execute. The template itself is unchanged
(it contains no |safe filters, so escaping is purely additive protection).

Determinism: WeasyPrint stamps no wall-clock into the PDF when the input HTML
is identical and fonts are pinned (Liberation/DejaVu system packages) — the
golden-file test holds same-input → same DOCUMENT (page count and extracted
text). NOT byte-identical: PDF bytes depend on the font stack and on
fontconfig's cache state, neither of which we control — it held on a warm
developer machine and did not on a cold CI runner. Nothing may key on a PDF's
sha256 for identity or de-duplication; publishing hashes whatever bytes it
produced, which is the only place that hash is meaningful.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import DictLoader, Environment

from career.cv.schemas import TailoredCV
from career.cv.template import COVER_LETTER_TEMPLATE, CV_TEMPLATE

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_month(yyyy_mm: object) -> str:
    """LEGACY §1.3 verbatim: 'YYYY-MM' → 'MMM YYYY'; empty/Present-ish →
    'Present'; anything unparseable passes through untouched."""
    if not yyyy_mm:
        return "Present"
    s = str(yyyy_mm).strip()
    if s.lower() in {"present", "current", "now"}:
        return "Present"
    try:
        parts = s.split("-")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            m = int(parts[1])
            if 1 <= m <= 12:
                return f"{_MONTHS[m]} {parts[0]}"
    except Exception:  # noqa: BLE001, S110 — verbatim: formatting never raises
        pass
    return s


def safe_name(value: str) -> str:
    """LEGACY §1.4 scratch-name sanitizer (canonical names come from §7)."""
    name = re.sub(r"[\s/\\]+", "_", value)
    name = re.sub(r"[^\w\-]", "", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_") or "untitled"


_env = Environment(
    loader=DictLoader({
        "cv_template.html": CV_TEMPLATE,
        "cover_letter_template.html": COVER_LETTER_TEMPLATE,
    }),
    autoescape=True,
)
_env.filters["fmt_month"] = fmt_month


def render_cv_html(cv: TailoredCV) -> str:
    return _env.get_template("cv_template.html").render(cv=cv)


def render_cv_pdf(cv: TailoredCV, output_path: Path) -> Path:
    from weasyprint import HTML  # deferred: heavy native import

    HTML(string=render_cv_html(cv)).write_pdf(str(output_path))
    return output_path


def render_cover_letter_html(
    cv: TailoredCV, *, letter_text: str, current_date: str
) -> str:
    paragraphs = [p.strip() for p in letter_text.split("\n\n") if p.strip()]
    return _env.get_template("cover_letter_template.html").render(
        cv=cv, letter_paragraphs=paragraphs, current_date=current_date
    )


def render_cover_letter_pdf(
    cv: TailoredCV, *, letter_text: str, current_date: str, output_path: Path
) -> Path:
    from weasyprint import HTML

    html = render_cover_letter_html(
        cv, letter_text=letter_text, current_date=current_date
    )
    HTML(string=html).write_pdf(str(output_path))
    return output_path
