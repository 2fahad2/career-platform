"""The «عينة» request — a prospect asks to see the work before paying.

The store copy promises it literally: «تبي تشوف مستوى الشغل قبل تدفع؟ راسلنا
واتساب بكلمة عينة». This module is that promise, kept.

Design notes:

* It answers UNKNOWN numbers — a prospect has no subscription yet, so this
  runs on the pre-activation path, before the «send your activation code»
  fallback. No tenant, no DB writes, nothing to leak.
* The artefacts are FICTIONAL (built by scripts/build_samples.py) — never a
  real customer's CV, whatever the marketing convenience (§15.13 spirit).
* Sending is best-effort per file: a missing or unsendable sample degrades to
  the text intro rather than leaving the prospect with silence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("career.whatsapp")

#: Everything a prospect might plausibly type to ask for a sample.
SAMPLE_TRIGGERS = frozenset({
    "عينة", "عينه", "العينة", "العينه", "ابي عينة", "أبي عينة",
    "ابغى عينة", "أبغى عينة", "sample", "Sample", "SAMPLE",
})

#: Repo-relative artefacts produced by scripts/build_samples.py.
SAMPLE_CV = "data/samples/sample-cv.pdf"
SAMPLE_REPORT = "data/samples/sample-analysis.pdf"

_INTRO = (
    "أبشر 👌 هذي عينتان من شغلنا الحقيقي — بيانات شخص وهمي، بنفس الجودة "
    "اللي توصلك:"
)
_CV_CAPTION = (
    "١) سيرة ذاتية إنجليزية مفصّلة على وظيفة بعينها — تجيك جاهزة مع كل فرصة."
)
_REPORT_CAPTION = (
    "٢) تقرير تشخيص السيرة — درجاتك ومواطن الخلل وخطواتك التالية بالعربي."
)
_OUTRO = (
    "كل سطر في اللي فوق مصدره كلام صاحب السيرة نفسه — ما نخترع ولا رقم.\n"
    "تبي نبدأ معك؟ اطلب من المتجر وارجع لنا بالرمز 🙌"
)


def is_sample_request(text: str | None) -> bool:
    return bool(text) and str(text).strip() in SAMPLE_TRIGGERS


def send_samples(
    whatsapp_client: Any, to_phone: str, *, repo_root: Path | None = None
) -> bool:
    """Send the intro + both artefacts. True when at least one landed —
    the caller falls back to the normal reply when nothing could be sent."""
    root = repo_root or Path(__file__).resolve().parents[3]
    sent_any = False
    try:
        whatsapp_client.send_text(to_phone, _INTRO)
        sent_any = True
    except Exception:  # noqa: BLE001 — a prospect must never see a crash
        logger.warning("sample intro failed", exc_info=True)

    for rel, filename, caption in (
        (SAMPLE_CV, "sample-cv.pdf", _CV_CAPTION),
        (SAMPLE_REPORT, "sample-analysis.pdf", _REPORT_CAPTION),
    ):
        path = root / rel
        if not path.exists():
            logger.warning("sample artefact missing: %s", rel)
            continue
        try:
            whatsapp_client.send_document(
                to_phone, str(path), filename=filename, caption=caption
            )
            sent_any = True
        except Exception:  # noqa: BLE001
            logger.warning("sample document failed: %s", rel, exc_info=True)

    if sent_any:
        try:
            whatsapp_client.send_text(to_phone, _OUTRO)
        except Exception:  # noqa: BLE001
            logger.warning("sample outro failed", exc_info=True)
    return sent_any
