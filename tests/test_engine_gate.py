"""Per-tenant gate + decision log acceptance tests (whitepaper §06, D4/D10).

The gate composes the ported career_core authorities per tenant: role weights
derive from the approved paths (D10), the salary threshold and unknown-salary
policy come from the versioned search policy (D4: صارمة تمنع UNKNOWN؛
متوازنة تمرّره مع شركة ومستوى قويين — the legacy decide_send semantics؛
واسعة تمرّره بترتيب أدنى), location honors cities/remote/relocation, and every
job gets a decision record that answers «ليش أرسلتوها لي؟». Near-misses are
captured for the zero-day report.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.engine import gate

NOW = datetime(2026, 7, 15, 23, 0, tzinfo=UTC)

APPROVED = {"primary": "business_analyst", "secondary": "it_operations", "stretch": None}


def _policy(**kw: object) -> gate.TenantGatePolicy:
    defaults: dict[str, object] = {
        "approved_paths": APPROVED,
        "cities_ar": ("الرياض",),
        "willing_to_relocate": False,
        "remote_policy": "hybrid",
        "min_salary_sar": 10000.0,
        "unknown_salary_policy": "balanced",
        "daily_job_limit": 2,
    }
    defaults.update(kw)
    return gate.TenantGatePolicy(**defaults)  # type: ignore[arg-type]


def _posting(**kw: object) -> gate.PostingFacts:
    defaults: dict[str, object] = {
        "title": "Senior Business Analyst",
        "company": "Saudi Aramco",
        "url": "https://careers.aramco.com/j/1",
        "location": "Riyadh, Saudi Arabia",
        "jd_text": "We need a senior business analyst. Requirements gathering, "
                   "stakeholder management, 8 years of experience. ERP background.",
    }
    defaults.update(kw)
    return gate.PostingFacts(**defaults)  # type: ignore[arg-type]


# ── role weights derive from the approved paths (D10) ────────────────────────


def test_role_map_derives_from_approved_paths() -> None:
    role_map = gate.build_role_map(APPROVED)
    assert role_map["business analyst"] == 30      # primary slot weight
    assert role_map["it operations"] == 24         # secondary slot weight
    assert "مدير مشاريع" not in role_map           # unapproved family absent
    assert "محلل أعمال" in role_map                # Arabic tokens ride along


# ── the decision record answers «ليش أرسلتوها لي؟» ──────────────────────────


def test_strong_job_passes_with_a_full_record() -> None:
    verdict = gate.evaluate(_policy(), _posting())
    assert verdict.decision == "PASS"
    r = verdict.reasons
    assert r["role_score"] >= 70
    assert r["salary_status"] in ("INFERRED_HIGH", "EXPLICIT_CONFIRMED", "INFERRED_MEDIUM")
    assert r["company_tier"] in (1, 2, 3, 4)
    assert r["location"] == "PASS"
    assert isinstance(r["final_score"], int)
    assert verdict.gate_policy_version == gate.GATE_POLICY_VERSION


def test_low_role_match_blocks_and_near_misses_at_the_boundary() -> None:
    # unrelated low match (40 — probed) → plain block, no near-miss
    far = gate.evaluate(
        _policy(), _posting(title="Business Analysis Officer", jd_text=None)
    )
    assert far.decision == "BLOCK"
    assert not far.near_miss
    # calibrated near-floor match (69 vs floor 70 — probed) → near-miss (§06)
    near = gate.evaluate(
        _policy(),
        _posting(title="Senior Business Analyst",
                 jd_text="Business analysis position. SQL, Power BI."),
    )
    assert near.decision == "BLOCK"
    assert near.reasons["gate_reason"].startswith("role_match_too_low")
    assert near.near_miss
    assert any("role_match_near" in r for r in near.reasons["near_reasons"])


_RICH_JD = ("We need a senior business analyst. Requirements gathering, "
            "stakeholder management, 8 years of experience. ERP background.")


def test_explicit_salary_below_min_blocks_and_near_when_close() -> None:
    # the rich JD keeps role >= 70 (probed 73) so the salary axis decides
    close = gate.evaluate(
        _policy(min_salary_sar=10000.0),
        _posting(jd_text=_RICH_JD + " Salary: 9,200 SAR per month."),
    )
    assert close.decision == "BLOCK"
    assert close.reasons["gate_reason"] == "salary_likely_below_min"
    assert close.near_miss                       # 9200 >= 85% of 10000
    far = gate.evaluate(
        _policy(min_salary_sar=10000.0),
        _posting(jd_text=_RICH_JD + " Salary: 5,000 SAR per month."),
    )
    assert far.decision == "BLOCK"
    assert not far.near_miss


# ── D4: the three unknown-salary policies ────────────────────────────────────

# calibrated (probed): role 81, salary UNKNOWN — tier-4 company, no
# seniority tokens, governance/skills JD without salary-evidence signals
_UNKNOWN_JD = ("Business analysis for IT governance. SQL, Power BI, BPMN, "
               "requirements, SLA delivery, ITSM processes.")


def test_unknown_salary_policies() -> None:
    weak_company = _posting(
        title="Business Analyst", company="XY",
        url="https://portal.example/j/1", jd_text=_UNKNOWN_JD,
    )
    # صارمة: UNKNOWN ممنوعة مهما كانت الإشارات
    strict = gate.evaluate(_policy(unknown_salary_policy="strict"), weak_company)
    assert strict.decision == "BLOCK"
    assert strict.reasons["gate_reason"] == "unknown_salary_strict_policy"
    # متوازنة: إشارات ضعيفة → BLOCK (سلوك decide_send الموروث)
    balanced = gate.evaluate(_policy(unknown_salary_policy="balanced"), weak_company)
    assert balanced.decision == "BLOCK"
    # واسعة: تمرّ لكن بعلامة خفض ترتيب صريحة
    wide = gate.evaluate(_policy(unknown_salary_policy="wide"), weak_company)
    assert wide.decision == "PASS"
    assert wide.reasons["demoted"] is True


# ── location honors cities, remote and relocation ────────────────────────────


def test_location_gate() -> None:
    policy = _policy()  # الرياض, no relocation, hybrid
    assert gate.evaluate(policy, _posting(location="Riyadh, Saudi Arabia")).decision == "PASS"
    jeddah = gate.evaluate(policy, _posting(location="Jeddah, Saudi Arabia"))
    assert jeddah.decision == "BLOCK"
    assert jeddah.near_miss                       # موقع — near-miss (§06)
    assert gate.evaluate(policy, _posting(location="Remote")).decision == "PASS"
    relocator = _policy(willing_to_relocate=True)
    assert gate.evaluate(relocator, _posting(location="Jeddah, Saudi Arabia")).decision == "PASS"
    # a missing location is doubt, not a block (many ads omit it)
    assert gate.evaluate(policy, _posting(location=None)).decision == "PASS"
    assert gate.evaluate(policy, _posting(location=None)).reasons["location"] == "UNKNOWN"


def test_junior_title_blocks_hard() -> None:
    verdict = gate.evaluate(_policy(), _posting(title="Junior Business Analyst"))
    assert verdict.decision == "BLOCK"
    assert not verdict.near_miss


# ── persistence: the decision log rows ───────────────────────────────────────


def test_decisions_persist_per_tenant_run_job(owner_engine: Engine) -> None:
    tenant_id, run_id, posting_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    with Session(owner_engine) as s:
        s.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
                  {"id": tenant_id, "c": f"TEN-G{uuid.uuid4().hex[:4]}"})
        s.execute(sql_text("INSERT INTO discovery_runs (id, run_date) VALUES (:id, '2026-07-15')"),
                  {"id": run_id})
        s.execute(
            sql_text(
                "INSERT INTO job_postings (id, url_identity, url, title, company, source) "
                "VALUES (:id, :ident, 'https://careers.aramco.com/j/1', "
                "'Senior Business Analyst', 'Saudi Aramco', 'searchapi_google_jobs')"
            ),
            {"id": posting_id, "ident": f"joburl:v1:{uuid.uuid4().hex}{uuid.uuid4().hex[:32]}"},
        )
        s.commit()
        try:
            verdict = gate.evaluate(_policy(), _posting())
            gate.persist_decision(
                s, tenant_id=uuid.UUID(tenant_id), run_id=uuid.UUID(run_id),
                job_posting_id=uuid.UUID(posting_id), verdict=verdict,
            )
            s.commit()
            row = s.execute(
                sql_text(
                    "SELECT decision, near_miss, gate_policy_version, "
                    "reasons->>'role_score' AS role_score "
                    "FROM tenant_job_decisions WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).one()
            assert row.decision == "PASS"
            assert row.gate_policy_version == gate.GATE_POLICY_VERSION
            assert int(row.role_score) >= 70
        finally:
            s.execute(sql_text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            s.execute(sql_text("DELETE FROM discovery_runs WHERE id = :id"), {"id": run_id})
            s.execute(sql_text("DELETE FROM job_postings WHERE id = :id"), {"id": posting_id})
            s.commit()
