"""Basic-data collection — buttons and lists as DATA (whitepaper §05).

The ten documented fields (the relocation+remote bullet is two answers), asked
in order with per-field validation and Arabic re-prompts. Never asks for a
national id or a date of birth — such questions do not exist here, and the
schema has no columns for them (defence in depth).

Notable rules:
- ``cv_full_name`` must be Latin script: the CV is English-only and the
  Arabic-leak guard (LEGACY §1.9) would reject Arabic on the PDF — better to
  collect it right than to fail later.
- ``expected_salary_sar`` is a soft per-tenant target (D4): skippable, parsed
  from Western or Arabic-Indic digits, sanity-bounded — never a hard gate.
- The cursor is presence-based (:func:`next_question` returns the first
  unanswered question), so the flow is resumable from persisted answers.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import CustomerProfile

#: Sentinel the channel layer sends when the customer taps "تخطّي".
SKIP = "__skip__"

_ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

_SALARY_MIN = Decimal("1000")
_SALARY_MAX = Decimal("500000")


class UnknownQuestion(Exception):
    """A question key that is not part of the documented flow."""


class AnswerInvalid(Exception):
    """The raw answer failed validation; ``reprompt_ar`` re-asks in Arabic."""

    def __init__(self, reprompt_ar: str) -> None:
        super().__init__(reprompt_ar)
        self.reprompt_ar = reprompt_ar


@dataclass(frozen=True)
class Option:
    id: str
    label_ar: str
    value: Any


@dataclass(frozen=True)
class Question:
    key: str
    prompt_ar: str
    kind: str  # text | buttons | list
    options: tuple[Option, ...] = ()
    skippable: bool = False


QUESTIONS: tuple[Question, ...] = (
    Question(
        key="cv_full_name",
        prompt_ar=(
            "وش الاسم اللي تبيه يظهر على سيرتك الذاتية؟ "
            "(بالأحرف الإنجليزية — السيرة تُكتب بالإنجليزية)"
        ),
        kind="text",
    ),
    Question(
        key="email",
        prompt_ar=(
            "وش بريدك الإلكتروني؟ (يظهر في سيرتك الذاتية "
            "ويتواصل عليه أصحاب العمل)"
        ),
        kind="text",
    ),
    Question(
        key="linkedin_url",
        prompt_ar=(
            "رابط حسابك في LinkedIn؟ (يظهر على سيرتك — "
            "أرسل الرابط أو اسم المستخدم، أو تخطَّ إن ما عندك حساب)"
        ),
        kind="text",
        skippable=True,
    ),
    Question(
        key="city",
        prompt_ar="في أي مدينة تسكن؟",
        kind="list",
        options=(
            Option("riyadh", "الرياض", "الرياض"),
            Option("jeddah", "جدة", "جدة"),
            Option("dammam", "الدمام / الخبر / الظهران", "الدمام"),
            Option("makkah", "مكة المكرمة", "مكة المكرمة"),
            Option("madinah", "المدينة المنورة", "المدينة المنورة"),
            Option("other", "مدينة أخرى (اكتبها)", None),
        ),
    ),
    Question(
        key="region",
        prompt_ar=(
            "وش منطقتك الإدارية؟ (تساعدنا نطابق الإعلانات اللي تذكر "
            "المنطقة بدل المدينة)"
        ),
        kind="list",
        options=(
            Option("riyadh_region", "منطقة الرياض", "الرياض"),
            Option("makkah_region", "منطقة مكة المكرمة", "مكة المكرمة"),
            Option("eastern", "المنطقة الشرقية", "الشرقية"),
            Option("madinah_region", "منطقة المدينة المنورة", "المدينة المنورة"),
            Option("qassim", "منطقة القصيم", "القصيم"),
            Option("asir", "منطقة عسير", "عسير"),
            Option("tabuk", "منطقة تبوك", "تبوك"),
            Option("hail", "منطقة حائل", "حائل"),
            Option("jazan", "منطقة جازان", "جازان"),
            Option("other", "منطقة أخرى (اكتبها)", None),
        ),
    ),
    Question(
        key="current_title",
        prompt_ar="وش مسماك الوظيفي الحالي (أو آخر مسمى)؟",
        kind="text",
    ),
    Question(
        key="years_experience",
        prompt_ar="كم سنة خبرة عندك؟ (رقم فقط)",
        kind="text",
    ),
    Question(
        key="notice_period_days",
        prompt_ar="كم فترة الإشعار المطلوبة في عملك الحالي؟",
        kind="buttons",
        options=(
            Option("immediate", "أقدر أبدأ فورًا", 0),
            Option("two_weeks", "أسبوعان", 14),
            Option("one_month", "شهر", 30),
            Option("two_months", "شهران", 60),
            Option("three_months", "ثلاثة أشهر", 90),
        ),
    ),
    Question(
        key="employment_type",
        prompt_ar="وش نوع الدوام اللي تبحث عنه؟",
        kind="buttons",
        options=(
            Option("full_time", "دوام كامل", "full_time"),
            Option("part_time", "دوام جزئي", "part_time"),
            Option("any", "أي منهما", "any"),
        ),
    ),
    Question(
        key="willing_to_relocate",
        prompt_ar="هل أنت مستعد للانتقال لمدينة ثانية لو كانت الفرصة مناسبة؟",
        kind="buttons",
        options=(
            Option("yes", "نعم", True),
            Option("no", "لا", False),
        ),
    ),
    Question(
        key="remote_preference",
        prompt_ar="وش تفضيلك لمكان العمل؟",
        kind="buttons",
        options=(
            Option("onsite", "حضوري", "onsite"),
            Option("remote", "عن بعد", "remote"),
            Option("hybrid", "هجين", "hybrid"),
            Option("any", "الكل يناسبني", "any"),
        ),
    ),
    Question(
        key="expected_salary_sar",
        prompt_ar=(
            "وش الراتب الشهري اللي تستهدفه بالريال؟ (رقم تقريبي — يساعدنا نرتب "
            "الفرص، وما نحجب عنك وظيفة ما أعلنت راتبها). تقدر تتخطى السؤال."
        ),
        kind="text",
        skippable=True,
    ),
    Question(
        key="communication_language",
        prompt_ar="وش لغة التواصل اللي تفضلها للرسائل؟",
        kind="buttons",
        options=(
            Option("ar", "العربية", "ar"),
            Option("en", "English", "en"),
        ),
    ),
    Question(
        key="requested_path",
        prompt_ar="وش المسار الوظيفي اللي تبينا نبحث لك فيه؟ (مثال: محلل أعمال)",
        kind="text",
    ),
)

_BY_KEY: dict[str, Question] = {q.key: q for q in QUESTIONS}


def question(key: str) -> Question:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise UnknownQuestion(f"unknown collection question: {key!r}") from None


def next_question(answers: dict[str, Any]) -> Question | None:
    """The first unanswered question, in documented order. Presence drives the
    cursor (a skipped answer is present as None), so the flow resumes from
    whatever was persisted."""
    for q in QUESTIONS:
        if q.key not in answers:
            return q
    return None


def is_complete(answers: dict[str, Any]) -> bool:
    return next_question(answers) is None


# ── per-field parsing (raw channel text/button id → typed value) ─────────────


def _parse_int(raw: str) -> int | None:
    normalized = raw.strip().translate(_ARABIC_INDIC_DIGITS)
    return int(normalized) if re.fullmatch(r"\d+", normalized) else None


def _option_value(q: Question, raw: str) -> Any:
    for opt in q.options:
        if raw.strip() == opt.id:
            return opt.value
    return _MISSING


_MISSING = object()


def parse_answer(key: str, raw: str) -> Any:
    """Parse and validate one raw answer. Returns the typed value, or raises
    :class:`AnswerInvalid` carrying the Arabic re-prompt."""
    q = question(key)
    text = raw.strip()

    if q.skippable and text == SKIP:
        return None

    if key == "cv_full_name":
        if _ARABIC_SCRIPT.search(text):
            raise AnswerInvalid(
                "السيرة الذاتية تُكتب بالإنجليزية — اكتب اسمك بالأحرف الإنجليزية "
                "من فضلك (مثال: Fahad Almulhim)."
            )
        if not (2 <= len(text) <= 128) or not re.search(r"[A-Za-z]", text):
            raise AnswerInvalid("اكتب اسمك الكامل بالأحرف الإنجليزية من فضلك.")
        return text

    if key == "email":
        candidate = text.lower()
        if _ARABIC_SCRIPT.search(candidate) or not re.fullmatch(
            r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", candidate
        ):
            raise AnswerInvalid(
                "اكتب بريدًا إلكترونيًا صحيحًا بالأحرف الإنجليزية "
                "(مثال: fahad@example.com)."
            )
        return candidate

    if key == "linkedin_url":
        candidate = text.strip().rstrip("/")
        if _ARABIC_SCRIPT.search(candidate) or len(candidate) > 200:
            raise AnswerInvalid(
                "أرسل رابط حسابك في LinkedIn (مثال: linkedin.com/in/fahad) "
                "أو اضغط «تخطّي»."
            )
        match = re.search(
            r"(?:^|/)in/([A-Za-z0-9\-_.%]{2,100})$", candidate
        ) or (
            re.fullmatch(r"[A-Za-z0-9\-_.%]{2,100}", candidate)
            if "/" not in candidate and "." not in candidate
            else None
        )
        if not match:
            raise AnswerInvalid(
                "الرابط غير واضح — أرسل رابط ملفك الشخصي "
                "(مثال: linkedin.com/in/fahad) أو اسم المستخدم فقط، "
                "أو اضغط «تخطّي»."
            )
        handle = match.group(1) if match.groups() else match.group(0)
        return f"linkedin.com/in/{handle}"

    if key == "city":
        chosen = _option_value(q, text)
        if chosen is not _MISSING and chosen is not None:
            return chosen
        if 2 <= len(text) <= 64 and text != "other":
            return text  # free-text city ("أخرى")
        raise AnswerInvalid("اختر مدينتك من القائمة أو اكتب اسمها.")

    if key == "region":
        chosen = _option_value(q, text)
        if chosen is not _MISSING and chosen is not None:
            return chosen
        if 2 <= len(text) <= 64 and text != "other":
            return text  # free-text region ("أخرى")
        raise AnswerInvalid("اختر منطقتك من القائمة أو اكتب اسمها.")

    if key == "current_title":
        if not (2 <= len(text) <= 128):
            raise AnswerInvalid("اكتب مسماك الوظيفي من فضلك.")
        return text

    if key == "years_experience":
        years = _parse_int(text)
        if years is None or not (0 <= years <= 50):
            raise AnswerInvalid("اكتب عدد سنوات الخبرة كرقم بين 0 و50 (مثال: 7).")
        return years

    if key == "expected_salary_sar":
        normalized = text.translate(_ARABIC_INDIC_DIGITS).replace(",", "").replace(" ", "")
        if not re.fullmatch(r"\d+", normalized):
            raise AnswerInvalid(
                "اكتب الراتب المستهدف كرقم بالريال (مثال: 12000)، "
                "أو اضغط «تخطّي»."
            )
        value = Decimal(normalized)
        if not (_SALARY_MIN <= value <= _SALARY_MAX):
            raise AnswerInvalid(
                "الرقم غير منطقي كراتب شهري بالريال — اكتب رقمًا مثل 12000، "
                "أو اضغط «تخطّي»."
            )
        return value

    if key == "requested_path":
        if not (2 <= len(text) <= 64):
            raise AnswerInvalid("اكتب المسار الوظيفي اللي تبيه (مثال: محلل أعمال).")
        return text

    # Remaining questions are pure option picks.
    chosen = _option_value(q, text)
    if chosen is _MISSING:
        labels = " / ".join(opt.label_ar for opt in q.options)
        raise AnswerInvalid(f"اختر أحد الخيارات: {labels}")
    return chosen


# ── persistence: one customer_profiles row per tenant, filled progressively ──

_PROFILE_COLUMNS = frozenset(q.key for q in QUESTIONS)


def apply_answer(
    session: Session, *, tenant_id: uuid.UUID, key: str, value: Any
) -> CustomerProfile:
    """Upsert the tenant's single profile row with one parsed answer."""
    if key not in _PROFILE_COLUMNS:
        raise UnknownQuestion(f"unknown collection question: {key!r}")
    profile = session.execute(
        select(CustomerProfile).where(CustomerProfile.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if profile is None:
        profile = CustomerProfile(id=uuid.uuid4(), tenant_id=tenant_id)
        session.add(profile)
    setattr(profile, key, value)
    profile.updated_at = func.now()
    session.flush()
    return profile
