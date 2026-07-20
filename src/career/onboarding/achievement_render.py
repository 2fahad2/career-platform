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
    def render(self, arabic_answer: str) -> dict[str, Any]:
        """{is_achievement, english_bullet, qualitative_only, arabic_gloss}."""
        ...


class RenderFailed(Exception):
    """The renderer produced nothing usable after the bounded retry."""


def render_achievement(
    renderer: AchievementRenderer,
    *,
    arabic_answer: str,
    vocabulary: set[str],
) -> dict[str, Any] | None:
    """Render + ground with ONE bounded regeneration. Returns the accepted
    payload {english_bullet, arabic_gloss, qualitative_only} or None when the
    answer is not an achievement or nothing grounded survives (never invents
    a fallback — a missing bullet is honest, a false one is not)."""
    for _attempt in range(2):
        result = renderer.render(arabic_answer)
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


class AnthropicAchievementRenderer:  # pragma: no cover — live boundary
    """Mirrors AnthropicExtractor: injectable client, json_schema output,
    stop_reason check. Safety comes from the deterministic guard, not this."""

    def __init__(self, api_key: str | None = None, client: Any | None = None,
                 model: str = _MODEL) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self._model = model

    def render(self, arabic_answer: str) -> dict[str, Any]:
        import json

        response = self._client.messages.create(
            model=self._model, max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"}, system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": arabic_answer}],
        )
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
