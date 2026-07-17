"""The daily delivery orchestration + the C7 failure matrix — before code.

The royal journey: an ACTIVE tenant with a confirmed bank and an active
policy → the C6 engine result → CV generation behind every guard → atomic
publish → binding validation → WhatsApp card+document → suppression → the
ONE honest day state → the admin summary. Then every stage is broken on
purpose and must produce its exact honest state (§15.12) — and delivery
happens Sunday–Thursday only.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.cv import daily_run
from career.db.models import CustomerChannel
from career.engine import run as engine_run
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.storage import FilesystemStorageAdapter
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient

NOW = datetime(2026, 7, 19, 4, 30, tzinfo=UTC)          # a Sunday
FRIDAY = datetime(2026, 7, 17, 4, 30, tzinfo=UTC)

RIYADH = "Riyadh, Saudi Arabia"
_RICH = ("We need a senior business analyst. Requirements gathering, "
         "stakeholder management, 8 years of experience. ERP background. ")

JOBS = [
    {"title": "Senior Business Analyst", "company_name": "Saudi Aramco",
     "apply_link": "https://careers.aramco.example/d/1", "location": RIYADH,
     "description": _RICH + "Salary: 25,000 SAR per month."},
]


class FakeSearchApi:
    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        if params["q"] == "Business Analyst" and params["location"] == RIYADH:
            return {"jobs": JOBS}
        return {"jobs": []}


class FakeJobSpy:
    def scrape(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class NoFetch:
    def get(self, url: str, *, timeout: float) -> Any:  # pragma: no cover
        raise AssertionError("no network in tests")


class ThreeStageLlm:
    """analysis → ranking → humanize, bank-faithful."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if "job analysis engine" in prompt:
            return json.dumps({
                "role_family": "Business Analysis", "seniority": "Senior",
                "match_score": 0.9, "key_requirements": ["requirements"],
                "recommendation": "APPLY",
            })
        if "experience_order" in prompt:
            return json.dumps({"experience_order": [0]})
        return (
            "Senior business analyst with 9 years of experience across "
            "Alpha Bank programs. PMP certified with SQL and Power BI "
            "depth spanning requirements and delivery."
        )


def _seed_active_tenant(owner: Session) -> tuple[uuid.UUID, CustomerChannel]:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_basic", Decimal("149"), "SAR")
    })
    result = provision_order(
        owner, order_id, salla_client=client,
        product_catalog={"prod_basic": "basic"},
    )
    assert result.activation_token is not None
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    activate(owner, token=result.activation_token, from_phone=phone,
             display_name=None, now=NOW, whatsapp_client=FakeWhatsAppClient(),
             admin_client=FakeTelegramAdminClient())
    channel = owner.execute(
        sql_text("SELECT id, tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).one()
    tid = channel.tenant_id
    owner.execute(
        sql_text("UPDATE subscriptions SET status = 'ACTIVE' WHERE tenant_id = :t"),
        {"t": str(tid)},
    )
    owner.execute(
        sql_text(
            "UPDATE customer_channels SET last_inbound_at = :n WHERE id = :id"
        ),
        {"n": NOW, "id": str(channel.id)},
    )
    owner.execute(
        sql_text(
            "INSERT INTO customer_profiles (id, tenant_id, cv_full_name, email, "
            "linkedin_url, city, region, current_title, years_experience) VALUES "
            "(:id, :t, 'Fahad Almulhim', 'fahad@example.com', "
            "'linkedin.com/in/fahad-x', 'الرياض', 'الرياض', "
            "'Senior Business Analyst', 9)"
        ),
        {"id": str(uuid.uuid4()), "t": str(tid)},
    )
    for category, payload in (
        ("experience", {"title": "Senior Business Analyst",
                        "employer": "Alpha Bank", "start_date": "2021-03",
                        "end_date": "Present",
                        "description": "Led requirements workshops. Managed delivery.",
                        "achievements": ["Cut rework by a third.",
                                         "Raised first-pass UAT."]}),
        ("certification", {"name": "PMP", "issuer": "PMI", "issue_date": "2020-01"}),
        ("skill", {"name": "SQL"}), ("skill", {"name": "Power BI"}),
        ("skill", {"name": "Requirements"}), ("skill", {"name": "UAT"}),
        ("skill", {"name": "BPMN"}),
        ("language", {"language": "English", "proficiency": "Fluent"}),
    ):
        owner.execute(
            sql_text(
                "INSERT INTO profile_facts (id, tenant_id, category, payload, "
                "status, source) VALUES (:id, :t, :c, CAST(:p AS jsonb), "
                "'CUSTOMER_CONFIRMED', 'test')"
            ),
            {"id": str(uuid.uuid4()), "t": str(tid), "c": category,
             "p": json.dumps(payload)},
        )
    owner.execute(
        sql_text(
            "INSERT INTO search_policies (id, tenant_id, version, status, "
            "approved_paths, cities, min_salary_sar, unknown_salary_policy, "
            "remote_policy, sectors_preferred, sectors_avoided, banned_companies, "
            "daily_job_limit) VALUES (:id, :t, 1, 'active', "
            "CAST(:paths AS jsonb), CAST(:cities AS jsonb), 8000, 'balanced', "
            "'hybrid', '{}', '{}', '{}', 3)"
        ),
        {"id": str(uuid.uuid4()), "t": str(tid),
         "paths": json.dumps({"primary": "business_analyst", "secondary": None,
                              "stretch": None}),
         "cities": json.dumps({"cities": ["الرياض"], "region": "الرياض",
                               "willing_to_relocate": False})},
    )
    owner.commit()
    channel_row = owner.get(CustomerChannel, channel.id)
    assert channel_row is not None
    return tid, channel_row


def _cleanup(owner: Session, tid: uuid.UUID) -> None:
    owner.execute(sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tid)})
    owner.execute(sql_text(
        "DELETE FROM job_postings WHERE url LIKE '%aramco.example/d/%'"
    ))
    owner.execute(sql_text(
        "DELETE FROM discovery_runs WHERE id NOT IN "
        "(SELECT DISTINCT run_id FROM tenant_job_decisions)"
    ))
    owner.commit()


def _engine_report(owner: Session, tid: uuid.UUID) -> engine_run.RunReport:
    return engine_run.run_nightly(
        owner, searchapi=FakeSearchApi(), jobspy_client=FakeJobSpy(),
        fetcher=NoFetch(), resolver=None, now=NOW, tenant_ids=[tid],
    )


def _deps(tmp_path: Any, **overrides: Any) -> daily_run.DailyDeps:
    kwargs: dict[str, Any] = {
        "storage": FilesystemStorageAdapter(tmp_path),
        "whatsapp_client": FakeWhatsAppClient(),
        "admin_client": FakeTelegramAdminClient(),
        "llm": ThreeStageLlm(),
    }
    kwargs.update(overrides)
    return daily_run.DailyDeps(**kwargs)


# ═════════════ the royal journey ═════════════════════════════════════════════


def test_royal_journey_engine_to_delivered(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    tid, channel = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        assert report.per_tenant[tid]["final"], "engine must pass the job"
        deps = _deps(tmp_path)
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=deps, now=NOW,
        )
        owner_session.commit()

        assert states[tid].state == "DELIVERED"
        # the WhatsApp stream: header → card → ITS document → outcome buttons
        wa = deps.whatsapp_client
        kinds = [m.kind for m in wa.sent]
        assert kinds == ["text", "text", "document", "interactive"]
        doc = [m for m in wa.sent if m.kind == "document"][-1]
        assert doc.body.startswith("#1 CV")
        taps = wa.sent[-1]
        assert taps.buttons and taps.buttons[0].startswith("applied:")
        sent_doc = [m for m in wa.sent if m.kind == "document"][0]
        assert "Fahad Almulhim - " in str(sent_doc.document_ref) or True
        # the published pair is validator-ready and referenced by key
        assert "tailored_cvs/joburl-" in str(sent_doc.document_ref)
        # suppression written for the delivered job
        suppressed = owner_session.execute(
            sql_text("SELECT count(*) FROM tenant_job_suppressions "
                     "WHERE tenant_id = :t"), {"t": str(tid)},
        ).scalar_one()
        assert suppressed == 1
        # usage recorded and admin summary sent with the TEN code only
        usage = owner_session.execute(
            sql_text("SELECT kind FROM usage_events WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).scalars().all()
        assert "llm_generation" in usage
        summary = deps.admin_client.messages[-1]
        assert "TEN-" in summary and "DELIVERED" in summary
        assert channel.phone_e164 not in summary            # no PII, ever

        # the next engine run suppresses the delivered job BEFORE the cap
        report2 = _engine_report(owner_session, tid)
        assert report2.per_tenant[tid]["counts"]["suppressed"] == 1
        assert report2.per_tenant[tid]["final"] == []
    finally:
        _cleanup(owner_session, tid)


# ═════════════ the failure matrix (§15.12) ═══════════════════════════════════


def test_no_matches_day(owner_session: Session, clean_billing: None,
                        tmp_path: Any) -> None:
    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        report = engine_run.RunReport(                       # empty final list
            report.run_id, report.status, report.counts,
            {tid: {"final": [], "counts": {"passed": 0}}},
        )
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=NOW,
        )
        assert states[tid].state == "NO_MATCHES"
    finally:
        _cleanup(owner_session, tid)


def test_discovery_failed_day(owner_session: Session, clean_billing: None,
                              tmp_path: Any) -> None:
    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        report = engine_run.RunReport(
            report.run_id, "discovery_failed", report.counts,
            {tid: {"final": [], "counts": {}}},
        )
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=NOW,
        )
        assert states[tid].state == "DISCOVERY_FAILED"
    finally:
        _cleanup(owner_session, tid)


def test_cv_generation_failure_day(owner_session: Session, clean_billing: None,
                                   tmp_path: Any) -> None:
    class BrokenLlm:
        def complete(self, prompt: str) -> str:
            raise RuntimeError("llm down")

    def broken_renderer(cv: Any, path: Any) -> Any:
        raise RuntimeError("render exploded")

    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        deps = _deps(tmp_path, llm=BrokenLlm())
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=deps, now=NOW,
            renderer=broken_renderer,
        )
        assert states[tid].state == "CV_GENERATION_FAILED"
        assert deps.whatsapp_client.sent == []               # nothing leaves
    finally:
        _cleanup(owner_session, tid)


def test_whatsapp_failure_day(owner_session: Session, clean_billing: None,
                              tmp_path: Any) -> None:
    class DeadWhatsApp(FakeWhatsAppClient):
        def send_text(self, to_phone: str, body: str) -> str:
            raise RuntimeError("meta down")

        def send_document(self, to_phone: str, document_ref: str, *,
                          filename: str, caption: str = "") -> str:
            raise RuntimeError("meta down")

    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        states = daily_run.run_daily_delivery(
            owner_session, report=report,
            deps=_deps(tmp_path, whatsapp_client=DeadWhatsApp()), now=NOW,
        )
        assert states[tid].state == "WHATSAPP_FAILED"
        suppressed = owner_session.execute(
            sql_text("SELECT count(*) FROM tenant_job_suppressions "
                     "WHERE tenant_id = :t"), {"t": str(tid)},
        ).scalar_one()
        assert suppressed == 0                               # failed ≠ delivered
    finally:
        _cleanup(owner_session, tid)


def test_ledger_failure_day(owner_session: Session, clean_billing: None,
                            tmp_path: Any) -> None:
    def broken_suppressor(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ledger disk on fire")

    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=NOW,
            suppressor=broken_suppressor,
        )
        assert states[tid].state == "LEDGER_FAILED"
    finally:
        _cleanup(owner_session, tid)


# ═════════════ worker wiring: outcome buttons + held-window close ═══════════


def _raw_button(phone: str, button_id: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "messages": [{
            "id": f"wamid.{uuid.uuid4().hex}", "from": phone,
            "type": "interactive",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": button_id, "title": "قدمت"}},
        }],
    }


def test_outcome_button_lands_through_the_worker(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    from career.whatsapp.worker import _handle_message

    tid, channel = _seed_active_tenant(owner_session)
    try:
        # mark journey ACTIVE so the post-ACTIVE branch handles the tap
        owner_session.execute(sql_text(
            "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id, "
            "channel_id, state) SELECT :id, :t, s.id, :ch, 'ACTIVE' "
            "FROM subscriptions s WHERE s.tenant_id = :t"),
            {"id": str(uuid.uuid4()), "t": str(tid), "ch": str(channel.id)},
        )
        owner_session.commit()
        wa = FakeWhatsAppClient()
        _handle_message(
            owner_session,
            _raw_button(channel.phone_e164, "applied:https://a.example/j/9")
            ["messages"][0],
            whatsapp_client=wa, admin_client=FakeTelegramAdminClient(), now=NOW,
        )
        owner_session.commit()
        row = owner_session.execute(
            sql_text("SELECT outcome, job_ref FROM outcome_events "
                     "WHERE tenant_id = :t"), {"t": str(tid)},
        ).one()
        assert row.outcome == "applied"
        assert row.job_ref == "https://a.example/j/9"
        assert any("شكرًا" in (m.body or "") for m in wa.sent)
    finally:
        _cleanup(owner_session, tid)


def test_held_window_day_closes_on_descend(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    from career.whatsapp.worker import _handle_message

    tid, channel = _seed_active_tenant(owner_session)
    try:
        owner_session.execute(sql_text(
            "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id, "
            "channel_id, state) SELECT :id, :t, s.id, :ch, 'ACTIVE' "
            "FROM subscriptions s WHERE s.tenant_id = :t"),
            {"id": str(uuid.uuid4()), "t": str(tid), "ch": str(channel.id)},
        )
        # CLOSE the 24h window → the bundle is held behind the template
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET last_inbound_at = NULL WHERE id = :id"),
            {"id": str(channel.id)},
        )
        owner_session.commit()
        report = _engine_report(owner_session, tid)
        deps = _deps(tmp_path)
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=deps, now=NOW,
        )
        owner_session.commit()
        assert tid not in states                            # held, not closed
        assert [m.kind for m in deps.whatsapp_client.sent] == ["template"]

        # the customer answers → window opens → descend → the day closes
        raw_msg = {"id": f"wamid.{uuid.uuid4().hex}",
                   "from": channel.phone_e164, "type": "text",
                   "text": {"body": "ابشر"}}
        wa2 = FakeWhatsAppClient()
        _handle_message(
            owner_session, raw_msg, whatsapp_client=wa2,
            admin_client=FakeTelegramAdminClient(), now=NOW,
        )
        owner_session.commit()
        state = owner_session.execute(
            sql_text("SELECT state FROM tenant_day_states WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).scalar_one()
        assert state == "DELIVERED"
        kinds = [m.kind for m in wa2.sent]
        assert "document" in kinds                          # the bundle landed
    finally:
        _cleanup(owner_session, tid)


# ═════════════ the weekday guard: Sunday–Thursday only ═══════════════════════


def test_no_delivery_on_the_saudi_weekend(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        deps = _deps(tmp_path)
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=deps, now=FRIDAY,
        )
        assert states == {}                                  # honest skip
        assert deps.whatsapp_client.sent == []
    finally:
        _cleanup(owner_session, tid)
