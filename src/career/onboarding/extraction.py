"""CV extraction via Claude — identity-stripped, fail-closed (whitepaper §05).

Order of defenses:
1. Consent gate (§12) — nothing runs before the required consents exist.
2. PII stripping (§15.8) — the customer's name, phone numbers (Western or
   Arabic-Indic digits) and emails are replaced with placeholders BEFORE any
   external call. A validator then proves the stripped text is clean; a leak
   raises instead of sending.
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
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from career.db.models import ProfileFact
from career.onboarding.consents import require_required_consents

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


def strip_pii(text: str, *, known_name: str | None) -> StrippedText:
    """Replace emails, phone numbers and the customer's name with placeholders."""
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

    for token in _name_tokens(known_name):
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        if pattern.search(out):
            replacements["[NAME]"] = known_name or ""
            out = pattern.sub("[NAME]", out)

    return StrippedText(text=out, replacements=replacements)


def assert_no_pii(text: str, *, known_name: str | None) -> None:
    """Fail closed: raise :class:`PiiLeak` if anything personal survived."""
    if _EMAIL.search(text):
        raise PiiLeak("email survived stripping")
    if _phone_hits(text):
        raise PiiLeak("phone-like digit run survived stripping")
    for token in _name_tokens(known_name):
        if re.search(re.escape(token), text, re.IGNORECASE):
            raise PiiLeak("customer name survived stripping")


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


class AnthropicExtractor:
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
