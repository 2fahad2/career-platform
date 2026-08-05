"""§15.8 at every model boundary — the promise printed on the store page.

«كل نداء يمر بعد تجريد اسمك وجوالك وبريدك» is published to customers and to
the regulator. This file exists because two of the six Claude call sites had
no gate at all, and one of them was reachable with the customer's raw WhatsApp
text: a free-text onboarding answer is stored verbatim as an experience title,
which the tailoring chain JSON-dumps straight into the ranking prompt.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from career.cv.generate import PiiGuardedLlm
from career.onboarding.extraction import (
    PiiLeak,
    assert_no_pii,
    infer_header_name,
    strip_pii,
)


class _Recorder:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "{}"


def test_the_customer_name_never_reaches_the_model() -> None:
    inner = _Recorder()
    guarded = PiiGuardedLlm(inner, known_name="Fahad Almulhim")

    guarded.complete("Rank these: Fahad Almulhim, branch manager")

    assert inner.prompts, "the call must still go through"
    assert "Fahad" not in inner.prompts[0]
    assert "Almulhim" not in inner.prompts[0]


def test_an_email_or_phone_in_the_bank_never_reaches_the_model() -> None:
    inner = _Recorder()
    guarded = PiiGuardedLlm(inner)

    guarded.complete("contact me on fahad@example.com or 0501234567")

    sent = inner.prompts[0]
    assert "fahad@example.com" not in sent
    assert "0501234567" not in sent


def test_a_leak_that_survives_stripping_is_not_sent_at_all() -> None:
    """Fail closed. The chain degrades to deterministic rules, so the
    customer still gets a CV — we lose a model call, not the promise."""

    class _Hostile(_Recorder):
        pass

    inner = _Hostile()
    guarded = PiiGuardedLlm(inner, known_name="Fahad")

    class _NoStrip:
        text = "Fahad stayed in the text"

    import career.onboarding.extraction as extraction

    original = extraction.strip_pii
    extraction.strip_pii = lambda text, **kw: _NoStrip()   # simulate a miss
    try:
        raised = False
        try:
            guarded.complete("Fahad")
        except PiiLeak:
            raised = True
    finally:
        extraction.strip_pii = original

    assert raised, "a surviving leak must raise, not be sent"
    assert not inner.prompts, "nothing may go on the wire after a leak"


def test_every_model_call_site_sits_behind_a_gate() -> None:
    """The structural guard: a NEW `.messages.create(` added without a PII
    gate in its module fails here, the way the two ungated sites would have.
    """
    src = pathlib.Path("src/career")
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        if ".messages.create(" not in body:
            continue
        gated = ("strip_pii" in body or "assert_no_pii" in body
                 or "PiiGuardedLlm" in body)
        if not gated:
            offenders.append(str(path))
    assert offenders == [], (
        "these modules call a model with no PII gate in sight: "
        + ", ".join(offenders)
    )


def test_the_tailoring_chain_installs_the_gate_itself() -> None:
    """Wrapping the client — not patching call sites — is what makes a future
    prompt safe by construction. Pin that it is still wired that way."""
    body = pathlib.Path("src/career/cv/generate.py").read_text(encoding="utf-8")
    assert re.search(r"llm\s*=\s*PiiGuardedLlm\(llm", body)


# ── the paid path was weaker than the free one (audit 2026-08-05) ────────────
#
# The 49-SAR funnel derives the name from the CV header before stripping,
# because it never asks for one. The PAID path passes CustomerProfile.
# cv_full_name — a field collection.py forces to be LATIN — and matched it as
# a literal case-insensitive substring. So the cheaper product was better
# protected than the subscription: an Arabic header half, a different
# transliteration, or simply no profile row and the customer's own name went
# to the provider verbatim, against «اسمك ورقم جوالك لا يُرسلان إلى أي نموذج»
# on the published privacy page. Every case below is an ordinary Saudi CV, not
# an adversarial one. Names here are the synthetic فلان الفلاني / Fulan
# Alfulani placeholders — never a customer's.

_TYPED_NAME = "Fulan Alfulani"

CV_BILINGUAL = """Fulan Alfulani / فلان الفلاني
Riyadh, Saudi Arabia
fulan.a@example.com | +966 50 000 0000

SUMMARY
Business analyst with six years in retail banking operations.

EXPERIENCE
Branch operations supervisor at Example Trading Company (2019 - 2023)
- Led a service quality program across four branches

EDUCATION
B.Sc. Information Systems, 2015
"""

CV_VARIANT_SPELLING = """Fulan Alfulani
fulan.a@example.com | +966 50 000 0000

SUMMARY
Business analyst with six years in retail banking operations.

EXPERIENCE
Branch operations supervisor at Example Trading Company (2019 - 2023)
- Led a service quality program across four branches

EDUCATION
B.Sc. Information Systems, 2015

References available on request from Fulaan Al-Fulani.
"""

CV_ARABIC_ONLY = """فلان الفلاني
الرياض
البريد fulan.a@example.com والجوال ٠٥٠٠٠٠٠٠٠٠

نبذة
محلل أعمال بخبرة ست سنوات في عمليات التجزئة.

الخبرات
مشرف عمليات فرع في شركة المثال التجارية (2019 - 2023)

التعليم
بكالوريوس نظم معلومات
"""

CV_ARABIC_DIACRITICS = """فُلان الفلانى
الرياض
البريد fulan.a@example.com والجوال ٠٥٠٠٠٠٠٠٠٠

نبذة
محلل أعمال بخبرة ست سنوات في عمليات التجزئة.

الخبرات
مشرف عمليات فرع في شركة المثال التجارية (2019 - 2023)

التعليم
بكالوريوس نظم معلومات
"""

CV_HEADER_DIFFERS = """Fulaan Bakr Al-Fulani
fulan.a@example.com | +966 50 000 0000

SUMMARY
Business analyst with six years in retail banking operations.

EXPERIENCE
Branch operations supervisor at Example Trading Company (2019 - 2023)
- Led a service quality program across four branches

EDUCATION
B.Sc. Information Systems, 2015
"""


def test_the_arabic_half_of_a_bilingual_header_is_stripped_too() -> None:
    """Leak 1. «Fulan Alfulani / فلان الفلاني» is an ordinary Saudi CV header.
    cv_full_name is Latin by validation, so the Arabic half matched nothing
    and rode to the provider — the same person's name, twice as identifying."""
    safe = strip_pii(CV_BILINGUAL, known_name=_TYPED_NAME).text

    assert "Fulan" not in safe and "Alfulani" not in safe
    assert "فلان" not in safe
    assert "الفلاني" not in safe
    assert "[NAME]" in safe
    # the professional content — the only reason we call the model — survives
    assert "Branch operations supervisor" in safe
    assert "service quality program" in safe


def test_a_different_transliteration_of_the_same_name_is_stripped() -> None:
    """Leak 2. No Arabic needed: the customer types Fulan Alfulani, the CV
    says Fulaan Al-Fulani in the references line at the bottom. Pure ASCII,
    literal match fails, name sent."""
    safe = strip_pii(CV_VARIANT_SPELLING, known_name=_TYPED_NAME).text

    assert "Fulaan" not in safe
    assert "Al-Fulani" not in safe and "Fulani" not in safe


def test_a_missing_profile_does_not_disable_name_stripping() -> None:
    """Leak 3, the total one: known_name=None stripped ZERO name tokens and
    assert_no_pii then asserted nothing about names at all. The funnel already
    recovers the name from the header; the paid path must not be weaker."""
    safe = strip_pii(CV_VARIANT_SPELLING, known_name=None).text

    assert "Fulan" not in safe and "Alfulani" not in safe
    assert "[NAME]" in safe


def test_an_arabic_only_cv_has_its_header_name_stripped() -> None:
    safe = strip_pii(CV_ARABIC_ONLY, known_name=None).text

    assert "فلان" not in safe
    assert "الفلاني" not in safe
    assert "مشرف عمليات فرع" in safe  # the work content survives


def test_diacritics_and_hamza_variants_of_the_name_are_stripped() -> None:
    """«فُلان الفلانى» is the same name as «فلان الفلاني» to every human and
    to no substring matcher: a damma and a dotless yaa are enough to leak."""
    safe = strip_pii(CV_ARABIC_DIACRITICS, known_name="فلان الفلاني").text

    assert "فُلان" not in safe
    assert "الفلانى" not in safe


def test_typed_name_and_header_name_are_both_stripped() -> None:
    """They disagree in the real world — a middle name on the CV, a short one
    typed on WhatsApp. Both are the customer, so both must go."""
    safe = strip_pii(CV_HEADER_DIFFERS, known_name=_TYPED_NAME).text

    for leaked in ("Fulan", "Fulaan", "Bakr", "Al-Fulani", "Alfulani"):
        assert leaked not in safe, f"{leaked} survived"
    assert "Example Trading Company" in safe


def test_the_paid_path_is_no_weaker_than_the_free_funnel() -> None:
    """The funnel feeds infer_header_name into strip_pii; the paid path never
    called it. Whatever the funnel would have stripped, the paid path strips."""
    funnel_safe = strip_pii(
        CV_HEADER_DIFFERS, known_name=infer_header_name(CV_HEADER_DIFFERS)
    ).text
    paid_safe = strip_pii(CV_HEADER_DIFFERS, known_name=_TYPED_NAME).text

    for token in ("Fulaan", "Bakr", "Al-Fulani"):
        assert token not in funnel_safe
        assert token not in paid_safe


def test_the_header_heuristic_never_eats_a_city_or_a_job_title() -> None:
    """The counterweight. Reading a name out of the document is only safe if
    it cannot swallow the content we are paying the model to read: a header
    line is «name + maybe a title», and the line under it is usually a city."""
    cv = CV_VARIANT_SPELLING.replace(
        "Fulan Alfulani", "Fulan Alfulani Senior Analyst\nRiyadh Saudi Arabia"
    )
    safe = strip_pii(cv, known_name=None).text

    assert "Fulan" not in safe and "Alfulani" not in safe
    assert "Senior Analyst" in safe
    assert "Riyadh Saudi Arabia" in safe


def test_a_role_title_or_a_whatsapp_answer_is_never_read_as_a_document() -> None:
    """strip_pii is the shared gate of six model boundaries, not a CV
    function. achievement_render hands it a one-line role title and
    enrichment hands it the achievement the customer just typed — inferring a
    "header name" there and blanking it would destroy the very content we are
    about to send, and the customer would see a soft failure they caused
    nothing of."""
    for text in (
        "مشرف عمليات فرع",
        "Branch Operations Supervisor",
        "طورت نظام المتابعة\nودربت الفريق كامل\nوصار الشغل اسرع بكثير\n"
        "وكل هذا خلال ستة اشهر من بداية المشروع",
    ):
        assert strip_pii(text, known_name=None).text == text


# ── the fail-closed backstop must be able to fail ────────────────────────────


def _redact_contacts(cv: str) -> str:
    """Email and phone already placeheld, name untouched — isolates the name
    check so a passing test cannot be the email rule firing by accident."""
    return (
        cv.replace("fulan.a@example.com", "[EMAIL_1]")
        .replace("+966 50 000 0000", "[PHONE_1]")
        .replace("٠٥٠٠٠٠٠٠٠٠", "[PHONE_1]")
    )


def test_the_backstop_catches_a_name_the_caller_never_told_us_about() -> None:
    """assert_no_pii re-applied the caller's own three rules, so it was depth
    against ONE failure mode and blind to the one that actually happened: a
    caller passing the wrong known_name (or None). It now reads the document
    itself, which is the source the caller failed to consult."""
    # Contacts already redacted, so ONLY the name check can raise here — the
    # old backstop would have waved all three through.
    with pytest.raises(PiiLeak):
        assert_no_pii(_redact_contacts(CV_VARIANT_SPELLING), known_name=None)
    with pytest.raises(PiiLeak):
        assert_no_pii(_redact_contacts(CV_ARABIC_ONLY), known_name=None)
    with pytest.raises(PiiLeak):
        # the exact shape of the real defect: the Latin half of a bilingual
        # header was stripped, its Arabic twin was not
        assert_no_pii(
            _redact_contacts(CV_BILINGUAL).replace("Fulan Alfulani", "[NAME] [NAME]"),
            known_name="Fulan Alfulani",
        )


def test_the_backstop_passes_text_it_has_no_business_blocking() -> None:
    """The other side of the trade-off, and the reason the backstop is
    precision-first: a false PiiLeak is not a near miss. enrichment.py turns
    it into a soft_fail (the customer's achievement is silently dropped),
    intent.py falls back to deterministic parsing, and the tailoring chain
    loses a model call on a CV a customer has paid for."""
    assert_no_pii(strip_pii(CV_BILINGUAL, known_name=_TYPED_NAME).text,
                  known_name=_TYPED_NAME)
    assert_no_pii(strip_pii(CV_ARABIC_ONLY, known_name=None).text,
                  known_name=None)
    # a role title off a ProfileFact payload (achievement_render.write)
    assert_no_pii("مشرف عمليات فرع", known_name=None)
    assert_no_pii("Branch Operations Supervisor", known_name=None)
    # a multi-line colloquial answer (enrichment.handle_answer)
    assert_no_pii(
        "طورت نظام المتابعة\nودربت الفريق كامل\nوصار الشغل اسرع بكثير\n"
        "وكل هذا خلال ستة اشهر فقط من بداية المشروع في الفرع الرئيسي",
        known_name=None,
    )
    # a ranking prompt with a job description in it (PiiGuardedLlm)
    assert_no_pii(
        "You are ranking CV experience entries for a specific job.\n\n"
        "Return ONLY valid JSON with this exact shape:\n{\n"
        '  "experience_order": [0, 1, 2]\n}\n\n'
        "Target job title: Business Analyst\nTarget company: Example Trading\n\n"
        "Job description:\nRiyadh based role supporting branch operations.\n\n"
        "Experience entries:\nBranch operations supervisor\n",
        known_name=None,
    )
