"""CV extraction acceptance tests (whitepaper §05 + §15.8) — before the code.

The extractor never sees PII: the customer's name, phone numbers (Western or
Arabic-Indic digits, any common format) and emails are replaced with
placeholders BEFORE any external call, and a fail-closed validator proves the
stripped text is clean. Extraction output is NOT truth — facts land as
EXTRACTED rows only (§15.5); the consent gate runs first (§12).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import text as sql_text

from career.db.session import tenant_session
from career.onboarding import consents, extraction

CV_TEXT = """
Fahad Almulhim
Riyadh, Saudi Arabia — +966 50 123 4567 — fahad.m@example.com
Senior Business Analyst at Acme Corp (2019 - Present)
- Led a digital transformation program across 3 departments
Education: B.Sc. Information Systems, KSU, 2015
Skills: SQL, Power BI, Stakeholder Management
للتواصل: ٠٥٥١٢٣٤٥٦٧
"""


# ── PII stripping: nothing personal ever leaves the server (§15.8) ───────────


def test_strip_replaces_emails_phones_and_name() -> None:
    stripped = extraction.strip_pii(CV_TEXT, known_name="Fahad Almulhim")
    assert "fahad.m@example.com" not in stripped.text
    assert "50 123 4567" not in stripped.text
    assert "٠٥٥١٢٣٤٥٦٧" not in stripped.text
    assert "Fahad" not in stripped.text
    assert "Almulhim" not in stripped.text
    # The professional content survives.
    assert "Senior Business Analyst" in stripped.text
    assert "digital transformation" in stripped.text


def test_strip_handles_phone_format_variants() -> None:
    variants = "+966501234567 / 0501234567 / 050-123-4567 / +966 50 123 4567"
    stripped = extraction.strip_pii(variants, known_name=None)
    for digits in ("501234567", "123", "4567"):
        assert digits not in stripped.text


def test_validator_fails_closed_on_leaks() -> None:
    with pytest.raises(extraction.PiiLeak):
        extraction.assert_no_pii("email me: someone@site.com", known_name=None)
    with pytest.raises(extraction.PiiLeak):
        extraction.assert_no_pii("call 0512345678 now", known_name=None)
    with pytest.raises(extraction.PiiLeak):
        extraction.assert_no_pii("Fahad did great work", known_name="Fahad Almulhim")
    extraction.assert_no_pii("worked on ERP rollouts since 2019", known_name=None)


def test_name_stripping_requires_meaningful_tokens() -> None:
    """Short particles inside a name (e.g. 'Al') must not blank the whole CV."""
    stripped = extraction.strip_pii("Al Falak Systems analyst", known_name="Mona Al Saud")
    assert "Systems analyst" in stripped.text


# ── the extractor boundary is injectable and receives ONLY stripped text ─────


@dataclass
class SpyExtractor:
    result: extraction.ExtractedFacts
    seen: list[str] = field(default_factory=list)

    def extract(self, cv_text: str) -> extraction.ExtractedFacts:
        self.seen.append(cv_text)
        return self.result


def _facts() -> extraction.ExtractedFacts:
    return extraction.ExtractedFacts(
        experiences=[{"title": "Senior Business Analyst", "employer": "Acme Corp",
                      "start_date": "2019", "end_date": None,
                      "achievements": ["Led a digital transformation program"]}],
        education=[{"degree": "B.Sc. Information Systems", "institution": "KSU",
                    "graduation_year": 2015}],
        certifications=[],
        skills=["SQL", "Power BI"],
        languages=[{"language": "Arabic", "proficiency": "native"}],
        achievements=["Led a digital transformation program across 3 departments"],
    )


def _grant_required(session, tenant_id: uuid.UUID) -> None:
    for p in consents.REQUIRED_KEYS:
        consents.record_consent(session, tenant_id=tenant_id, purpose=p, action="granted")


def test_pipeline_strips_before_the_extractor_sees_anything(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    spy = SpyExtractor(result=_facts())
    with tenant_session(a) as s:
        _grant_required(s, tid)
        extraction.run_extraction(
            s, tenant_id=tid, cv_text=CV_TEXT, known_name="Fahad Almulhim",
            extractor=spy,
        )
    assert len(spy.seen) == 1
    extraction.assert_no_pii(spy.seen[0], known_name="Fahad Almulhim")  # no raise


def test_extraction_refuses_before_consent(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    spy = SpyExtractor(result=_facts())
    with tenant_session(a) as s:
        with pytest.raises(consents.ConsentMissing):
            extraction.run_extraction(
                s, tenant_id=uuid.UUID(a), cv_text=CV_TEXT,
                known_name="Fahad Almulhim", extractor=spy,
            )
    assert spy.seen == []  # the boundary was never touched


def test_facts_land_as_extracted_rows_never_confirmed(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _grant_required(s, tid)
        rows = extraction.run_extraction(
            s, tenant_id=tid, cv_text=CV_TEXT, known_name="Fahad Almulhim",
            extractor=SpyExtractor(result=_facts()),
        )
    assert rows  # something was stored
    with tenant_session(a) as s:
        db = s.execute(
            sql_text("SELECT category, status, source FROM profile_facts")
        ).all()
    assert len(db) == len(rows)
    assert {r.status for r in db} == {"EXTRACTED"}  # §15.5: extraction is not truth
    assert {r.source for r in db} == {"cv_extraction"}
    assert {r.category for r in db} == {
        "experience", "education", "skill", "language", "achievement",
    }


# ── AnthropicExtractor: request shape + honest failure, no network ───────────


class _FakeContent:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], stop_reason: str = "end_turn") -> None:
        self.content = [_FakeContent(json.dumps(payload))]
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return self._response


class _FakeAnthropic:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


_PAYLOAD = {
    "experiences": [], "education": [], "certifications": [],
    "skills": ["SQL"], "languages": [], "achievements": [],
}


def test_anthropic_extractor_request_shape() -> None:
    fake = _FakeAnthropic(_FakeResponse(_PAYLOAD))
    client = extraction.AnthropicExtractor(client=fake)
    result = client.extract("stripped cv text")
    assert result.skills == ["SQL"]

    call = fake.messages.calls[0]
    assert call["model"] == "claude-opus-4-8"
    assert call["max_tokens"] == 16000
    assert call["thinking"] == {"type": "adaptive"}
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    # The stripped CV text rides in the user message.
    assert "stripped cv text" in str(call["messages"])


def test_anthropic_extractor_fails_closed_on_refusal_and_truncation() -> None:
    for stop in ("refusal", "max_tokens"):
        fake = _FakeAnthropic(_FakeResponse(_PAYLOAD, stop_reason=stop))
        with pytest.raises(extraction.ExtractionFailed):
            extraction.AnthropicExtractor(client=fake).extract("text")


def test_anthropic_extractor_budget_guard_rejects_oversized_input() -> None:
    fake = _FakeAnthropic(_FakeResponse(_PAYLOAD))
    client = extraction.AnthropicExtractor(client=fake)
    with pytest.raises(extraction.ExtractionFailed, match="too_large"):
        client.extract("x" * (extraction.MAX_INPUT_CHARS + 1))
    assert fake.messages.calls == []  # rejected before any spend
