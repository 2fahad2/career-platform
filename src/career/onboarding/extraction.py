"""CV extraction via Claude — identity-stripped, fail-closed (whitepaper §05).

Order of defenses:
1. Consent gate (§12) — nothing runs before the required consents exist.
2. PII stripping (§15.8) — the customer's name, phone numbers (Western or
   Arabic-Indic digits) and emails are replaced with placeholders BEFORE any
   external call. The name comes from what the customer typed AND from what
   the CV prints at its top, matched on folded fuzzy tokens, because a Saudi
   name has no single spelling (audit 2026-08-05). A validator then proves
   the stripped text is clean; a leak raises instead of sending.
3. The extractor boundary is an injectable Protocol — tests use fakes with no
   network; AnthropicExtractor is the real implementation (Claude only — D1).
4. Extraction output is NOT truth (§15.5): facts are stored as EXTRACTED rows
   and enter the achievement bank only after customer confirmation (C5.7).

Degradation (D1): there is no silver rule-based fallback that could honestly
replace LLM extraction — on failure we raise ExtractionFailed and the flow
keeps the customer in CV_PROCESSING's documented failure path instead of
pretending. Retries for transient API errors are the SDK's built-in ones.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from career.arabic import AR_DIACRITICS, AR_MARKS, fold_token
from career.cv.close import LlmMeter, TokenCounter
from career.db.models import ProfileFact
from career.onboarding.consents import require_required_consents

logger = logging.getLogger("career.onboarding")

#: Budget guard: reject absurdly large inputs before any token is spent.
#: A 15-page CV extracts to well under this; bigger means something is wrong.
MAX_INPUT_CHARS = 60_000

_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 16000

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Phone-ish runs in Western or Arabic-Indic digits with common separators.
# Only replaced/flagged when the run carries >= 9 digits, so years and date
# ranges ("2019 - 2023") survive.
_PHONE = re.compile(r"\+?[0-9٠-٩۰-۹][0-9٠-٩۰-۹\s\-()]{6,}[0-9٠-٩۰-۹]")
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]")

#: Name particles this short ("Al", "bin") are too ambiguous to strip/flag —
#: blanking them would eat unrelated words like "Al Falak Systems".
_MIN_NAME_TOKEN = 3

#: The placeholder every name hit collapses to. Downstream contracts depend on
#: the exact shapes: funnel/flow.py counts ``[EMAIL_`` keys to decide whether
#: the CV carried contact details, _SYSTEM tells the model to ignore [NAME],
#: and tests pin "[NAME]" in the stripped text. Never reshape these.
_NAME = "[NAME]"

# ── name matching (audit 2026-08-05) ─────────────────────────────────────────
#
# What went wrong: name stripping was a literal case-insensitive substring
# match on ``CustomerProfile.cv_full_name``, a field collection.py forces to
# be LATIN. So the guarantee «اسمك لا يُرسل إلى أي نموذج» was only ever as
# good as one exact Latin string, and three ordinary Saudi CVs broke it —
# a bilingual header whose Arabic half matched nothing, a CV that spells the
# same name differently from what the customer typed on WhatsApp, and a
# missing profile row, which stripped no name at all and made assert_no_pii
# silently assert nothing. The 29-SAR analysis funnel was BETTER protected,
# because it never has a typed name and therefore had to derive one from the
# CV header. That asymmetry is what this section removes: the CV itself is
# now a name source everywhere, and comparison happens on folded, fuzzy
# tokens instead of raw bytes.
#
# Direction of error, deliberately: over-stripping costs one redacted word in
# a prompt; under-stripping costs the promise printed on the store page.

# The marks (AR_MARKS/AR_DIACRITICS) and the fold (fold_token) come from
# career.arabic — the leaf that imports nothing from career at all.
#
# They used to be COPIED here, with a comment explaining why: the fold lived in
# achievement_render, which reaches into career.cv.generate at module level,
# and this module is itself the leaf every model boundary imports (cv.generate,
# intent, enrichment, bullet_panel and achievement_render all import IT), so
# importing upwards would have closed a cycle around the one function that must
# never fail to load. The cycle is gone because the fold moved DOWN instead,
# and the copy went with it: two copies of the primitive that decides whether
# «اوافق» is a consent is a divergence waiting to happen, and only one of them
# would ever get the fix.

#: A word = letters plus Arabic marks, joined by an internal hyphen or
#: apostrophe, so "Al-Fulani" and "O'Hara" stay ONE token and «فُلان» is not
#: split at its damma (a mark is not \w, which is exactly how it used to
#: escape). Placeholders are matched first in _TOKEN so a name that happens to
#: skeletonise like "EMAIL" can never chew a placeholder apart.
_LETTER = f"(?:[^\\W\\d_]|[{AR_MARKS}])"
_WORD = re.compile(f"{_LETTER}+(?:['’-]{_LETTER}+)*")
_PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9_]*\]")
_TOKEN = re.compile(f"{_PLACEHOLDER.pattern}|{_WORD.pattern}")

#: Bilingual headers put both halves on one line: «Fulan Alfulani / فلان
#: الفلاني». Only these separators split a header line — a comma still
#: disqualifies the whole line, which is what keeps "Senior Analyst, Riyadh"
#: from being read as a name.
_HEADER_SPLIT = re.compile(r"\s*[/|•·]\s*|\s+[—–-]\s+")
_HEADER_EDGE = re.compile(r"^[\s*_|—–-]+|[\s*_|—–-]+$")

#: Words that sit on a header line and are NOT the person: job titles and
#: places. Filtered per TOKEN, never per line — "Fulan Alfulani Senior
#: Analyst" must still yield the two name tokens, so rejecting the whole line
#: would leak exactly the CVs that print a title next to the name.
_ROLE_WORDS: frozenset[str] = frozenset({
    "analyst", "manager", "engineer", "developer", "specialist", "consultant",
    "director", "officer", "supervisor", "technician", "accountant",
    "designer", "administrator", "coordinator", "assistant", "executive",
    "lead", "senior", "junior", "head", "chief", "intern", "trainee",
    "operations", "business", "project", "sales", "marketing", "finance",
    "support", "quality", "security", "software", "systems", "system",
    "مدير", "مهندس", "محاسب", "اخصائي", "مشرف", "فني", "مطور", "مستشار",
    "رئيس", "مساعد", "منسق", "موظف", "عمليات", "مبيعات", "تسويق",
})
_PLACE_WORDS: frozenset[str] = frozenset({
    "riyadh", "jeddah", "dammam", "khobar", "dhahran", "mecca", "makkah",
    "medina", "madinah", "taif", "abha", "jubail", "yanbu", "buraidah",
    "saudi", "arabia", "ksa", "kingdom", "dubai", "emirates", "bahrain",
    "الرياض", "جدة", "الدمام", "الخبر", "مكة", "المدينة", "السعودية",
    "المملكة", "العربية", "الشرقية",
})


#: Common Saudi given names, FOLDED. This list is a precision device for the
#: backstop only — never a recall device: nothing depends on a name being in
#: it (stripping works from the header and the typed name regardless), and a
#: name missing from it merely means the backstop stays quiet, which is the
#: failure direction assert_no_pii chooses on purpose.
_AR_GIVEN_NAMES: frozenset[str] = frozenset(fold_token(n) for n in {
    "محمد", "احمد", "فهد", "خالد", "سعد", "سلطان", "سعود", "نايف", "تركي",
    "بندر", "ماجد", "ناصر", "مشعل", "يوسف", "ابراهيم", "عمر", "علي", "حسن",
    "حسين", "صالح", "سلمان", "راشد", "طلال", "وليد", "زياد", "فيصل", "منصور",
    "مازن", "انس", "ريان", "نواف", "عادل", "بدر", "رياض", "هاني", "سامي",
    "سارة", "ساره", "نورة", "نوره", "هند", "ريم", "مها", "امل", "لمي",
    "منيرة", "منيره", "الجوهرة", "شيماء", "دانة", "دانه", "غاده", "غادة",
})



def _variants(folded: str) -> tuple[str, ...]:
    """The folded token plus the forms an Arabic proclitic hides it behind —
    «وفهد» / «الفهد» are the same name token as «فهد»."""
    forms = [folded]
    for prefix in ("ال", "و", "ف", "ب", "ل", "ك"):
        if folded.startswith(prefix) and len(folded) - len(prefix) >= _MIN_NAME_TOKEN:
            forms.append(folded[len(prefix):])
    return tuple(forms)


_VOWELS = re.compile(r"[aeiouy]")
_DOUBLED = re.compile(r"(.)\1+")


def _skeleton(folded: str) -> str:
    """Consonant skeleton of a Latin token, first letter kept, doubles
    collapsed: fahad/fahd → fhd, mohammed/mohamed/muhammad → mhmd. This is
    the whole point — Arabic names have no canonical transliteration, so the
    vowels a customer types are noise and the consonants are the name."""
    if not folded.isascii():
        return ""  # Arabic needs no skeleton: folding already normalises it
    return _DOUBLED.sub(r"\1", folded[:1] + _VOWELS.sub("", folded[1:]))


def _within_one_edit(a: str, b: str) -> bool:
    """One insertion, substitution or transposition apart. Pruned on length
    and first letter first, so this stays O(1) for almost every word of a CV
    and never turns the strip into a quadratic scan."""
    if abs(len(a) - len(b)) > 1 or a[:1] != b[:1]:
        return False
    if a == b:
        return True
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if len(a) == len(b):
        return a[i + 1:] == b[i + 1:] or (
            a[i:i + 1] == b[i + 1:i + 2]
            and a[i + 1:i + 2] == b[i:i + 1]
            and a[i + 2:] == b[i + 2:]
        )
    return a[i:] == b[i + 1:]


class _NameMatcher:
    """Does this word look like one of the customer's name tokens?

    Three rules, cheapest first: folded equality, consonant skeleton, one
    edit. Every answer is memoised per distinct word, so a 5000-word CV costs
    one fold per DISTINCT token and the name set is a handful of entries —
    linear in the document, never a scan per token per name."""

    __slots__ = ("_exact", "_skeletons", "_fuzzy", "_cache")

    def __init__(self, tokens: list[str]) -> None:
        self._exact: set[str] = set()
        self._skeletons: set[str] = set()
        self._fuzzy: set[str] = set()
        self._cache: dict[str, bool] = {}
        for token in tokens:
            folded = fold_token(token)
            if len(folded) < _MIN_NAME_TOKEN:
                continue
            for form in _variants(folded):
                self._exact.add(form)
                if len(form) >= 4:
                    skeleton = _skeleton(form)
                    if len(skeleton) >= 3:
                        self._skeletons.add(skeleton)
                if len(form) >= 6:
                    self._fuzzy.add(form)

    def __bool__(self) -> bool:
        return bool(self._exact)

    def matches(self, word: str) -> bool:
        cached = self._cache.get(word)
        if cached is None:
            cached = self._cache[word] = self._match(word)
        return cached

    def _match(self, word: str) -> bool:
        folded = fold_token(word)
        if len(folded) < _MIN_NAME_TOKEN:
            return False
        for form in _variants(folded):
            if form in self._exact:
                return True
            if len(form) >= 4:
                skeleton = _skeleton(form)
                if len(skeleton) >= 3 and skeleton in self._skeletons:
                    return True
            if len(form) >= 6 and any(
                _within_one_edit(form, known) for known in self._fuzzy
            ):
                return True
        return False


class PiiLeak(Exception):
    """Stripped text still contains PII — refuse to send it anywhere."""


class ExtractionFailed(Exception):
    """The extractor could not produce a trustworthy result."""


def _phone_hits(text: str) -> list[re.Match[str]]:
    return [m for m in _PHONE.finditer(text) if len(_DIGITS.findall(m.group())) >= 9]


def _name_tokens(known_name: str | None) -> list[str]:
    if not known_name:
        return []
    return [t for t in re.split(r"\s+", known_name.strip()) if len(t) >= _MIN_NAME_TOKEN]


@dataclass(frozen=True)
class StrippedText:
    text: str
    replacements: dict[str, str]  # placeholder -> original (never leaves the server)


#: CV section headings and role words that look like a name to a naive
#: heuristic. Kept small and English/Arabic both, because a false positive
#: only costs one redacted heading — a false negative leaks a real name.
#:
#: AUDIT 2026-08-05. The Arabic half of this list was a list of ONE-word
#: headings, and almost no Saudi CV writes one-word headings: «الخبرة
#: العملية» and «المؤهل الدراسي» are what people actually type, and neither
#: «الخبرة» nor «المؤهل» was here. A CV headed that way scored zero section
#: words, was not recognised as a document at all, and its header name went to
#: the model verbatim — against «اسمك لا يُرسل إلى أي نموذج», on a CV the
#: customer had paid us to read. Every word below is a heading word only; the
#: heading TEST (`_heading_lines`) is what keeps them from firing inside prose.
_HEADER_STOPWORDS: frozenset[str] = frozenset({
    "curriculum", "vitae", "resume", "cv", "profile", "summary", "objective",
    "contact", "experience", "education", "skills", "projects", "languages",
    "certifications", "references", "personal", "information", "details",
    "work", "professional", "employment", "history", "academic", "training",
    "courses", "qualifications", "qualification", "achievements", "awards",
    "السيرة", "الذاتية", "سيرة", "ذاتية", "الملف", "الشخصي", "نبذة",
    "الخبرات", "التعليم", "المهارات", "المشاريع", "اللغات", "الشهادات",
    "معلومات", "الاتصال", "البيانات", "الشخصية",
    "الخبرة", "العملية", "المهنية", "الوظيفية", "المؤهل", "المؤهلات",
    "الدراسي", "الدراسية", "العلمية", "العلمي", "الدورات", "التدريبية",
    "التدريب", "الهدف", "الوظيفي", "الإنجازات", "الدراسات", "المعلومات",
})
#: Words that may sit on a heading line without being a heading themselves:
#: «نبذة عني», "Summary of Qualifications". They can never make a line a
#: heading alone — a heading line still needs a word from the list above.
_HEADING_FILLER: frozenset[str] = frozenset({
    "of", "and", "the", "my", "about", "عني", "عن", "نفسي", "لي",
})


#: Folded forms of every word that may sit on a header line without being the
#: person. Folding matters: «نبذة» stored here compares as «نبذه».
_SECTION_WORDS: frozenset[str] = frozenset(fold_token(w) for w in _HEADER_STOPWORDS)
_NOT_A_NAME: frozenset[str] = _SECTION_WORDS | frozenset(
    fold_token(w) for w in (_ROLE_WORDS | _PLACE_WORDS)
)

#: How far down the page a name may hide. Past this we are reading the body.
_HEADER_LINES = 6


def _clean_header_line(raw: str) -> str:
    """One physical line, ready to be read as a header: placeholders removed
    (an already-redacted name must not look like a fresh one) and decorative
    edges trimmed."""
    return _HEADER_EDGE.sub("", _PLACEHOLDER.sub(" ", raw)).strip()


def _is_name_line(segment: str) -> bool:
    """The original conservative header test, now applied per SEGMENT so a
    bilingual line yields both halves instead of nothing."""
    line = segment.strip()
    if not (2 <= len(line.split()) <= 4) or len(line) > 60:
        return False
    if any(ch.isdigit() for ch in line) or "@" in line:
        return False
    if any(ch in line for ch in ",:;/\\()[]{}<>"):
        return False
    words = [w.strip(".").lower() for w in line.split()]
    if any(fold_token(w) in _SECTION_WORDS for w in words):
        return False
    # Arabic marks are not alphabetic to str.isalpha, so «فُلان» fails this
    # test unless the marks come off first — that alone leaked diacritised
    # names past the funnel's inference.
    return all(
        AR_DIACRITICS.sub("", w).replace("'", "").replace("-", "").isalpha()
        for w in words
    )


def _header_name_lines(text: str, *, max_lines: int, max_hits: int) -> list[str]:
    """Name candidates from the top of a document, at most ``max_hits`` LINES
    worth. Two lines is the honest ceiling: a CV puts the name on the first
    line or two, and reading further trades a name we already have for a
    redacted city or job title further down the page."""
    found: list[str] = []
    hits = 0
    for raw in text.splitlines()[:max_lines]:
        line = _clean_header_line(raw)
        if not line:
            continue
        segments = [s for s in _HEADER_SPLIT.split(line) if s and _is_name_line(s)]
        if not segments:
            continue
        found.extend(s.strip() for s in segments)
        hits += 1
        if hits >= max_hits:
            break
    return found


def infer_header_name(text: str, *, max_lines: int = 6) -> str | None:
    """The name printed at the top of a CV, or None.

    Why this exists: the funnel product (29 SAR) never asks the customer for
    their name — they just upload a CV — so ``known_name`` was None and the
    name line went to the model verbatim, while the published privacy page
    promises «اسمك ورقم جوالك لا يُرسلان إلى أي نموذج». This recovers a name
    to strip when we were never told one.

    Deliberately conservative: two-to-four alphabetic words, no digits, no
    punctuation that names do not carry, and nothing that matches a section
    heading. It may miss an unusual layout — the model still never sees an
    email or a phone, and a miss is visible, whereas a wrong redaction only
    costs a heading."""
    names = _header_name_lines(text, max_lines=max_lines, max_hits=1)
    return names[0] if names else None


#: A heading is SHORT. Past this the line is carrying content, whatever words
#: it opens with.
_MAX_HEADING_WORDS = 4
#: The smallest document we will read a header name out of.
_MIN_DOC_LINES = 4


def _is_heading_word(folded: str) -> bool:
    """A section word, or one wearing the «و» a bilingual heading joins with
    («التعليم والتدريب»)."""
    return folded in _SECTION_WORDS or (
        folded.startswith("و") and folded[1:] in _SECTION_WORDS
    )


def _heading_lines(text: str) -> set[str]:
    """The distinct section words that appear as a HEADING — a short line made
    of nothing but heading words, or the label before a colon.

    AUDIT 2026-08-05, and the whole point of the rewrite. The old test counted
    two section WORDS anywhere in the text, which is a property ordinary prose
    has: «قدت فريق التعليم والتدريب / وطورت المهارات الرقمية» is five lines of
    a customer's own enrichment answer and it scored two headings. strip_pii
    then read its short lines as header names and replaced «وحققت نتائج
    ممتازة» with «[NAME] [NAME] [NAME]» — the achievement the customer had
    just typed, destroyed on the way to the model and stored in the bank as
    garbage, with assert_no_pii raising nothing because the corruption is not
    a leak.

    A heading is a LINE, not a word. That single change separates the two
    populations completely: a CV prints «الخبرة العملية» on a line of its own,
    and a person writing about their work puts those words inside a sentence.
    """
    found: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        label = line.split(":", 1)[0] if ":" in line else line
        words = [fold_token(m.group()) for m in _WORD.finditer(label)]
        if not words or len(words) > _MAX_HEADING_WORDS:
            continue
        headings = {w for w in words if _is_heading_word(w)}
        if not headings:
            continue
        if all(_is_heading_word(w) or w in _HEADING_FILLER for w in words):
            found |= headings
    return found


def _looks_like_cv(text: str) -> bool:
    """Is this a CV-shaped DOCUMENT rather than a role title, a WhatsApp reply
    or a prompt?

    The header heuristic is only meaningful on a document, and strip_pii is
    the shared gate of six model boundaries: achievement_render passes a
    one-line role title, enrichment passes a customer's colloquial answer.
    Inferring a "header name" from those and blanking it would silently
    destroy the very content we are about to send — the achievement the
    customer just typed. Two distinct section HEADINGS plus four lines is the
    cheap, honest discriminator: every real CV has them and a WhatsApp
    message has neither.

    A layout this misses — a CV that runs its headings inline — no longer
    ends in a leak, and that is deliberate: since 2026-08-05 assert_no_pii
    decides for itself what a document is (:func:`_document_shaped`) instead
    of asking this function, so a name this misses is caught there and the
    send fails closed."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < _MIN_DOC_LINES:
        return False
    return len(_heading_lines(text)) >= 2


def _inferred_tokens(candidate: str) -> list[str]:
    """The tokens of an INFERRED header name that could be the person. The
    typed name is authoritative and never filtered; a guess is, because
    "Fulan Alfulani Senior Analyst" and "Riyadh Saudi Arabia" both survive the
    header test and only one of them is a human."""
    tokens: list[str] = []
    for match in _WORD.finditer(candidate):
        folded = fold_token(match.group())
        if len(folded) >= _MIN_NAME_TOKEN and folded not in _NOT_A_NAME:
            tokens.append(match.group())
    return tokens


def strip_pii(text: str, *, known_name: str | None) -> StrippedText:
    """Replace emails, phone numbers and the customer's name with placeholders.

    The name is taken from BOTH sources we have: what the customer typed
    (authoritative) and what the CV prints at the top of itself (the only
    source that knows the Arabic half of a bilingual header, or the spelling
    the customer's own CV uses). Matching is on folded, fuzzy tokens, so
    «الفلانى» reaches «الفلاني» and "Fahd Al-Mulhim" reaches "Fahad
    Almulhim" — the transliteration of a Saudi name is not stable and never
    was safe to compare byte-for-byte."""
    replacements: dict[str, str] = {}
    out = text

    for i, match in enumerate(_EMAIL.findall(out), start=1):
        placeholder = f"[EMAIL_{i}]"
        replacements[placeholder] = match
        out = out.replace(match, placeholder)

    for i, match in enumerate(_phone_hits(out), start=1):
        placeholder = f"[PHONE_{i}]"
        replacements[placeholder] = match.group()
        out = out.replace(match.group(), placeholder)

    typed = _name_tokens(known_name)
    inferred: list[str] = []
    header: str | None = None
    if _looks_like_cv(out):
        # Read the header AFTER the contact sweep: «Fulan Alfulani — [EMAIL_1]»
        # only becomes readable as a name once the email is a placeholder.
        for candidate in _header_name_lines(out, max_lines=_HEADER_LINES, max_hits=2):
            tokens = _inferred_tokens(candidate)
            if tokens:
                header = header or candidate
                inferred.extend(tokens)

    hit = False
    # Pass 1 — the literal substring sweep that shipped, byte-for-byte. It
    # catches a typed name glued inside another word («وفهد», "Fahadco"),
    # which a tokeniser by definition cannot see. Longest token first: with
    # "Fulan Alfulani" the short token used to eat the long one from the
    # inside and leave "Al[NAME]i" on the wire — no leak, but a mangled
    # employer the extractor then reads as a fact.
    for token in sorted(typed, key=len, reverse=True):
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        if pattern.search(out):
            hit = True
            out = pattern.sub(_NAME, out)

    # Pass 2 — ONE tokeniser pass over the document for the folded/fuzzy
    # forms of every name token we hold. Linear in the text, memoised per
    # distinct word; placeholders are consumed whole so [EMAIL_1] can never be
    # rewritten into [[NAME]_1] by a customer whose name skeletonises alike.
    matcher = _NameMatcher(typed + inferred)
    if matcher:
        def _replace(match: re.Match[str]) -> str:
            nonlocal hit
            word = match.group()
            if word.startswith("["):
                return word
            if matcher.matches(word):
                hit = True
                return _NAME
            return word

        out = _TOKEN.sub(_replace, out)

    if hit:
        replacements[_NAME] = known_name or header or ""

    return StrippedText(text=out, replacements=replacements)


def _person_shaped(tokens: list[str]) -> bool:
    """Does this run of words look like a PERSON, as opposed to any other
    short line a document may open with? Two or more surviving tokens, and:

    Latin — every token is Title Case or ALL CAPS, the way a CV prints a name
    and the way prose does not.

    Arabic — carries a given name we know, or the «اسم + اللقب» shape where
    the family name takes the definite article and the given name does not.
    Arabic has no letter case, so this is the only signal available without
    guessing.

    AUDIT 2026-08-06. The article branch used to ask only whether the FIRST
    token lacked «ال» and the LAST one carried it, and half the Arabic
    language answers yes to that: «درست العملاء المتوقعين» is a customer
    describing their own work and it passed. A Saudi name is «اسم + لقب» —
    exactly ONE of its tokens takes the article, and it is the family name at
    the end. A verb phrase sprays the article over every noun in it. Counting
    the article-bearing tokens instead of looking at the two ends separates
    the two populations for free: «فلان الفلاني» has one, «درست العملاء
    المتوقعين» has two. What this branch alone cannot separate is «طورت خطة
    التواصل», which also has exactly one — that is what
    :func:`_reads_as_a_sentence` is for, and why it runs first."""
    latin = [t for t in tokens if t.isascii()]
    arabic = [fold_token(t) for t in tokens if not t.isascii()]
    if len(latin) >= 2 and all(_title_cased(t) for t in latin):
        return True
    if len(arabic) >= 2:
        if any(f in _AR_GIVEN_NAMES or f.startswith("عبدال") for f in arabic):
            return True
        articled = [f for f in arabic if f.startswith("ال")]
        if len(articled) == 1 and arabic[-1].startswith("ال"):
            return True
    return False


#: Arabic function words a name line cannot contain. A person is «فلان
#: الفلاني»; a sentence is «عملت في شركة الاتصالات» — the preposition is the
#: giveaway, and it is a closed class, so this list cannot rot the way a
#: vocabulary of content words would. Stored folded, and matched with the
#: proclitic «و» stripped, because «ثم» and «وثم» are the same word.
_AR_FUNCTION_WORDS: frozenset[str] = frozenset(fold_token(w) for w in {
    "في", "من", "إلى", "الى", "على", "عن", "مع", "ثم", "عند", "لدى", "بعد",
    "قبل", "خلال", "حتى", "بين", "ضمن", "لكن", "أو", "او", "كما", "حيث",
    "التي", "الذي", "هذا", "هذه", "كل", "بدون", "بسبب", "أثناء", "اثناء",
})
#: The shortest word this rule will call a first-person past verb. «بنت» and
#: «بيت» are three letters; «عملت», «درست», «طورت», «حققت» are four.
_MIN_VERB_LEN = 4


def _reads_as_a_sentence(candidate: str) -> bool:
    """Is this short line a fragment of prose rather than a person's name?

    AUDIT 2026-08-06, and the half of the backstop that was destroying
    answers. ``_residual_name`` reads the top lines of anything
    :func:`_document_shaped` accepts, and a customer describing their career
    on WhatsApp writes exactly the shape ``_is_name_line`` was built to
    recognise: two to four alphabetic words, no digits, no punctuation.
    «عملت في شركة الاتصالات» and «درست العملاء المتوقعين» both arrived as
    "names", assert_no_pii raised PiiLeak, and enrichment.handle_answer turns
    any exception into a soft_fail — so the customer's own achievement was
    dropped in silence, which is the precise asymmetry that function's
    docstring says must not happen.

    Two signals, both structural and both closed-class, so neither can be
    outgrown by vocabulary:

    * a function word — a name line never contains a preposition or a
      conjunction, and a sentence about your work almost always does;
    * a leading first-person past verb: «فعلتُ» is written «فعلت», and an
      Arabic answer to «وش سويت؟» opens with one nearly every time. Applied
      to the FIRST token only, and only in Arabic.

    The cost is that a name whose first token ends in «ت» — «ثابت الفلاني» —
    is invisible to the BACKSTOP. That is the direction this function is
    allowed to be wrong in: strip_pii does not consult it (it reads the
    header through ``_inferred_tokens``, which is unchanged), so recall is
    where it always was and only precision moved. Latin needs no equivalent
    rule: ``_person_shaped`` already demands that every Latin token be Title
    Case, and prose is not.
    """
    words = [m.group() for m in _WORD.finditer(candidate)]
    if not words:
        return False
    for word in words:
        folded = fold_token(word)
        stem = folded[1:] if folded.startswith("و") else folded
        if folded in _AR_FUNCTION_WORDS or stem in _AR_FUNCTION_WORDS:
            return True
    if words[0].isascii():
        return False
    first = fold_token(words[0])
    first = first[1:] if first.startswith("و") else first
    return (
        len(first) >= _MIN_VERB_LEN
        and first.endswith("ت")
        and first not in _AR_GIVEN_NAMES
    )


def _title_cased(token: str) -> bool:
    parts = [p for p in re.split(r"['’-]", token) if p]
    return bool(parts) and all(
        p[:1].isupper() and (len(p) == 1 or p[1:].islower() or p.isupper())
        for p in parts
    )


#: Employment dates. A CV carries them; a WhatsApp achievement rarely carries
#: two, and «حققنا 2024» is one.
_YEAR = re.compile(r"(?<![0-9])(?:19|20)\d{2}(?![0-9])")
#: A bulleted line, in any of the marks a converted PDF/DOCX leaves behind.
_BULLET_LINE = re.compile(r"^[ \t]*[-–—•·*▪◦]\s+\S", re.MULTILINE)
#: A contact placeholder strip_pii has already written. Its presence means the
#: text carried an email or a phone number, which is a DOCUMENT fact.
_CONTACT_PLACEHOLDER = re.compile(r"\[(?:EMAIL|PHONE)_\d+\]")
#: How many bulleted lines make a document. Two is a WhatsApp list.
_MIN_DOC_BULLETS = 3


def _document_shaped(text: str) -> bool:
    """Is this a FILE we are looking at, rather than something someone typed?

    AUDIT 2026-08-05 — the structural half of the leak. The backstop gated its
    residual-name check on ``_looks_like_cv``, the stripper's own predicate,
    so the two failed in exactly the same places: the CV headed «الخبرة
    العملية» / «المؤهل الدراسي» was invisible to the stripper AND invisible to
    the thing whose only job is to catch what the stripper misses. A backstop
    that shares the primary's assumption is not a backstop; it is the same
    check spelled twice.

    So this asks the question a different way, from signals the heading
    vocabulary knows nothing about and that no edit to that vocabulary can
    move: dates, bullets, and contact details that were already redacted.
    ``_looks_like_cv`` is one route in, never the only one — the union is what
    makes the coverage independent.

    AUDIT 2026-08-06 — and the sentence above («strict enough to leave a typed
    answer alone») was simply not true of the evidence as it was weighted.
    Years and bullets each admitted a document ON THEIR OWN, gated on nothing
    but four non-blank lines, and those are the two things a person describing
    their career on WhatsApp writes:

        «عملت في شركة الاتصالات / من 2018 إلى 2022 / …»   → two years
        «- درست العملاء المتوقعين / - طورت خطة التواصل / …» → four bullets

    Both were read as documents, both then produced a "residual name", and
    enrichment.handle_answer turned the resulting PiiLeak into a soft_fail —
    the customer's achievement dropped without a word. So the evidence is now
    weighted by how document-SPECIFIC it is, which is the honest reading of
    each signal:

    * STRONG, sufficient alone. A redacted contact means the text carried an
      email or a phone number, which prose does not; recognised section
      headings mean lines whose whole content is a heading, which prose does
      not have either. Between them these cover effectively every real CV,
      because strip_pii runs first and every CV prints a way to reach its
      author.
    * WEAK, never sufficient alone. Two years is a date range and a sentence
      can hold one; three bullets is a WhatsApp list. Together they are a
      layout, and a layout is a file — so the pair still admits a document,
      which is what keeps a heading-less, contact-less CV from walking past.

    A false alarm here is a customer's achievement silently dropped (see
    :func:`assert_no_pii`), and that is why the weak signals had to lose their
    standing rather than merely be raised: no threshold on years or bullets
    separates a CV from a person listing what they did."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < _MIN_DOC_LINES:
        return False
    if _looks_like_cv(text) or _CONTACT_PLACEHOLDER.search(text):
        return True
    weak = (
        len(set(_YEAR.findall(text))) >= 2,
        len(_BULLET_LINE.findall(text)) >= _MIN_DOC_BULLETS,
    )
    return all(weak)


def _residual_name(text: str) -> str | None:
    """A name that appears to have survived stripping, or None. Only the first
    two readable lines of a document are examined — see the reasoning in
    :func:`assert_no_pii`."""
    if not _document_shaped(text):
        return None
    for candidate in _header_name_lines(text, max_lines=_HEADER_LINES, max_hits=2):
        # Prose first, and on the RAW candidate rather than its surviving
        # tokens: the preposition that gives «عملت في شركة الاتصالات» away is
        # two letters long, so `_inferred_tokens` throws away the evidence
        # (audit 2026-08-06).
        if _reads_as_a_sentence(candidate):
            continue
        tokens = _inferred_tokens(candidate)
        if len(tokens) >= 2 and _person_shaped(tokens):
            return candidate
    return None


def _body_words(text: str) -> list[str]:
    """Every word of the text except the ones inside a placeholder."""
    return [m.group() for m in _TOKEN.finditer(text) if not m.group().startswith("[")]


def assert_no_pii(text: str, *, known_name: str | None) -> None:
    """Fail closed: raise :class:`PiiLeak` if anything personal survived.

    This is the backstop, and until the 2026-08-05 audit it could not fail in
    the way that mattered. It re-ran the caller's own three rules on the
    caller's own ``known_name``, so it was depth against a typo in strip_pii
    and blind to the failure that actually happens in production: the caller
    holding the WRONG name, or no name at all. It now also reads the DOCUMENT
    — the source the caller failed to consult — and refuses text whose top
    still carries something person-shaped.

    The trade-off is not symmetric and the choice here is deliberate. A false
    negative costs one leaked name at one model boundary. A false POSITIVE
    costs a paying customer: enrichment.handle_answer turns any exception into
    a soft_fail and the achievement the customer just typed is dropped;
    intent.py falls back to deterministic parsing; the tailoring chain loses a
    model call on a CV someone has paid for. So RECALL lives in strip_pii,
    where the cost of being wrong is a redacted word inside a prompt, and this
    function is tuned for PRECISION: it only speaks up about a document
    (:func:`_document_shaped` — four lines plus dates, bullets, redacted
    contacts or headings, deliberately NOT the stripper's own predicate, which
    is how it stayed blind to the stripper's blind spot until 2026-08-05),
    only about its first two
    readable lines, only when two or more tokens survive the role/place filter,
    and only when they carry a positive person signal — Latin title case, or a
    known Arabic given name or the «اسم + اللقب» article pattern. A name
    buried in the body of a document goes undetected on purpose: we will not
    block a customer's delivery on a heuristic that cannot tell a person from
    an employer."""
    if _EMAIL.search(text):
        raise PiiLeak("email survived stripping")
    if _phone_hits(text):
        raise PiiLeak("phone-like digit run survived stripping")
    typed = _name_tokens(known_name)
    for token in typed:
        if re.search(re.escape(token), text, re.IGNORECASE):
            raise PiiLeak("customer name survived stripping")
    matcher = _NameMatcher(typed)
    if matcher and any(matcher.matches(word) for word in _body_words(text)):
        raise PiiLeak("a spelling variant of the customer name survived stripping")
    if _residual_name(text) is not None:
        raise PiiLeak("a personal name survived stripping")


# ── the extractor boundary ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractedFacts:
    experiences: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    certifications: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[dict[str, Any]] = field(default_factory=list)
    achievements: list[str] = field(default_factory=list)


class ExtractorClient(Protocol):
    def extract(self, cv_text: str) -> ExtractedFacts: ...


def _string_items() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _object_items(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "experiences": _object_items(
            {
                "title": {"type": ["string", "null"]},
                "employer": {"type": ["string", "null"]},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "description": {"type": ["string", "null"]},
                "achievements": _string_items(),
            },
            ["title", "employer", "start_date", "end_date", "description", "achievements"],
        ),
        "education": _object_items(
            {
                "degree": {"type": ["string", "null"]},
                "field_of_study": {"type": ["string", "null"]},
                "institution": {"type": ["string", "null"]},
                "graduation_year": {"type": ["integer", "null"]},
            },
            ["degree", "field_of_study", "institution", "graduation_year"],
        ),
        "certifications": _object_items(
            {
                "name": {"type": ["string", "null"]},
                "issuer": {"type": ["string", "null"]},
                "issue_date": {"type": ["string", "null"]},
            },
            ["name", "issuer", "issue_date"],
        ),
        "skills": _string_items(),
        "languages": _object_items(
            {
                "language": {"type": ["string", "null"]},
                "proficiency": {"type": ["string", "null"]},
            },
            ["language", "proficiency"],
        ),
        "achievements": _string_items(),
    },
    "required": [
        "experiences", "education", "certifications", "skills", "languages",
        "achievements",
    ],
    "additionalProperties": False,
}

_SYSTEM = (
    "You extract structured facts from resumes (Arabic or English). Rules:\n"
    "- Extract ONLY what the text literally states. Never infer, embellish or "
    "invent anything — a downstream validator treats invented content as a "
    "defect.\n"
    "- The text contains placeholders like [NAME], [EMAIL_1], [PHONE_1] for "
    "redacted personal data. Ignore them; never reproduce them in output "
    "fields.\n"
    "- Keep the original language of each fact (do not translate).\n"
    "- Dates: copy them as written; use null when absent."
)


class AnthropicExtractor(TokenCounter):
    """The real extractor — Claude only (D1). The SDK client is injectable so
    tests exercise the exact request shape with no network; retries for
    transient errors are the SDK's built-in exponential backoff."""

    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        model: str = _MODEL,
    ) -> None:
        if client is None:  # pragma: no cover — exercised live in C5's gate
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self._model = model
        self.reset_token_counters()

    def extract(self, cv_text: str) -> ExtractedFacts:
        if len(cv_text) > MAX_INPUT_CHARS:
            # Budget guard: refuse before any token is spent — never silently
            # truncate (a truncated CV would extract misleading facts).
            raise ExtractionFailed("cv_text_too_large")
        response = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": cv_text}],
        )
        self.absorb_usage(response)
        if response.stop_reason != "end_turn":
            raise ExtractionFailed(f"stop_reason:{response.stop_reason}")
        # getattr-based extraction: works for SDK blocks and injected fakes
        # alike, and keeps mypy honest about the SDK's wide content union.
        text: str | None = None
        for block in response.content:
            candidate = getattr(block, "text", None)
            if getattr(block, "type", "") == "text" and isinstance(candidate, str):
                text = candidate
                break
        if text is None:
            raise ExtractionFailed("no_text_block")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ExtractionFailed("invalid_json") from exc
        return ExtractedFacts(
            experiences=data.get("experiences", []),
            education=data.get("education", []),
            certifications=data.get("certifications", []),
            skills=data.get("skills", []),
            languages=data.get("languages", []),
            achievements=data.get("achievements", []),
        )


# ── the pipeline: consent → strip → prove clean → extract → EXTRACTED rows ──

_CATEGORY_ITEMS: tuple[tuple[str, str], ...] = (
    ("experiences", "experience"),
    ("education", "education"),
    ("certifications", "certification"),
    ("languages", "language"),
)


def run_extraction(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    cv_text: str,
    known_name: str | None,
    extractor: ExtractorClient,
) -> list[ProfileFact]:
    """Strip PII, prove the text clean, extract, and store EXTRACTED facts."""
    require_required_consents(session, tenant_id=tenant_id)

    stripped = strip_pii(cv_text, known_name=known_name)
    assert_no_pii(stripped.text, known_name=known_name)  # defense in depth

    # §14 cost fuel: the extraction spend is metered on the SAME mechanism as
    # every other paid Claude boundary — one usage_events row with the real
    # token numbers, attributed to this tenant.
    with LlmMeter(session, tenant_id=tenant_id).around("llm_extraction", extractor):
        facts = extractor.extract(stripped.text)

    rows: list[ProfileFact] = []

    def _add(category: str, payload: dict[str, Any]) -> None:
        row = ProfileFact(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            category=category,
            payload=payload,
            status="EXTRACTED",
            source="cv_extraction",
        )
        session.add(row)
        rows.append(row)

    for attr, category in _CATEGORY_ITEMS:
        for item in getattr(facts, attr):
            _add(category, dict(item))
    for skill in facts.skills:
        _add("skill", {"name": skill})
    for achievement in facts.achievements:
        _add("achievement", {"text": achievement})

    session.flush()
    return rows
