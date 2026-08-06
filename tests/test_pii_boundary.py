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


# ── the headings an ordinary Saudi CV actually prints (audit 2026-08-05) ─────
#
# The section-word list was a list of ONE-word Arabic headings, and «الخبرة
# العملية» / «المؤهل الدراسي» — what people really type — matched none of
# them. The CV was not recognised as a document, no header name was inferred,
# the name went to the model verbatim, and assert_no_pii agreed it was fine
# because it asked the SAME predicate. Nothing here is adversarial: it is the
# most ordinary Arabic CV in the country.

CV_ORDINARY_ARABIC_HEADINGS = """فلان الفلاني
محلل أعمال

الخبرة العملية
مشرف عمليات فرع في شركة المثال التجارية (2019 - 2023)

المؤهل الدراسي
بكالوريوس نظم معلومات
"""


def test_the_headings_an_ordinary_arabic_cv_prints_are_recognised() -> None:
    """«الخبرة العملية» and «المؤهل الدراسي» are not a layout choice — they
    are what a Saudi CV says. The name rode to the provider unredacted."""
    assert infer_header_name(CV_ORDINARY_ARABIC_HEADINGS) is not None
    safe = strip_pii(CV_ORDINARY_ARABIC_HEADINGS, known_name=None).text

    assert "فلان" not in safe
    assert "الفلاني" not in safe
    assert "[NAME]" in safe
    assert "مشرف عمليات فرع" in safe          # the work content survives
    assert "الخبرة العملية" in safe            # and so does the heading


def test_the_backstop_does_not_share_the_strippers_blind_spot() -> None:
    """The structural defect under the leak above: assert_no_pii gated its
    residual-name check on ``_looks_like_cv`` — the stripper's own predicate —
    so the only thing standing behind the stripper was a copy of the
    stripper's assumption. It could not, by construction, catch a layout the
    stripper did not recognise.

    This CV runs its headings INLINE, so the heading test does not fire and
    the stripper genuinely misses the name. The backstop must still refuse it,
    from evidence the heading vocabulary knows nothing about."""
    import career.onboarding.extraction as extraction

    headingless = """Fulan Alfulani
[EMAIL_1] | [PHONE_1]
Branch operations supervisor, Example Trading — 2019 to 2023
Business analyst, Example Bank — 2015 to 2019
B.Sc. Information Systems, 2015
"""
    assert not extraction._looks_like_cv(headingless), (
        "the fixture must be a layout the STRIPPER misses, or this proves "
        "nothing about the backstop being independent of it"
    )
    with pytest.raises(PiiLeak):
        assert_no_pii(headingless, known_name=None)


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


def test_an_answer_full_of_career_words_is_not_shredded_into_placeholders(
) -> None:
    """AUDIT 2026-08-05. The fixture above passes for the wrong reason — it
    happens to contain no section words. Put the ordinary nouns of a career
    back in and the old rule («two section WORDS anywhere, four lines») fired
    on a customer's own enrichment answer: its short lines then read as header
    names and «وحققت نتائج ممتازة» went to the model as «[NAME] [NAME]
    [NAME]». assert_no_pii raises nothing at that — the text is corrupted, not
    leaked — so the model saw garbage and the achievement bank stored it.

    A heading is a LINE, not a word: a CV prints «الخبرة العملية» on a line of
    its own, and a person describing their work puts those words in a
    sentence."""
    answers = (
        "قدت فريق التعليم والتدريب\nوطورت المهارات الرقمية\n"
        "وحققت نتائج ممتازة\nخلال سنة واحدة\nمع فريق صغير",
        "اشتغلت على المشاريع الكبيرة\nودربت الفريق على المهارات الجديدة\n"
        "وحققت نتائج ممتازة\nفي وقت قصير\nبدون تأخير",
        "حدثت الملف الشخصي للعملاء\nوجمعت البيانات من كل الفروع\n"
        "ورفعت جودة الخدمة\nخلال ستة اشهر\nبدون اي تأخير",
    )
    for answer in answers:
        stripped = strip_pii(answer, known_name=None)
        assert stripped.text == answer, "the customer's own words came back changed"
        assert "[NAME]" not in stripped.text
        assert_no_pii(stripped.text, known_name=None)   # and it is not a leak


# ── the backstop turned on the people it protects (audit 2026-08-06) ────────
#
# _document_shaped accepted ">=2 distinct years" and ">=3 bullet lines" as
# document evidence ON THEIR OWN, gated on nothing but four non-blank lines —
# and a person describing their career on WhatsApp writes exactly that.
# _residual_name then read whole Arabic verb-phrases as person-shaped. Both
# answers below raised PiiLeak, and enrichment.handle_answer turns any
# exception into a soft_fail: the achievement the customer had just typed was
# dropped without a word, which is the one asymmetry assert_no_pii's own
# docstring says must not happen. bullet_panel.py raises on the same text and
# funnel/flow.py answers a document upload with «تعذر قراءة الملف».

ANSWER_WITH_A_DATE_RANGE = (
    "عملت في شركة الاتصالات\nمن 2018 إلى 2022\n"
    "وحققت زيادة في المبيعات\nثم انتقلت لجهة ثانية"
)
ANSWER_IN_BULLETS = (
    "- درست العملاء المتوقعين\n- طورت خطة التواصل\n"
    "- حققت نتائج ممتازة\n- ورفعت الرضا"
)
#: Bullets AND dates at once — document-shaped ON PURPOSE, so nothing about
#: this text is protected by the evidence rule. It isolates the second half of
#: the fix: a verb phrase is not a name.
ANSWER_BULLETED_WITH_DATES = (
    "- عملت في شركة الاتصالات من 2018 إلى 2022\n- طورت خطة التواصل\n"
    "- حققت نتائج ممتازة\n- ورفعت رضا العملاء"
)


def test_an_ordinary_typed_answer_is_not_a_document() -> None:
    """Two years is a date range; four dashes is a WhatsApp list. Neither is
    evidence of a FILE, and treating them as such put the backstop in the way
    of the customer it exists to protect."""
    import career.onboarding.extraction as extraction

    for answer in (ANSWER_WITH_A_DATE_RANGE, ANSWER_IN_BULLETS):
        assert not extraction._document_shaped(answer)
        assert strip_pii(answer, known_name=None).text == answer
        assert_no_pii(answer, known_name=None)


def test_an_arabic_verb_phrase_is_never_read_as_a_name() -> None:
    """The other half, isolated. «عملت في شركة الاتصالات» and «طورت خطة
    التواصل» are two-to-four alphabetic words with no digits and no
    punctuation — precisely the shape the header heuristic calls a name. This
    fixture is document-shaped on purpose, so the evidence rule protects
    nothing here and only the sentence test can keep it clean."""
    import career.onboarding.extraction as extraction

    assert extraction._document_shaped(ANSWER_BULLETED_WITH_DATES), (
        "the fixture must be a document, or it proves nothing about the "
        "residual-name half"
    )
    assert extraction._residual_name(ANSWER_BULLETED_WITH_DATES) is None
    assert_no_pii(ANSWER_BULLETED_WITH_DATES, known_name=None)


#: A CV with no recognisable headings and no contact details left in it: the
#: weak signals are all that remain, which is why they still admit a document
#: TOGETHER. Removing them outright would have traded one failure for its
#: opposite.
CV_LAYOUT_ONLY = """فلان الفلاني
مشرف عمليات فرع في شركة المثال التجارية
- قاد برنامج جودة الخدمة في أربعة فروع
- رفع رضا العملاء خلال 2019
- خفض زمن الانتظار في 2022
"""


def test_the_backstop_still_refuses_the_documents_it_was_built_for() -> None:
    """The counterweight to both halves at once, and the properties the fix
    was not allowed to spend:

    * the ordinary Arabic headings «الخبرة العملية» / «المؤهل الدراسي» still
      make a document, and its header name is still stripped and still
      refused if it survives;
    * a layout with no headings and no contacts left — nothing but dates and
      bullets — is still a document, because together they are a file;
    * «فلان الفلاني» is still person-shaped through the «اسم + اللقب» article
      pattern, with no given name in any list.
    """
    import career.onboarding.extraction as extraction

    safe = strip_pii(CV_ORDINARY_ARABIC_HEADINGS, known_name=None).text
    assert "فلان" not in safe and "الفلاني" not in safe
    with pytest.raises(PiiLeak):                       # if it ever survived
        assert_no_pii(CV_ORDINARY_ARABIC_HEADINGS, known_name=None)

    assert not extraction._looks_like_cv(CV_LAYOUT_ONLY), (
        "the fixture must reach _document_shaped through the weak pair, or "
        "it proves nothing about them still counting"
    )
    assert extraction._document_shaped(CV_LAYOUT_ONLY)
    with pytest.raises(PiiLeak):
        assert_no_pii(CV_LAYOUT_ONLY, known_name=None)


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
