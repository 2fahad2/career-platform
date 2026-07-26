"""Live-bug regression: channel lookups must tolerate the «+» difference."""

from __future__ import annotations

from career.whatsapp.phones import phone_variants, same_phone


def test_variants_cover_both_spellings() -> None:
    v = phone_variants("+966569456456")
    assert "966569456456" in v and "+966569456456" in v


def test_variants_from_bare_digits() -> None:
    v = phone_variants("966569456456")
    assert "+966569456456" in v and "966569456456" in v


def test_empty_input_matches_nothing() -> None:
    assert phone_variants(None) == []
    assert phone_variants("") == []
    assert phone_variants("+") == []


def test_same_phone_ignores_plus() -> None:
    assert same_phone("+966500000000", "966500000000")
    assert not same_phone("+966500000000", "966500000001")
    assert not same_phone(None, "966500000000")
