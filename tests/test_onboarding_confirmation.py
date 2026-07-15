"""Fact-confirmation acceptance tests (whitepaper §05, §15.5) — before the code.

Extraction is not truth: EXTRACTED facts become bank material only through
CUSTOMER_CONFIRMED / CUSTOMER_CORRECTED / OPERATOR_VERIFIED; a rejection
excludes the fact AND records a forbidden claim the generator must never make.
Corrections preserve the original payload as an audit trail. Questions are
asked only about the missing and the conflicting — not a 30-question survey.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sql_text

from career.db.session import tenant_session
from career.onboarding import confirmation, extraction

FACTS = extraction.ExtractedFacts(
    experiences=[{"title": "Senior BA", "employer": "Acme", "start_date": "2019",
                  "end_date": None, "description": None, "achievements": []}],
    education=[{"degree": "B.Sc. IS", "institution": "KSU", "graduation_year": 2015,
                "field_of_study": None}],
    certifications=[{"name": "PMP", "issuer": "PMI", "issue_date": "2020"}],
    skills=["SQL"],
    languages=[],
    achievements=["Cut onboarding time by 40%"],
)


class _PassThrough:
    def extract(self, cv_text: str) -> extraction.ExtractedFacts:
        return FACTS


def _seed(session, tenant_id: uuid.UUID) -> list:
    from career.onboarding import consents

    for p in consents.REQUIRED_KEYS:
        consents.record_consent(session, tenant_id=tenant_id, purpose=p, action="granted")
    return extraction.run_extraction(
        session, tenant_id=tenant_id, cv_text="cv", known_name=None,
        extractor=_PassThrough(),
    )


# ── the cursor: first unconfirmed fact, resumable, completion flag ───────────


def test_next_fact_walks_until_done(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        rows = _seed(s, tid)
        seen = 0
        while (fact := confirmation.next_fact_to_confirm(s, tenant_id=tid)) is not None:
            confirmation.confirm_fact(s, tenant_id=tid, fact_id=fact.id)
            seen += 1
        assert seen == len(rows)
        assert confirmation.is_confirmation_complete(s, tenant_id=tid)


def test_every_fact_prompt_is_arabic_with_three_choices(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed(s, tid)
        fact = confirmation.next_fact_to_confirm(s, tenant_id=tid)
        assert fact is not None
        prompt = confirmation.render_fact_prompt(fact)
        assert prompt.prompt_ar.strip()
        assert [o.id for o in prompt.options] == ["confirm", "correct", "reject"]


# ── the three customer verdicts ──────────────────────────────────────────────


def test_confirm_marks_fact_and_enters_bank(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        rows = _seed(s, tid)
        confirmation.confirm_fact(s, tenant_id=tid, fact_id=rows[0].id)
        bank = confirmation.achievement_bank(s, tenant_id=tid)
    assert [f.id for f in bank] == [rows[0].id]
    assert bank[0].status == "CUSTOMER_CONFIRMED"
    assert bank[0].confirmed_at is not None


def test_correction_updates_payload_and_keeps_the_original(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        rows = _seed(s, tid)
        exp = next(r for r in rows if r.category == "experience")
        confirmation.correct_fact(
            s, tenant_id=tid, fact_id=exp.id,
            corrected_payload={**exp.payload, "title": "Lead BA"},
        )
    with tenant_session(a) as s:
        row = s.execute(
            sql_text(
                "SELECT status, payload->>'title' AS title, "
                "original_payload->>'title' AS original_title "
                "FROM profile_facts WHERE id = :id"
            ),
            {"id": str(exp.id)},
        ).one()
    assert row.status == "CUSTOMER_CORRECTED"
    assert row.title == "Lead BA"
    assert row.original_title == "Senior BA"  # audit trail survives


def test_rejection_excludes_from_bank_and_records_forbidden_claim(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        rows = _seed(s, tid)
        cert = next(r for r in rows if r.category == "certification")
        confirmation.reject_fact(s, tenant_id=tid, fact_id=cert.id)
        bank_ids = {f.id for f in confirmation.achievement_bank(s, tenant_id=tid)}
        assert cert.id not in bank_ids
    with tenant_session(a) as s:
        claims = s.execute(
            sql_text("SELECT claim, source, source_fact_id FROM forbidden_claims")
        ).all()
    assert len(claims) == 1
    assert "PMP" in claims[0].claim  # the generator must never claim it (§15.5)
    assert claims[0].source == "customer_rejected"
    assert claims[0].source_fact_id == cert.id


# ── §15.5 hard invariant: the bank is confirm-gated, always ──────────────────


def test_bank_never_contains_extracted_or_rejected(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        rows = _seed(s, tid)
        confirmation.confirm_fact(s, tenant_id=tid, fact_id=rows[0].id)
        confirmation.reject_fact(s, tenant_id=tid, fact_id=rows[1].id)
        # rows[2:] stay EXTRACTED
        bank = confirmation.achievement_bank(s, tenant_id=tid)
    statuses = {f.status for f in bank}
    assert statuses <= confirmation.BANK_STATUSES
    assert "EXTRACTED" not in statuses
    assert "CUSTOMER_REJECTED" not in statuses
    assert len(bank) == 1


def test_verdicts_apply_only_to_extracted_facts(two_tenants: tuple[str, str]) -> None:
    """The flow is linear: re-confirming, or confirming a rejected fact,
    is a bug upstream — refuse loudly instead of silently flipping states."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        rows = _seed(s, tid)
        confirmation.confirm_fact(s, tenant_id=tid, fact_id=rows[0].id)
        with pytest.raises(confirmation.InvalidFactTransition):
            confirmation.confirm_fact(s, tenant_id=tid, fact_id=rows[0].id)
        confirmation.reject_fact(s, tenant_id=tid, fact_id=rows[1].id)
        with pytest.raises(confirmation.InvalidFactTransition):
            confirmation.confirm_fact(s, tenant_id=tid, fact_id=rows[1].id)


def test_unknown_fact_id_is_loud(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        with pytest.raises(confirmation.FactNotFound):
            confirmation.confirm_fact(
                s, tenant_id=uuid.UUID(a), fact_id=uuid.uuid4()
            )


# ── operator verification and conversation-sourced facts ────────────────────


def test_operator_verification_enters_the_bank(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        rows = _seed(s, tid)
        confirmation.operator_verify(s, tenant_id=tid, fact_id=rows[0].id)
        bank = confirmation.achievement_bank(s, tenant_id=tid)
    assert bank[0].status == "OPERATOR_VERIFIED"


def test_conversation_fact_is_customer_confirmed_by_definition(
    two_tenants: tuple[str, str],
) -> None:
    """A fact the customer states directly in the conversation needs no
    second confirmation round — they just asserted it."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        fact = confirmation.add_conversation_fact(
            s, tenant_id=tid, category="skill", payload={"name": "Tableau"}
        )
        bank = confirmation.achievement_bank(s, tenant_id=tid)
    assert fact.source == "conversation"
    assert [f.id for f in bank] == [fact.id]


# ── gaps: questions only about the missing (§05) ─────────────────────────────


def test_gaps_report_missing_essentials_only(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        assert confirmation.pending_gaps(s, tenant_id=tid) == ["experience", "skill"]
        confirmation.add_conversation_fact(
            s, tenant_id=tid, category="experience",
            payload={"title": "BA", "employer": "X"},
        )
        assert confirmation.pending_gaps(s, tenant_id=tid) == ["skill"]
        confirmation.add_conversation_fact(
            s, tenant_id=tid, category="skill", payload={"name": "SQL"}
        )
        assert confirmation.pending_gaps(s, tenant_id=tid) == []
