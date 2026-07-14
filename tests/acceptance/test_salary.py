"""Acceptance tests — salary logic (LEGACY §2.1, §2.3, Verification Record).

Per DEVIATIONS D4 the threshold is injected per tenant (no global 24k): the
parsing/fail-closed behavior is identical to legacy, measured against the
tenant's expected salary.
"""

from __future__ import annotations

from career_core.salary import (
    SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT,
    SALARY_GATE_FAIL_LIKELY_LOW,
    SALARY_GATE_PASS_CONFIRMED,
    SALARY_GATE_PASS_LIKELY_HIGH,
    SALARY_GATE_PASS_POSSIBLE_HIGH,
    SALARY_GATE_UNKNOWN,
    parse_explicit_salary,
    score_salary_gate,
)


class TestExplicitParsing:
    def test_monthly_single(self) -> None:
        assert parse_explicit_salary("Salary: SAR 30,000 per month") == (30000.0, 30000.0)

    def test_annual_divided_by_12_exactly_once_decimal(self) -> None:
        # 300,000 / 12 = 25,000 exact — Decimal, no float drift
        assert parse_explicit_salary("SAR 300,000 per annum") == (25000.0, 25000.0)

    def test_annual_non_divisible_rounds_half_up(self) -> None:
        # 100,000 / 12 = 8333.333... → 8333.33
        assert parse_explicit_salary("100,000 SAR yearly") == (8333.33, 8333.33)

    def test_k_notation_per_amount(self) -> None:
        assert parse_explicit_salary("SAR 20k - 25k per month") == (20000.0, 25000.0)

    def test_range_before_single_no_double_count(self) -> None:
        # The range span must not be re-counted as two singles that contradict.
        assert parse_explicit_salary("SAR 20,000 - 25,000 per month") == (20000.0, 25000.0)

    def test_inverted_range_swapped(self) -> None:
        assert parse_explicit_salary("SAR 25,000 to 20,000 monthly") == (20000.0, 25000.0)

    def test_trailing_marker_preferred_forward_window(self) -> None:
        assert parse_explicit_salary("SAR 30,000 per month") == (30000.0, 30000.0)

    def test_leading_marker_backward_window(self) -> None:
        assert parse_explicit_salary("Monthly salary: 15,000 SAR") == (15000.0, 15000.0)

    def test_no_period_marker_fails_closed(self) -> None:
        # Magnitude never implies period (bug fba3a16).
        assert parse_explicit_salary("Salary SAR 30,000") is None

    def test_years_of_experience_never_a_salary(self) -> None:
        # "year" alone is not a period marker — phrases only.
        assert parse_explicit_salary("5+ years of experience required, SAR 30,000") is None

    def test_contradictory_representations_fail_closed(self) -> None:
        text = "Pay is SAR 30,000 per month which is 150,000 SAR per year"
        assert parse_explicit_salary(text) is None

    def test_consistent_duplicate_representations_pass(self) -> None:
        text = "SAR 24,000 per month. Compensation: SAR 24,000 monthly."
        assert parse_explicit_salary(text) == (24000.0, 24000.0)

    def test_slash_year_and_pa_markers(self) -> None:
        assert parse_explicit_salary("SAR 240,000/year") == (20000.0, 20000.0)
        assert parse_explicit_salary("SAR 240,000 p.a.") == (20000.0, 20000.0)

    def test_pcm_marker(self) -> None:
        assert parse_explicit_salary("SAR 12,000 pcm") == (12000.0, 12000.0)

    def test_empty_and_no_amounts(self) -> None:
        assert parse_explicit_salary("") is None
        assert parse_explicit_salary(None) is None
        assert parse_explicit_salary("Competitive salary") is None


class TestGateExplicitBranch:
    def test_confirmed_at_or_above_tenant_min(self) -> None:
        r = score_salary_gate(
            "Business Analyst", min_monthly_sar=7000,
            jd_text="Salary SAR 8,000 per month", company_tier=3,
        )
        assert r.outcome == SALARY_GATE_PASS_CONFIRMED
        assert r.confidence == 95

    def test_fail_below_tenant_min(self) -> None:
        r = score_salary_gate(
            "Business Analyst", min_monthly_sar=15000,
            jd_text="Salary SAR 8,000 per month", company_tier=3,
        )
        assert r.outcome == SALARY_GATE_FAIL_LIKELY_LOW
        assert r.confidence == 95

    def test_same_job_different_tenants_differ(self) -> None:
        jd = "Salary SAR 12,000 per month"
        low = score_salary_gate("Analyst", min_monthly_sar=7000, jd_text=jd, company_tier=3)
        high = score_salary_gate("Analyst", min_monthly_sar=24000, jd_text=jd, company_tier=3)
        assert low.outcome == SALARY_GATE_PASS_CONFIRMED
        assert high.outcome == SALARY_GATE_FAIL_LIKELY_LOW


class TestGateJuniorDisqualifier:
    def test_junior_title_fails_immediately(self) -> None:
        for title in ("Junior Business Analyst", "IT Helpdesk Agent", "Desktop Support",
                      "L1 Support Engineer", "Intern - IT", "Field Technician"):
            r = score_salary_gate(title, min_monthly_sar=10000, company_tier=1)
            assert r.outcome == SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT, title
            assert r.confidence == 95

    def test_junior_beats_explicit_salary(self) -> None:
        r = score_salary_gate(
            "Junior Analyst", min_monthly_sar=5000,
            jd_text="SAR 30,000 per month", company_tier=1,
        )
        assert r.outcome == SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT


class TestGateEvidenceScoring:
    def test_tier1_manager_reaches_likely_high(self) -> None:
        # tier1 (+30) + manager (+15) = 45 >= 35 → LIKELY_HIGH, conf min(85, 45)
        r = score_salary_gate("IT Operations Manager", min_monthly_sar=24000, company_tier=1)
        assert r.outcome == SALARY_GATE_PASS_LIKELY_HIGH
        assert r.confidence == 45

    def test_exec_title_points(self) -> None:
        # tier2 (+20) + exec (+20) = 40 → LIKELY_HIGH
        r = score_salary_gate("Head of Technology", min_monthly_sar=24000, company_tier=2)
        assert r.outcome == SALARY_GATE_PASS_LIKELY_HIGH

    def test_possible_high_band(self) -> None:
        # tier3 (+10) + senior (+10) = 20 → POSSIBLE_HIGH, conf min(60, 20)
        r = score_salary_gate("Senior Consultant", min_monthly_sar=24000, company_tier=3)
        assert r.outcome == SALARY_GATE_PASS_POSSIBLE_HIGH
        assert r.confidence == 20

    def test_low_band(self) -> None:
        # tier4 (+0) + analyst (+5) + regulated in company (+10) = 15 → FAIL_LIKELY_LOW
        r = score_salary_gate(
            "Analyst", min_monthly_sar=24000, company="Regional Bank", company_tier=4,
        )
        assert r.outcome == SALARY_GATE_FAIL_LIKELY_LOW

    def test_unknown_band(self) -> None:
        # tier4 (+0) + weak title (+5) = 5 → UNKNOWN, conf 0, est None
        r = score_salary_gate("Coordinator Advisor", min_monthly_sar=24000, company_tier=4)
        assert r.outcome == SALARY_GATE_UNKNOWN
        assert r.confidence == 0
        assert r.estimated_range is None

    def test_jd_signals_and_years(self) -> None:
        # tier3 (+10) + analyst/advisor (+5) + team-of (+8) + years>=7 (+10) = 33 → POSSIBLE
        jd = "Lead a team of 5. Requires 8 years experience."
        r = score_salary_gate("Advisor", min_monthly_sar=24000, jd_text=jd, company_tier=3)
        assert r.outcome == SALARY_GATE_PASS_POSSIBLE_HIGH

    def test_aggregator_source_penalty(self) -> None:
        # tier3 (+10) + senior (+10) = 20; aggregator (-5) = 15 → FAIL_LIKELY_LOW
        r = score_salary_gate(
            "Senior Specialist", min_monthly_sar=24000, company_tier=3,
            source_quality_score=20,
        )
        assert r.outcome == SALARY_GATE_FAIL_LIKELY_LOW

    def test_estimated_range_scales_with_tenant_min(self) -> None:
        # Legacy bands were calibrated to 24k (24_000–35_000); per D4 the range
        # scales with the injected min so a 7k tenant gets a 7k-relative band.
        r24 = score_salary_gate("IT Manager", min_monthly_sar=24000, company_tier=1)
        r7 = score_salary_gate("IT Manager", min_monthly_sar=7000, company_tier=1)
        assert r24.estimated_range == (24000, 35000)
        assert r7.estimated_range is not None
        assert r7.estimated_range[0] == 7000
        assert r7.estimated_range[1] < r24.estimated_range[1]
