"""Acceptance tests — complete-sentence summary enforcement (LEGACY §1.6, DD-CV-06).

Never cut mid-sentence, never append "...". Retained text is always a truthful
verbatim subset; a failing summary blocks BEFORE any file is written.
"""

from __future__ import annotations

from career_core.sentences import (
    enforce_complete_summary,
    split_complete_sentences,
    validate_summary_quality,
)


class TestSplitting:
    def test_basic_split(self) -> None:
        assert split_complete_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]

    def test_decimal_not_a_boundary(self) -> None:
        assert split_complete_sentences("Improved uptime to 99.9 percent. Done.") == [
            "Improved uptime to 99.9 percent.", "Done.",
        ]

    def test_abbreviation_not_a_boundary(self) -> None:
        sentences = split_complete_sentences("Worked with tools e.g. Jira daily. Led teams.")
        assert sentences == ["Worked with tools e.g. Jira daily.", "Led teams."]

    def test_trailing_fragment_dropped(self) -> None:
        assert split_complete_sentences("Complete sentence. Dangling fragment without end") == [
            "Complete sentence.",
        ]


class TestEnforcement:
    def test_short_summary_unchanged(self) -> None:
        s = "Experienced analyst. Delivered projects."
        assert enforce_complete_summary(s, max_chars=400) == s

    def test_keeps_only_complete_sentences_within_budget(self) -> None:
        s = ("First sentence about delivery excellence. " * 5).strip()  # ~215 chars
        out = enforce_complete_summary(s, max_chars=100)
        two = "First sentence about delivery excellence. First sentence about delivery excellence."
        assert out == two[:len(out)]
        assert out.endswith(".")
        assert len(out) <= 100
        assert "..." not in out

    def test_never_appends_ellipsis(self) -> None:
        s = "A" * 500 + ". Short tail."
        out = enforce_complete_summary(s, max_chars=400)
        assert not out.endswith("...")
        assert not out.endswith("…")

    def test_first_sentence_too_long_picks_shortest_fitting(self) -> None:
        s = ("This opening sentence is deliberately made far too long to fit the budget "
             "because it keeps going and going without any early terminator at all. Short one.")
        out = enforce_complete_summary(s, max_chars=40)
        assert out == "Short one."

    def test_nothing_fits_returns_original_for_gate_to_block(self) -> None:
        s = "B" * 450 + "."
        assert enforce_complete_summary(s, max_chars=400) == s

    def test_non_string_returns_empty(self) -> None:
        assert enforce_complete_summary(None, max_chars=400) == ""
        assert enforce_complete_summary(123, max_chars=400) == ""


class TestQualityGate:
    def test_valid_summary(self) -> None:
        ok, reason = validate_summary_quality("Delivered major projects on time.")
        assert (ok, reason) == (True, None)

    def test_missing(self) -> None:
        assert validate_summary_quality(None) == (False, "summary_missing")
        assert validate_summary_quality("   ") == (False, "summary_missing")

    def test_not_string(self) -> None:
        assert validate_summary_quality(42) == (False, "summary_not_string")

    def test_too_long(self) -> None:
        ok, reason = validate_summary_quality("x" * 401 + ".", max_chars=400)
        assert (ok, reason) == (False, "summary_too_long")

    def test_trailing_ellipsis_unicode_and_ascii(self) -> None:
        assert validate_summary_quality("Did things…") == (False, "summary_trailing_ellipsis")
        assert validate_summary_quality("Did things...") == (False, "summary_trailing_ellipsis")

    def test_incomplete_terminal(self) -> None:
        assert validate_summary_quality("No terminator here") == (
            False, "summary_incomplete_terminal",
        )

    def test_quote_wrapped_terminal_ok(self) -> None:
        ok, reason = validate_summary_quality('Led the "transformation program."')
        assert (ok, reason) == (True, None)

    def test_only_quotes_incomplete(self) -> None:
        assert validate_summary_quality('""') == (False, "summary_incomplete_terminal")

    def test_dangling_connector(self) -> None:
        # The exact legacy failure shape: "…SAR <AMOUNT> and…" — truncated at a
        # connector then terminated.
        assert validate_summary_quality("Saved costs and.") == (
            False, "summary_dangling_connector",
        )
        assert validate_summary_quality("Worked with.") == (
            False, "summary_dangling_connector",
        )
