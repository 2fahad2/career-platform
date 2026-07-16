"""The CV-analysis evaluation — whitepaper §04, deterministic and Arabic.

Five numeric scores + the top-5 notes + the requested-vs-alternative
comparison + three next steps. No LLM, no network: the same facts always
produce the same report (variants-over-generation discipline), the path
scoring is the SAME authority the subscription uses (C5.8 — one truth), and
the notes are rule-based Arabic data with priorities — genuinely useful,
never filler. Works on EXTRACTED facts by design: this cheap product
evaluates the uploaded CV as-is; the confirmation loop belongs to the
subscription.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from career.db.models import ProfileFact
from career.onboarding.paths import (
    DEFAULT_FAMILIES,
    PathFamily,
    resolve_requested,
    score_path,
)

#: overall = weighted blend — the weights are part of the product contract.
_WEIGHTS = {"fit": 0.30, "clarity": 0.20, "achievements": 0.25,
            "readability": 0.25}

_NUMBER_RE = re.compile(r"\d")


@dataclass(frozen=True)
class PathVerdict:
    key: str
    label_ar: str
    score: int


@dataclass(frozen=True)
class Report:
    overall: int
    clarity: int
    achievements: int
    fit: int
    readability: int
    notes: tuple[str, ...]
    requested: PathVerdict
    alternative: PathVerdict | None
    next_steps: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """Plain-JSON shape (tuples → lists) — what the DB row stores."""
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        payload["next_steps"] = list(self.next_steps)
        return payload


def _experiences(facts: list[ProfileFact]) -> list[dict[str, Any]]:
    return [dict(f.payload or {}) for f in facts if f.category == "experience"]


def _achievement_texts(facts: list[ProfileFact]) -> list[str]:
    texts: list[str] = []
    for exp in _experiences(facts):
        texts.extend(str(a) for a in exp.get("achievements") or [])
    for f in facts:
        if f.category == "achievement":
            text = str((f.payload or {}).get("text") or "")
            if text:
                texts.append(text)
    return texts


def _clarity_score(facts: list[ProfileFact], fam: PathFamily | None) -> int:
    """How focused the history is on the requested path."""
    titles = [str(e.get("title") or "") for e in _experiences(facts)]
    titles = [t for t in titles if t]
    if not titles or fam is None:
        return 20 if titles else 0
    tokens = fam.title_tokens
    matched = sum(
        1 for t in titles if any(tok in t.lower() for tok in tokens)
    )
    base = round(70 * matched / len(titles))
    bonus = 30 if matched and titles and any(
        tok in titles[0].lower() for tok in tokens
    ) else 0                                # the MOST RECENT role matches
    return min(100, base + bonus)


def _achievements_score(facts: list[ProfileFact]) -> int:
    texts = _achievement_texts(facts)
    if not texts:
        return 0
    count_pts = min(50, len(texts) * 10)             # up to 5 achievements
    measurable = sum(1 for t in texts if _NUMBER_RE.search(t))
    measurable_pts = round(50 * measurable / len(texts))
    return min(100, count_pts + measurable_pts)


def _readability_score(facts: list[ProfileFact], contact_found: bool) -> int:
    """Machine readability — did structured extraction actually succeed?"""
    experiences = _experiences(facts)
    score = 0
    if contact_found:
        score += 20
    if experiences:
        score += 20
        dated = sum(1 for e in experiences if e.get("start_date"))
        score += round(20 * dated / len(experiences))
    if any(f.category == "education" for f in facts):
        score += 15
    skills = sum(1 for f in facts if f.category == "skill")
    score += min(15, skills * 3)
    if any(f.category == "certification" for f in facts):
        score += 10
    return min(100, score)


# ── the top-5 notes: (priority, predicate) → Arabic advice ───────────────────


def _notes(
    facts: list[ProfileFact], *, contact_found: bool, fit: int, clarity: int
) -> tuple[str, ...]:
    texts = _achievement_texts(facts)
    measurable = sum(1 for t in texts if _NUMBER_RE.search(t))
    experiences = _experiences(facts)
    undated = [e for e in experiences if not e.get("start_date")]
    skills = sum(1 for f in facts if f.category == "skill")
    certs = any(f.category == "certification" for f in facts)

    candidates: list[tuple[int, bool, str]] = [
        (1, not texts,
         "سيرتك تخلو من إنجازات واضحة — أضف لكل وظيفة إنجازين على الأقل "
         "بصيغة نتيجة، لا مهام يومية."),
        (2, bool(texts) and measurable < max(1, len(texts) // 2),
         "قوِّ إنجازاتك بأرقام قابلة للقياس (نسبة تحسّن، عدد مشاريع، حجم "
         "أثر) — الأرقام هي ما يوقف عين المسؤول."),
        (3, bool(undated),
         "بعض خبراتك بلا تواريخ واضحة — أنظمة التوظيف الآلية ترفض أو تتجاهل "
         "الخبرات غير المؤرخة، اكتبها بصيغة شهر/سنة."),
        (4, not contact_found,
         "بيانات التواصل لم تُقرأ آليًا من ملفك — ضع البريد والجوال نصًا "
         "صريحًا أعلى الصفحة، لا داخل صورة أو ترويسة."),
        (5, skills < 8,
         "قسم المهارات قصير — أضف المهارات والأدوات التي تجيدها فعلًا حتى "
         "تلتقطها أنظمة الفرز بالكلمات المفتاحية."),
        (6, clarity < 50,
         "مسارك يبدو متشتتًا بين أدوار مختلفة — قدّم الخبرات الأقرب لمسارك "
         "المستهدف ولخّص ما عداها."),
        (7, not certs,
         "لا توجد شهادات مهنية — شهادة واحدة معتمدة في مجالك ترفع موثوقية "
         "ملفك بوضوح."),
        (8, fit < 60,
         "الملاءمة مع مسارك المطلوب متوسطة — أبرز في أعلى السيرة الخبرات "
         "والمهارات التي تخدم هذا المسار تحديدًا."),
        (9, bool(texts) and measurable >= max(1, len(texts) // 2),
         "إنجازاتك المرقّمة نقطة قوة حقيقية — حافظ عليها وقدّمها في أول "
         "سطرين من كل وظيفة."),
    ]
    picked = [note for _, hit, note in sorted(candidates) if hit]
    return tuple(picked[:5])


def evaluate(
    facts: list[ProfileFact], *, requested_path: str, contact_found: bool
) -> Report:
    fam = resolve_requested(requested_path)
    requested_key = fam.key if fam else requested_path
    requested_label = fam.label_ar if fam else requested_path

    fit = score_path(fam, facts).score if fam else 0
    clarity = _clarity_score(facts, fam)
    achievements = _achievements_score(facts)
    readability = _readability_score(facts, contact_found)
    overall = round(
        fit * _WEIGHTS["fit"] + clarity * _WEIGHTS["clarity"]
        + achievements * _WEIGHTS["achievements"]
        + readability * _WEIGHTS["readability"]
    )

    scored = sorted(
        (score_path(f, facts) for f in DEFAULT_FAMILIES
         if fam is None or f.key != fam.key),
        key=lambda s: -s.score,
    )
    labels = {f.key: f.label_ar for f in DEFAULT_FAMILIES}
    alternative = (
        PathVerdict(scored[0].path, labels.get(scored[0].path, scored[0].path),
                    scored[0].score)
        if scored else None
    )

    steps: list[str] = []
    if alternative and alternative.score > fit + 15:
        steps.append(
            f"فكّر جديًا بمسار {alternative.label_ar} كبديل — ملفك الحالي "
            f"يدعمه بدرجة {alternative.score} مقابل {fit} لمسارك المطلوب."
        )
    elif fit >= 60:
        steps.append(
            f"استمر في مسار {requested_label} — ملفك يدعمه بوضوح، "
            "وركّز على معالجة الملاحظات أعلاه."
        )
    else:
        steps.append(
            f"مسار {requested_label} ممكن لكنه يحتاج تقوية — عالج الفجوات "
            "في الملاحظات قبل التقديم المكثف."
        )
    steps.append("حدّث سيرتك بالملاحظات ثم أعد قياسها — التحسين يتراكم.")
    steps.append(
        "الاشتراك في البحث اليومي: نبحث عنك كل ليلة، نرشّح بدقة، ونجهّز لك "
        "CV مخصصًا لكل فرصة."
    )

    return Report(
        overall=overall, clarity=clarity, achievements=achievements,
        fit=fit, readability=readability,
        notes=_notes(facts, contact_found=contact_found, fit=fit,
                     clarity=clarity),
        requested=PathVerdict(requested_key, requested_label, fit),
        alternative=alternative,
        next_steps=tuple(steps),
    )


__all__ = ["PathVerdict", "Report", "evaluate"]
