"""Rendering a colloquial-Arabic achievement into a grounded English CV bullet
(F-ENRICH, CHANGELOG §13, deviation D14).

The safety spine is :func:`bullet_is_grounded` — a DETERMINISTIC cross-lingual
guard that treats the customer's raw Arabic answer as the ONLY evidence and
rejects any number, tool, certification, or proper noun in the English bullet
that is not traceable to it (or to the confirmed bank). This closes the hole
``contains_invented_content`` cannot see: it compares a rewrite against an
``original`` in the SAME language, so an English "40%" never matches Arabic
text and would pass. Here the Arabic answer's digits (Arabic-Indic → Western,
plus spelled-out one..twenty) and its Latin runs (SQL, Jira) are the whitelist.

The model's own claims about what it used are never trusted — the guard
recomputes from the raw Arabic. A rejected bullet triggers one bounded
regeneration; a still-ungrounded bullet is never stored. Final promotion to
the bank requires the customer's explicit confirmation (the second net).
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from career.arabic import normalize_ar
from career.cv.close import LlmMeter, TokenCounter, metered
from career.cv.generate import _CERT_RE, _KNOWN_FRAMEWORKS
from career.cv.validate import _ARABIC_RE

#: An invented ENTITY that matters is a tool/cert/employer/product — captured
#: as an ALL-CAPS acronym (STC, SQL, NOC, ITIL), a CamelCase/internal-capital
#: brand (ServiceNow, PowerBI), a known framework, or a cert. A plain
#: sentence-initial capital ("Significantly", "Served") is NOT an entity — the
#: old blanket proper-noun rule flagged those and is deliberately not used.
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9+#.&/-]{1,}\b")           # STC, SQL, NOC
_CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b")     # ServiceNow

#: Arabic-Indic + Persian digits → Western (reuse extraction's digit classes).
_AR_TO_WEST = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

#: Spelled-out Arabic 1..20 → digit (colloquial + MSA forms), and the English
#: numerals a bullet might use — both mapped before the subset check.
_AR_NUMBER_WORDS: dict[str, int] = {
    "واحد": 1, "وحده": 1, "وحدة": 1, "اثنين": 2, "اثنينن": 2, "ثنين": 2,
    "ثلاثة": 3, "ثلاث": 3, "تلاته": 3, "ثلاثه": 3, "اربعة": 4, "اربع": 4,
    "اربعه": 4, "خمسة": 5, "خمس": 5, "خمسه": 5, "ستة": 6, "ست": 6, "سته": 6,
    "سبعة": 7, "سبع": 7, "سبعه": 7, "ثمانية": 8, "ثمان": 8, "ثمانيه": 8,
    "تسعة": 9, "تسع": 9, "تسعه": 9, "عشرة": 10, "عشر": 10, "عشره": 10,
    "عشرين": 20, "ثلاثين": 30, "اربعين": 40, "خمسين": 50, "مية": 100,
    "مئة": 100, "ماية": 100, "الف": 1000, "الفين": 2000,
}
_EN_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "hundred": 100, "thousand": 1000,
}

_DIGIT_RUN = re.compile(r"\d+")
_AR_WORD_RE = re.compile(r"[؀-ۿ]+")
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9+#.&/-]*")


def _evidence(arabic_answer: str) -> tuple[set[int], set[str]]:
    """(numbers, lowercase-entity-tokens) grounded by the Arabic answer.
    Numbers = every digit run after Arabic→Western translation + spelled-out
    words. Entities = Latin runs typed in the answer (SQL, Cisco), lowered."""
    west = arabic_answer.translate(_AR_TO_WEST)
    numbers = {int(m) for m in _DIGIT_RUN.findall(west)}
    for word in _AR_WORD_RE.findall(arabic_answer):
        if word in _AR_NUMBER_WORDS:
            numbers.add(_AR_NUMBER_WORDS[word])
    entities = {m.lower() for m in _LATIN_RUN.findall(west)}
    return numbers, entities


def bullet_is_grounded(
    english: str, *, arabic_answer: str, vocabulary: set[str]
) -> tuple[bool, str]:
    """(ok, reason). Every factual atom of the English bullet must trace to
    the Arabic answer or the confirmed bank. Deterministic; the model's
    self-report is never consulted."""
    # ARABIC LEAK — the bullet must be English before it is ever shown/stored.
    if _ARABIC_RE.search(english):
        return False, "arabic_leak"

    ev_numbers, ev_entities = _evidence(arabic_answer)

    # NUMBER SUBSET — every English number (digit run OR spelled-out) must be
    # present in the Arabic evidence. Kills an invented "40%" or "team of 15".
    english_l = english.lower()
    en_numbers = {int(m) for m in _DIGIT_RUN.findall(english)}
    for word, value in _EN_NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", english_l):
            en_numbers.add(value)
    if any(n not in ev_numbers for n in en_numbers):
        return False, "invented_number"

    # ENTITY SUBSET — every cert, known framework, acronym, and CamelCase
    # brand must be grounded by the Arabic Latin-runs or the confirmed bank.
    hits: set[str] = set()
    hits |= {m.lower() for m in _CERT_RE.findall(english)}
    hits |= {m.lower() for m in _ACRONYM_RE.findall(english)}
    hits |= {m.lower() for m in _CAMEL_RE.findall(english)}
    for fw in _KNOWN_FRAMEWORKS:
        if re.search(rf"\b{re.escape(fw)}\b", english_l):
            hits.add(fw)
    for hit in hits:
        parts = hit.split()
        if all(p in ev_entities or p in vocabulary for p in parts):
            continue
        return False, "invented_entity"

    return True, "ok"


# ── the model boundary (one PII-free structured call, D14) ───────────────────


class AchievementRenderer(Protocol):
    def render(
        self, arabic_answer: str, *, angle: str = "", instruction: str = ""
    ) -> dict[str, Any]:
        """{is_achievement, english_bullet, qualitative_only, arabic_gloss}.

        ``angle`` steers one panel candidate (F-PANEL §14); ``instruction``
        carries the customer's own editing note («ابدع», «اختصرها»). Neither
        may loosen the grounding rules — the deterministic guard runs after."""
        ...


class RenderFailed(Exception):
    """The renderer produced nothing usable after the bounded retry."""


#: The ONLY editing directions we will ever put in a prompt (review finding
#: 2026-07-30). The customer's own words are NEVER interpolated: they are
#: classified into one of these fixed English strings first. This closes the
#: prompt-injection surface, removes any PII path into the model (§15.8), and
#: denies the one invention the deterministic guard cannot catch — invented
#: SCOPE or SENIORITY in plain prose («قل إني كنت مدير الفرع») passes the
#: number/entity checks, so we refuse to relay such a request at all.
EDIT_INTENTS: dict[str, str] = {
    "stronger": "Make it read stronger and more accomplished — sharper verbs, "
                "clearer ownership — while adding NO fact the Arabic lacks.",
    "shorter": "Make it shorter and tighter without dropping substance.",
    "simpler": "Use plainer, simpler English.",
    "rephrase": "Rephrase it differently with the same meaning.",
}

# ── normalize_ar lives in career.arabic now, and is re-exported here ────────
#
# Arabic normalisation is shared by every text comparison in this feature: a
# customer types «مضبوط ✅» or «مضبوط» or «مضبووط», «أقوى» or «اقوي» — all must
# compare equal.
#
# The fold was DEFINED here, and this module reaches into career.cv.generate at
# import time, so every caller that only wanted to compare two Arabic strings —
# the pure inbound classifier, the PII stripper — had to choose between
# dragging the whole CV stack in behind it and keeping a second copy of a
# compliance primitive. Both happened. career.arabic is a leaf that imports
# nothing from career, and the import at the top of this file re-exports the
# name so the callers that already reach for it here keep working, while a
# future reader still finds exactly ONE authority.


#: Saudi-colloquial cues → intent, in NORMALISED form (so no duplicate hamza
#: spellings are needed). Unmatched text falls back to «rephrase»: harmless
#: and always safe, which is what makes a hostile note toothless.
_INTENT_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ابدع", "اقوي", "قوي", "احترافي", "افخم", "حسن", "طور", "احلي",
      "افضل", "اجمل"), "stronger"),
    (("اقصر", "اختصر", "قصر", "طويله", "مختصر"), "shorter"),
    (("ابسط", "بسط", "سهل", "صعبه", "وضح", "اوضح"), "simpler"),
)


def classify_edit_intent(text: str | None) -> str:
    """Map a customer's editing note onto a FIXED intent key. Never returns
    their words — only one of EDIT_INTENTS' keys."""
    body = normalize_ar(text)
    for cues, intent in _INTENT_CUES:
        if any(cue in body for cue in cues):
            return intent
    return "rephrase"


# Kept deliberately: the single-shot ancestor of run_panel (bullet_panel.py).
# D14 documents it, three tests pin it, and it is the documented fallback when
# no panel/judge is wired.
def render_achievement(
    renderer: AchievementRenderer,
    *,
    arabic_answer: str,
    vocabulary: set[str],
    instruction: str = "",
    meter: LlmMeter | None = None,
) -> dict[str, Any] | None:
    """Render + ground with ONE bounded regeneration. Returns the accepted
    payload {english_bullet, arabic_gloss, qualitative_only} or None when the
    answer is not an achievement or nothing grounded survives (never invents
    a fallback — a missing bullet is honest, a false one is not).

    ``meter`` (§14) records each render's real token spend against the tenant;
    None keeps this pure for the unit tests that own its behaviour."""
    # review finding #1: only forward what we actually have, so a renderer
    # with the narrow signature keeps working (and a drifted real renderer
    # still fails loudly rather than silently).
    extra: dict[str, Any] = {"instruction": instruction} if instruction else {}
    for _attempt in range(2):
        # the RETRY is a second billed call — meter inside the loop, not around
        with metered(meter, "llm_render", renderer):
            result = renderer.render(arabic_answer, **extra)
        if not result.get("is_achievement"):
            return None
        english = str(result.get("english_bullet") or "").strip()
        if not english:
            continue
        ok, _reason = bullet_is_grounded(
            english, arabic_answer=arabic_answer, vocabulary=vocabulary
        )
        if ok:
            return {
                "english_bullet": english,
                "arabic_gloss": str(result.get("arabic_gloss") or "").strip(),
                "qualitative_only": bool(result.get("qualitative_only")),
            }
    return None  # ungrounded after retry — store nothing


# ── the real Claude renderer (one PII-free structured call) ──────────────────

_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 1024

_SYSTEM = (
    "You turn a Saudi job-seeker's colloquial-Arabic sentence about what they "
    "did in a job into ONE professional English CV bullet. Render ONLY what "
    "the Arabic states. NEVER introduce a number, percentage, tool, "
    "certification, employer, product, or scope that is not explicitly in the "
    "Arabic. If the Arabic gives magnitude in words (بشكل كبير) do NOT convert "
    "it to a number. Output English only for the bullet; provide a faithful "
    "Arabic paraphrase as the gloss. If the message is not a work achievement, "
    "set is_achievement=false."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_achievement", "english_bullet", "qualitative_only",
                 "arabic_gloss"],
    "properties": {
        "is_achievement": {"type": "boolean"},
        "english_bullet": {"type": "string"},
        "qualitative_only": {"type": "boolean"},
        "arabic_gloss": {"type": "string"},
    },
}


class AnthropicAchievementRenderer(TokenCounter):  # pragma: no cover — live
    """Mirrors AnthropicExtractor: injectable client, json_schema output,
    stop_reason check. Safety comes from the deterministic guard, not this.

    Metered (§14): the panel fires this THREE times per customer answer, so an
    unmetered renderer was the single largest blind spot in the cost picture."""

    def __init__(self, api_key: str | None = None, client: Any | None = None,
                 model: str = _MODEL) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self._model = model
        self.reset_token_counters()

    def render(
        self, arabic_answer: str, *, angle: str = "", instruction: str = ""
    ) -> dict[str, Any]:
        import json

        # _SYSTEM stays byte-frozen: it is the authority channel. The angle
        # is ours (a fixed literal); the editing direction is a FIXED string
        # from EDIT_INTENTS — never the customer's own text (§15.9 spirit).
        system = _SYSTEM
        if angle:
            system += f"\n\nEMPHASIS FOR THIS DRAFT: {angle}"
        user_text = arabic_answer
        if instruction:
            direction = EDIT_INTENTS.get(instruction, EDIT_INTENTS["rephrase"])
            user_text = (
                f"{arabic_answer}\n\n---\nThe job-seeker asked for a "
                f"revision. Direction: {direction}"
            )
        response = self._client.messages.create(
            model=self._model, max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"}, system=system,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": user_text}],
        )
        self.absorb_usage(response)
        if response.stop_reason != "end_turn":
            return {"is_achievement": False}
        for block in response.content:
            text = getattr(block, "text", None)
            if getattr(block, "type", "") == "text" and isinstance(text, str):
                try:
                    return dict(json.loads(text))
                except ValueError:
                    return {"is_achievement": False}
        return {"is_achievement": False}


# ── the icebreaker examples (كاسر التجمّد, D14 message set) ──────────────────

_EXAMPLES_SYSTEM = (
    "You write EXACTLY three short example achievement lines in colloquial "
    "Saudi Arabic for a job-seeker's specific past role, so they can pick the "
    "one closest to what they actually did. Base the examples ONLY on the "
    "role title and description given. STRICT RULES: no numbers or "
    "percentages of any kind, no tool/product/company names that are not in "
    "the given text, each line under 12 words, first-person past tense "
    "(كنت مسؤول عن…, طورت…, دربت…). Plausible everyday activities for this "
    "role — never impressive-sounding inventions."
)

_EXAMPLES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["examples"],
    "properties": {
        # LIVE BUG (29 July): structured-output schemas accept neither
        # minItems>1 nor maxItems — with minItems/maxItems=3 EVERY call 400'd,
        # and the swallowed error left the icebreaker silently dead from the
        # day it shipped. The count lives in the prompt; prepare_examples
        # trims to three after the deterministic scrub gate.
        "examples": {"type": "array", "items": {"type": "string"}},
    },
}

_ANY_DIGIT_RE = re.compile(r"[0-9٠-٩۰-۹]")


def scrub_examples(examples: list[str], *, role_payload_text: str) -> list[str]:
    """Deterministic gate on model-written examples (they become the
    customer's answer verbatim when picked, so they must be un-poisonable):
    drop any example carrying a digit, or a Latin token not present in the
    role's own confirmed payload."""
    allowed_latin = {m.lower() for m in _LATIN_RUN.findall(role_payload_text)}
    clean: list[str] = []
    for ex in examples:
        text = str(ex).strip()
        if not text or _ANY_DIGIT_RE.search(text):
            continue
        latin = {m.lower() for m in _LATIN_RUN.findall(text)}
        if latin - allowed_latin:
            continue
        clean.append(text)
    return clean


def format_examples_message(examples: list[str]) -> str:
    """The approved icebreaker wrapper — numbered menu, pick or edit or
    write your own (bidi-pure: each line is direction-pure Arabic)."""
    numbered = "\n".join(
        f"{'١٢٣'[i]}) {ex}" for i, ex in enumerate(examples[:3])
    )
    return (
        "عشان أسهّلها عليك، هذي أمثلة قريبة من مجالك — أي وحدة تشبه شغلك؟\n"
        "اختر رقم، أو عدّلها بكلماتك، أو اكتب من عندك:\n\n"
        f"{numbered}\n\n"
        "قل لي بس ١ أو ٢ أو ٣ وأنا أكمّل الباقي 👌"
    )


class ExamplesWriter(Protocol):
    def write(self, role_title: str, role_description: str) -> list[str]:
        """Three colloquial example lines for this role (may be empty)."""
        ...


class AnthropicExamplesWriter(TokenCounter):  # pragma: no cover — live
    """One structured call; the deterministic scrub_examples gate follows."""

    def __init__(self, api_key: str | None = None, client: Any | None = None,
                 model: str = _MODEL) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self._model = model
        self.reset_token_counters()

    def write(self, role_title: str, role_description: str) -> list[str]:
        import json

        from career.onboarding.extraction import assert_no_pii, strip_pii

        # These two strings come straight off a ProfileFact payload, and a
        # fact built from a customer's free-text answer is stored verbatim —
        # so «أنا فهد وشتغلت مدير فرع» would have gone to the model as-is.
        role_title = strip_pii(role_title or "", known_name=None).text
        role_description = strip_pii(role_description or "", known_name=None).text
        assert_no_pii(role_title, known_name=None)
        assert_no_pii(role_description, known_name=None)

        prompt = f"الدور: {role_title}\nالوصف: {role_description or '—'}"
        response = self._client.messages.create(
            model=self._model, max_tokens=1024,
            thinking={"type": "adaptive"}, system=_EXAMPLES_SYSTEM,
            output_config={
                "format": {"type": "json_schema", "schema": _EXAMPLES_SCHEMA}
            },
            messages=[{"role": "user", "content": prompt}],
        )
        self.absorb_usage(response)
        if response.stop_reason != "end_turn":
            return []
        for block in response.content:
            text = getattr(block, "text", None)
            if getattr(block, "type", "") == "text" and isinstance(text, str):
                try:
                    return [str(e) for e in json.loads(text).get("examples", [])]
                except (ValueError, AttributeError):
                    return []
        return []
