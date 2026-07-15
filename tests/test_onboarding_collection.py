"""Basic-data collection acceptance tests (whitepaper §05) — before the code.

Buttons and lists, not a 30-question survey: the ten documented fields (the
relocation+remote bullet is two answers), in order, each with per-field
validation and Arabic re-prompts. The CV name must be Latin script (the CV is
English-only — LEGACY §1.9 Arabic-leak guard); the expected salary is a soft
target and skippable (D4); no national-id or date-of-birth question exists.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from career.db.session import tenant_session
from career.onboarding import collection

# ── the questions are data, in the documented order ──────────────────────────

EXPECTED_KEYS = [
    "cv_full_name",
    "city",
    "current_title",
    "years_experience",
    "notice_period_days",
    "employment_type",
    "willing_to_relocate",
    "remote_preference",
    "expected_salary_sar",
    "communication_language",
    "requested_path",
]


def test_questions_cover_the_ten_documented_fields_in_order() -> None:
    assert [q.key for q in collection.QUESTIONS] == EXPECTED_KEYS


def test_no_question_ever_asks_for_national_id_or_birth_date() -> None:
    for q in collection.QUESTIONS:
        assert "هوية" not in q.prompt_ar
        assert "ميلاد" not in q.prompt_ar
        assert q.key not in {"national_id", "date_of_birth"}


def test_interactive_questions_have_options_and_text_ones_do_not() -> None:
    for q in collection.QUESTIONS:
        if q.kind in ("buttons", "list"):
            assert q.options, q.key
            for opt in q.options:
                assert opt.id and opt.label_ar
        else:
            assert q.kind == "text"


def test_only_the_salary_question_is_skippable() -> None:
    skippable = [q.key for q in collection.QUESTIONS if q.skippable]
    assert skippable == ["expected_salary_sar"]


def test_every_question_has_arabic_prompt() -> None:
    for q in collection.QUESTIONS:
        assert q.prompt_ar.strip(), q.key


# ── cursor: first unanswered question, in order ──────────────────────────────


def test_next_question_walks_in_order_and_finishes() -> None:
    answers: dict[str, object] = {}
    seen = []
    while (q := collection.next_question(answers)) is not None:
        seen.append(q.key)
        answers[q.key] = "x"  # presence, not validity, drives the cursor
    assert seen == EXPECTED_KEYS
    assert collection.is_complete(answers)


def test_skipped_salary_counts_as_answered() -> None:
    answers = dict.fromkeys(EXPECTED_KEYS, "x")
    answers["expected_salary_sar"] = None  # skipped
    assert collection.next_question(answers) is None
    assert collection.is_complete(answers)


# ── per-field parsing and validation ─────────────────────────────────────────


def test_cv_name_rejects_arabic_script_with_helpful_reprompt() -> None:
    """The CV is English-only (LEGACY §1.9 Arabic-leak guard) — an Arabic name
    would bleed into the PDF, so it is re-asked in Latin letters."""
    with pytest.raises(collection.AnswerInvalid) as err:
        collection.parse_answer("cv_full_name", "فهد الملحم")
    assert "بالإنجليزية" in err.value.reprompt_ar
    assert collection.parse_answer("cv_full_name", "Fahad Almulhim") == "Fahad Almulhim"


def test_cv_name_rejects_empty_and_absurd_lengths() -> None:
    with pytest.raises(collection.AnswerInvalid):
        collection.parse_answer("cv_full_name", "   ")
    with pytest.raises(collection.AnswerInvalid):
        collection.parse_answer("cv_full_name", "x" * 200)


def test_city_accepts_option_id_and_free_text() -> None:
    assert collection.parse_answer("city", "riyadh") == "الرياض"
    assert collection.parse_answer("city", "حائل") == "حائل"
    with pytest.raises(collection.AnswerInvalid):
        collection.parse_answer("city", "")


def test_years_experience_parses_arabic_digits_and_bounds() -> None:
    assert collection.parse_answer("years_experience", "٧") == 7
    assert collection.parse_answer("years_experience", "12") == 12
    for bad in ("خبرة طويلة", "-3", "70"):
        with pytest.raises(collection.AnswerInvalid):
            collection.parse_answer("years_experience", bad)


def test_notice_period_buttons_map_to_days() -> None:
    assert collection.parse_answer("notice_period_days", "immediate") == 0
    assert collection.parse_answer("notice_period_days", "one_month") == 30
    with pytest.raises(collection.AnswerInvalid):
        collection.parse_answer("notice_period_days", "next_year")


def test_employment_type_options() -> None:
    assert collection.parse_answer("employment_type", "full_time") == "full_time"
    assert collection.parse_answer("employment_type", "any") == "any"
    with pytest.raises(collection.AnswerInvalid):
        collection.parse_answer("employment_type", "freelance_forever")


def test_relocation_and_remote_options() -> None:
    assert collection.parse_answer("willing_to_relocate", "yes") is True
    assert collection.parse_answer("willing_to_relocate", "no") is False
    assert collection.parse_answer("remote_preference", "hybrid") == "hybrid"
    with pytest.raises(collection.AnswerInvalid):
        collection.parse_answer("remote_preference", "moon")


def test_salary_parses_arabic_digits_separators_and_skip() -> None:
    """D4: a soft target — skippable, never a hard gate."""
    assert collection.parse_answer("expected_salary_sar", "٧٠٠٠") == Decimal("7000")
    assert collection.parse_answer("expected_salary_sar", "12,500") == Decimal("12500")
    assert collection.parse_answer("expected_salary_sar", collection.SKIP) is None
    for bad in ("مليون ريال", "0", "999", "9999999"):
        with pytest.raises(collection.AnswerInvalid):
            collection.parse_answer("expected_salary_sar", bad)


def test_language_and_path() -> None:
    assert collection.parse_answer("communication_language", "ar") == "ar"
    assert collection.parse_answer("communication_language", "en") == "en"
    assert (
        collection.parse_answer("requested_path", "محلل أعمال تقني")
        == "محلل أعمال تقني"
    )
    with pytest.raises(collection.AnswerInvalid):
        collection.parse_answer("requested_path", "")


def test_unknown_question_key_is_an_error() -> None:
    with pytest.raises(collection.UnknownQuestion):
        collection.parse_answer("favorite_color", "blue")


# ── persistence: one profile row per tenant, updated progressively ───────────


def test_apply_answer_upserts_a_single_profile_row(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        collection.apply_answer(s, tenant_id=tid, key="cv_full_name", value="Fahad A")
        collection.apply_answer(s, tenant_id=tid, key="years_experience", value=9)
    with tenant_session(a) as s:
        collection.apply_answer(s, tenant_id=tid, key="cv_full_name", value="Fahad B")

    with tenant_session(a) as s:
        rows = s.execute(
            text(
                "SELECT cv_full_name, years_experience, updated_at "
                "FROM customer_profiles"
            )
        ).all()
    assert len(rows) == 1
    name, years, updated_at = rows[0]
    assert (name, years) == ("Fahad B", 9)
    assert updated_at is not None


def test_apply_answer_rejects_unknown_key_before_db(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        with pytest.raises(collection.UnknownQuestion):
            collection.apply_answer(
                s, tenant_id=uuid.UUID(a), key="national_id", value="123"
            )
