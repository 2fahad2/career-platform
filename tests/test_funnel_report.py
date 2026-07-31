"""The Arabic RTL report PDF + WhatsApp summary (whitepaper §04) — before code.

One shareable A4 page (the report is a marketing tool by design), fully
Arabic and RTL, deterministic bytes for identical input, carrying the five
scores, the notes, the two-path comparison, and the three steps. The
WhatsApp summary is short, Arabic, and ends with the upgrade CTA.
"""

from __future__ import annotations

from pathlib import Path

from career.funnel import report as funnel_report
from career.funnel.evaluation import PathVerdict, Report

REPORT = Report(
    overall=78, clarity=85, achievements=70, fit=81, readability=75,
    notes=(
        "قوِّ إنجازاتك بأرقام قابلة للقياس.",
        "قسم المهارات قصير — أضف أدواتك الفعلية.",
    ),
    requested=PathVerdict("business_analyst", "محلل أعمال", 81),
    alternative=PathVerdict("it_operations", "عمليات تقنية المعلومات", 62),
    next_steps=(
        "استمر في مسار محلل أعمال.",
        "حدّث سيرتك بالملاحظات ثم أعد قياسها.",
        "الاشتراك في البحث اليومي: نبحث عنك كل ليلة.",
    ),
)


def test_report_html_is_rtl_arabic_and_complete() -> None:
    html = funnel_report.render_report_html(REPORT, report_date="2026-07-17")
    assert 'dir="rtl"' in html
    assert "78" in html and "81" in html and "62" in html
    assert "محلل أعمال" in html
    assert "عمليات تقنية المعلومات" in html
    for note in REPORT.notes:
        assert note in html
    for step in REPORT.next_steps:
        assert step in html
    assert "career-platform.net" in html          # shareable footer


def test_report_pdf_is_one_page_and_deterministic(tmp_path: Path) -> None:
    from pypdf import PdfReader

    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    # warm the font stack first — see the note in test_cv_render.py: on a cold
    # machine the warm-up alone changed the bytes, so the comparison measured
    # the runner's cache state instead of our determinism.
    funnel_report.render_report_pdf(REPORT, report_date="2026-07-17",
                                    output_path=tmp_path / "warmup.pdf")
    funnel_report.render_report_pdf(REPORT, report_date="2026-07-17",
                                    output_path=a)
    funnel_report.render_report_pdf(REPORT, report_date="2026-07-17",
                                    output_path=b)
    reader = PdfReader(str(a))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "78" in text                            # the overall score
    assert a.read_bytes() == b.read_bytes()        # golden determinism


def test_whatsapp_summary_is_short_arabic_with_cta() -> None:
    summary = funnel_report.whatsapp_summary(REPORT)
    assert len(summary) < 900
    assert "78" in summary and "محلل أعمال" in summary
    assert "62" in summary                         # the alternative score too
    assert "البحث اليومي" in summary               # the upgrade CTA
    assert summary.count("\n") >= 4                # readable lines, not a blob


def test_no_placeholder_leaks_in_the_report() -> None:
    html = funnel_report.render_report_html(REPORT, report_date="2026-07-17")
    for forbidden in ("{{", "}}", "None", "CANDIDATE"):
        assert forbidden not in html
