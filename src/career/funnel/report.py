"""The Arabic RTL report — one shareable A4 page + the WhatsApp summary.

Our own artifact (no LEGACY source): the same visual language as the v5 CV
(the blue accents, the thin rules) but fully Arabic and RTL. Pango shapes
Arabic natively under WeasyPrint; the font is the pinned system Noto Naskh
Arabic. Deterministic bytes for identical input — same golden discipline as
the CV renderer. The report is deliberately shareable (footer brand): the
customer forwarding it IS the acquisition funnel (§04).
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import DictLoader, Environment

from career.funnel.evaluation import Report

REPORT_TEMPLATE = """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
    <meta charset="UTF-8">
    <title>تقرير تحليل السيرة الذاتية</title>
    <style>
        @page { size: A4; margin: 14mm 16mm; }
        html, body {
            font-family: "Noto Naskh Arabic","DejaVu Sans",sans-serif;
            color: #111; font-size: 10.5pt; line-height: 1.7; margin: 0;
            direction: rtl;
        }
        .brand    { font-size: 9pt; color: #7f8c8d; }
        .title    { font-size: 20pt; font-weight: 700; color: #2c3e50; margin-top: 2mm; }
        .date     { font-size: 9pt; color: #95a5a6; }
        .overall-box {
            display: flex; align-items: baseline; gap: 6mm;
            border-bottom: 0.5pt solid #bdc3c7; padding: 3mm 0 4mm 0;
        }
        .overall-num  { font-size: 34pt; font-weight: 700; color: #2c3e50; }
        .overall-label{ font-size: 12pt; font-weight: 700; color: #34495e; }
        .section  {
            font-size: 13pt; font-weight: 700; color: #34495e;
            border-bottom: 0.5pt solid #bdc3c7; padding-bottom: 1mm;
            margin: 5mm 0 2.5mm 0;
        }
        table.scores { width: 100%; border-collapse: collapse; }
        table.scores td { padding: 1.2mm 0; font-size: 10.5pt; }
        td.score-num { width: 14mm; font-weight: 700; color: #2c3e50; }
        .bar-wrap { background: #ecf0f1; border-radius: 2pt; height: 3.2mm; width: 100%; }
        .bar      { background: #2c3e50; border-radius: 2pt; height: 3.2mm; }
        ol.notes  { margin: 0 5mm 0 0; padding: 0; }
        ol.notes li { margin: 1.4mm 0; }
        .compare  {
            display: flex; gap: 5mm; margin-top: 1mm;
        }
        .path-card {
            flex: 1; border: 0.4pt solid #dde2e5; border-radius: 3pt;
            padding: 2.5mm 3.5mm;
        }
        .path-name  { font-weight: 700; color: #2c3e50; }
        .path-score { font-size: 16pt; font-weight: 700; color: #34495e; }
        .path-tag   { font-size: 8.5pt; color: #7f8c8d; }
        ul.steps  { margin: 0 5mm 0 0; padding: 0; }
        ul.steps li { margin: 1.4mm 0; }
        .footer   {
            margin-top: 6mm; padding-top: 2mm; border-top: 0.5pt solid #bdc3c7;
            font-size: 8.5pt; color: #95a5a6;
        }
    </style>
</head>
<body>
    <div class="brand">لمّاح · مساعد التوظيف الشخصي</div>
    <div class="title">تقرير تحليل السيرة الذاتية</div>
    <div class="date">تاريخ التقييم: {{ report_date }}</div>

    <div class="overall-box">
        <div class="overall-num">{{ report.overall }}</div>
        <div class="overall-label">الدرجة العامة من 100</div>
    </div>

    <div class="section">الدرجات التفصيلية</div>
    <table class="scores">
        {% for label, value in detail_scores %}
        <tr>
            <td style="width: 42mm;">{{ label }}</td>
            <td class="score-num">{{ value }}</td>
            <td><div class="bar-wrap"><div class="bar"
                style="width: {{ value }}%;"></div></div></td>
        </tr>
        {% endfor %}
    </table>

    <div class="section">أهم الملاحظات</div>
    <ol class="notes">
        {% for note in report.notes %}<li>{{ note }}</li>{% endfor %}
    </ol>

    <div class="section">مسارك المطلوب مقابل البديل المقترح</div>
    <div class="compare">
        <div class="path-card">
            <div class="path-tag">مسارك المطلوب</div>
            <div class="path-name">{{ report.requested.label_ar }}</div>
            <div class="path-score">{{ report.requested.score }}</div>
        </div>
        {% if report.alternative %}
        <div class="path-card">
            <div class="path-tag">البديل الأقرب لملفك</div>
            <div class="path-name">{{ report.alternative.label_ar }}</div>
            <div class="path-score">{{ report.alternative.score }}</div>
        </div>
        {% endif %}
    </div>

    <div class="section">خطواتك الثلاث التالية</div>
    <ul class="steps">
        {% for step in report.next_steps %}<li>{{ step }}</li>{% endfor %}
    </ul>

    <div class="footer">
        أُعدّ هذا التقرير آليًا من محتوى سيرتك كما قُرئت — بلا مجاملة وبلا
        تهويل. · لمّاح
    </div>
</body>
</html>
"""

_env = Environment(
    loader=DictLoader({"report.html": REPORT_TEMPLATE}), autoescape=True
)

_DETAIL_LABELS = (
    ("وضوح المسار", "clarity"),
    ("قوة الإنجازات", "achievements"),
    ("الملاءمة للمسار المطلوب", "fit"),
    ("جودة القراءة الآلية", "readability"),
)


def render_report_html(report: Report, *, report_date: str) -> str:
    detail_scores = [
        (label, getattr(report, attr)) for label, attr in _DETAIL_LABELS
    ]
    return _env.get_template("report.html").render(
        report=report, report_date=report_date, detail_scores=detail_scores
    )


def render_report_pdf(
    report: Report, *, report_date: str, output_path: Path
) -> Path:
    from weasyprint import HTML  # deferred: heavy native import

    HTML(string=render_report_html(report, report_date=report_date)).write_pdf(
        str(output_path)
    )
    return output_path


def whatsapp_summary(report: Report) -> str:
    """The in-chat Arabic summary — short lines, both path scores, the CTA."""
    lines = [
        "📊 *تقرير تحليل سيرتك جاهز!*",
        f"الدرجة العامة: *{report.overall}/100*",
        f"• وضوح المسار: {report.clarity}",
        f"• قوة الإنجازات: {report.achievements}",
        f"• الملاءمة لمسار {report.requested.label_ar}: {report.fit}",
        f"• جودة القراءة الآلية: {report.readability}",
    ]
    if report.alternative is not None:
        lines.append(
            f"البديل الأقرب لملفك: {report.alternative.label_ar} "
            f"({report.alternative.score})"
        )
    if report.notes:
        lines.append(f"أهم ملاحظة: {report.notes[0]}")
    lines.append("التقرير الكامل في ملف PDF المرفق 📎")
    lines.append(
        "وإذا حاب نشتغل عنك: اشترك في البحث اليومي — نبحث كل ليلة ونجهّز "
        "لك CV مخصصًا لكل فرصة."
    )
    return "\n".join(lines)


__all__ = ["render_report_html", "render_report_pdf", "whatsapp_summary"]
