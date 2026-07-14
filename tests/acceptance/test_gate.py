"""Acceptance tests — gate & ranking (LEGACY §4, Verification Record).

Includes the Verification Record's exact-total anchor: role match == 70 for
"IT Operations Manager" with no JD text. Deterministic rules decide
eligibility; per D10 the weights are injectable (legacy defaults asserted here).
"""

from __future__ import annotations

from career_core.gate import (
    MAX_GATE_SEND,
    classify_company_tier,
    company_quality_score,
    compute_fit_score,
    compute_role_match_score,
    decide_send,
    fit_label,
    gate_sort_key,
    source_quality_score,
)
from career_core.salary import (
    SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT,
    SALARY_GATE_FAIL_LIKELY_LOW,
    SALARY_GATE_PASS_CONFIRMED,
    SALARY_GATE_PASS_LIKELY_HIGH,
    SALARY_GATE_PASS_POSSIBLE_HIGH,
    SALARY_GATE_UNKNOWN,
)


class TestCompanyTier:
    def test_tier1_keywords(self) -> None:
        for company in ("Saudi Aramco", "NEOM Company", "SAMA", "alrajhi bank",
                        "Public Investment Fund", "وزارة الصحة"):
            tier, reason = classify_company_tier(company)
            assert tier == 1, company
            assert reason.startswith("tier1:")

    def test_tier2_keywords(self) -> None:
        for company in ("Accenture Middle East", "Deloitte", "IBM Saudi", "Mobily"):
            tier, _ = classify_company_tier(company)
            assert tier == 2, company

    def test_tier3_recruiter_signals(self) -> None:
        tier, reason = classify_company_tier("Global Talent Staffing")
        assert tier == 3
        assert reason.startswith("tier3_recruiter:")

    def test_tier4_name_too_short(self) -> None:
        assert classify_company_tier("AB") == (4, "tier4_name_too_short")

    def test_tier3_default(self) -> None:
        assert classify_company_tier("Contoso Industrial") == (3, "tier3_default")

    def test_quality_score_map(self) -> None:
        assert [company_quality_score(t) for t in (1, 2, 3, 4)] == [95, 75, 55, 25]


class TestSourceQuality:
    def test_direct_ats_90(self) -> None:
        assert source_quality_score("https://boards.greenhouse.io/x/jobs/1") == 90
        assert source_quality_score("https://acme.wd3.myworkdayjobs.com/en/j/1") == 90

    def test_known_board_65(self) -> None:
        assert source_quality_score("https://www.linkedin.com/jobs/view/1") == 65
        assert source_quality_score("https://www.bayt.com/en/job/1") == 65

    def test_aggregator_20(self) -> None:
        assert source_quality_score("https://sa.jooble.org/j/1") == 20

    def test_unknown_50_and_none_0(self) -> None:
        assert source_quality_score("https://careers.contoso.com/j/1") == 50
        assert source_quality_score("") == 0
        assert source_quality_score(None) == 0


class TestRoleMatch:
    def test_dev_titles_hard_reject(self) -> None:
        for title in ("Software Engineer", "Full Stack Developer", "DevOps Engineer",
                      "Backend Developer", "Python Developer"):
            assert compute_role_match_score(title) == 0, title

    def test_support_titles_hard_reject(self) -> None:
        for title in ("Helpdesk Agent", "Desktop Support Specialist",
                      "Service Desk Agent", "Cabling Technician"):
            assert compute_role_match_score(title) == 0, title

    def test_verification_record_anchor_exactly_70(self) -> None:
        # VR anchor: role 30 + seniority(manager) 20 + domain(it operations) 15
        # + skills 0 + bridge 5 + governance 0 = 70.
        assert compute_role_match_score("IT Operations Manager") == 70

    def test_capped_at_100(self) -> None:
        # role 30 + seniority 20 + domain 15 + skills capped 15 + bridge 10
        # + governance/delivery 10 = 100 (cap).
        jd = ("business analyst requirements user stories stakeholder uat itil pmp "
              "servicenow governance sla incident change management agile jira "
              "business process delivery reporting compliance sama strategy technology")
        assert compute_role_match_score("Technical Business Analyst Manager", jd_text=jd) == 100

    def test_sql_not_a_dev_reject(self) -> None:
        # Deliberate: BAs need SQL (LEGACY §4.6 note).
        score = compute_role_match_score("Business Analyst", jd_text="SQL required")
        assert score > 0


class TestSendBlockDecision:
    def test_junior_blocks(self) -> None:
        d = decide_send(SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT, role_match_score=95,
                        company_tier=1, cq_score=95, sq_score=90)
        assert (d.decision, d.reason) == ("BLOCK", "junior_or_support_role")

    def test_low_match_blocks_with_value(self) -> None:
        d = decide_send(SALARY_GATE_PASS_CONFIRMED, role_match_score=69,
                        company_tier=1, cq_score=95, sq_score=90)
        assert d.decision == "BLOCK"
        assert d.reason == "role_match_too_low:69"

    def test_salary_fail_blocks(self) -> None:
        d = decide_send(SALARY_GATE_FAIL_LIKELY_LOW, role_match_score=80,
                        company_tier=2, cq_score=75, sq_score=65)
        assert (d.decision, d.reason) == ("BLOCK", "salary_likely_below_min")

    def test_confirmed_non_tier4_sends(self) -> None:
        d = decide_send(SALARY_GATE_PASS_CONFIRMED, role_match_score=70,
                        company_tier=3, cq_score=55, sq_score=50)
        assert d.decision == "SEND"

    def test_tier4_needs_confirmed_and_90(self) -> None:
        ok = decide_send(SALARY_GATE_PASS_CONFIRMED, role_match_score=90,
                         company_tier=4, cq_score=25, sq_score=20)
        assert ok.decision == "SEND"
        not_conf = decide_send(SALARY_GATE_PASS_LIKELY_HIGH, role_match_score=95,
                               company_tier=4, cq_score=25, sq_score=20)
        assert not_conf.decision == "BLOCK"
        low_match = decide_send(SALARY_GATE_PASS_CONFIRMED, role_match_score=89,
                                company_tier=4, cq_score=25, sq_score=20)
        assert low_match.decision == "BLOCK"

    def test_possible_high_thresholds(self) -> None:
        ok = decide_send(SALARY_GATE_PASS_POSSIBLE_HIGH, role_match_score=90,
                         company_tier=1, cq_score=85, sq_score=50)
        assert ok.decision == "SEND"
        low_cq = decide_send(SALARY_GATE_PASS_POSSIBLE_HIGH, role_match_score=90,
                             company_tier=2, cq_score=84, sq_score=50)
        assert low_cq.decision == "BLOCK"

    def test_unknown_salary_thresholds(self) -> None:
        ok = decide_send(SALARY_GATE_UNKNOWN, role_match_score=90,
                         company_tier=1, cq_score=85, sq_score=70)
        assert ok.decision == "SEND"
        low_sq = decide_send(SALARY_GATE_UNKNOWN, role_match_score=90,
                             company_tier=1, cq_score=85, sq_score=69)
        assert low_sq.decision == "BLOCK"


class TestRanking:
    def test_sort_key_order_and_cap(self) -> None:
        jobs = [
            ("unknown", SALARY_GATE_UNKNOWN, 90, 95),
            ("confirmed-weak", SALARY_GATE_PASS_CONFIRMED, 55, 70),
            ("likely-strong", SALARY_GATE_PASS_LIKELY_HIGH, 95, 90),
            ("confirmed-strong", SALARY_GATE_PASS_CONFIRMED, 95, 92),
            ("possible", SALARY_GATE_PASS_POSSIBLE_HIGH, 85, 88),
        ]
        ranked = sorted(jobs, key=lambda j: gate_sort_key(j[1], j[2], j[3]))
        names = [j[0] for j in ranked]
        assert names == ["confirmed-strong", "confirmed-weak", "likely-strong",
                         "possible", "unknown"]
        assert MAX_GATE_SEND == 8

    def test_confirmed_low_cq_still_beats_likely_high_cq(self) -> None:
        # salary_gate_order dominates cq/match.
        a = gate_sort_key(SALARY_GATE_PASS_CONFIRMED, 25, 70)
        b = gate_sort_key(SALARY_GATE_PASS_LIKELY_HIGH, 95, 95)
        assert a < b


class TestFitScore:
    def test_dev_title_hard_reject_zero(self) -> None:
        assert compute_fit_score("React Developer", "Acme", "Riyadh").score == 0

    def test_strong_title_bonus_once_plus_signals(self) -> None:
        r = compute_fit_score("IT Operations Manager", "NEOM", "Riyadh")
        # +20 (first strong token) +10 (it operations) +6 (operations manager)
        # +3 (neom) +3 (riyadh) = 42
        assert r.score == 42
        assert r.label == "FAIR"

    def test_labels_thresholds(self) -> None:
        assert fit_label(70) == "STRONG"
        assert fit_label(69) == "GOOD"
        assert fit_label(50) == "GOOD"
        assert fit_label(49) == "FAIR"
        assert fit_label(30) == "FAIR"
        assert fit_label(29) == "LOW"

    def test_reason_first_five_signals(self) -> None:
        r = compute_fit_score(
            "IT Manager", "SAMA",
            "Riyadh governance compliance itil servicenow sla incident management",
        )
        assert len(r.reason.split(", ")) <= 5
