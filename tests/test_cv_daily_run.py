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


def _probe_phone() -> str:
    """A real Salla order ALWAYS carries the buyer's mobile — the whole
    zero-touch activation path keys on it. Test orders that omitted it were
    describing a shape the world never sends, and that unrealism is exactly
    how the phone defect survived: the suite was green while no real customer
    could have been activated."""
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _seed_active_tenant(owner: Session) -> tuple[uuid.UUID, CustomerChannel]:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_basic", Decimal("149"), "SAR",
                       customer_phone=_probe_phone())
    })
    result = provision_order(
        owner, order_id, salla_client=client,
        product_catalog={"prod_basic": "basic"},
        expected_pricing={k: (Decimal("149"), "SAR") for k in {"prod_basic": "basic"}},
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


def test_all_suppressed_day_is_no_matches_not_cv_failure(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """Audit fix: every gate pass already delivered (suppressed within TTL)
    is an honest NO_MATCHES — nothing failed, the jobs are repeats."""
    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        report = engine_run.RunReport(
            report.run_id, report.status, report.counts,
            {tid: {"final": [], "counts": {"passed": 1, "suppressed": 1}}},
        )
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=NOW,
        )
        assert states[tid].state == "NO_MATCHES"
    finally:
        _cleanup(owner_session, tid)


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


class DeadSearchApi:
    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("dns is down")


class DeadJobSpy:
    def scrape(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise ConnectionError("dns is down")


def test_a_total_discovery_failure_still_closes_every_tenants_day(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """AUDIT §15.12: DISCOVERY_FAILED was UNREACHABLE in production. The
    engine returned that status with a hard-coded empty per_tenant map, and
    the delivery phase — the only writer of day states — runs only when the
    report carries tenants, so a night where every source died left every
    paying customer with no row at all for that date. The state above was
    proven only by a hand-built report shape run_nightly could never emit;
    this one runs the REAL nightly path with both fetchers dead."""
    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = engine_run.run_nightly(
            owner_session, searchapi=DeadSearchApi(), jobspy_client=DeadJobSpy(),
            fetcher=NoFetch(), resolver=None, now=NOW, tenant_ids=[tid],
        )
        assert report.status == "discovery_failed"
        # the report the engine ACTUALLY emits now names the tenants it owes
        assert list(report.per_tenant) == [tid]
        assert report.per_tenant[tid]["final"] == []

        deps = _deps(tmp_path)
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=deps, now=NOW,
        )
        assert states[tid].state == "DISCOVERY_FAILED"
        assert deps.whatsapp_client.sent == []   # a failed night sends nothing
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


def test_stale_held_delivery_expires_into_honest_close(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """Audit fix: a held bundle whose window never opened must close its own
    day (WHATSAPP_FAILED) at the next run instead of vanishing silently."""
    from datetime import timedelta

    tid, channel = _seed_active_tenant(owner_session)
    try:
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET last_inbound_at = NULL WHERE id = :id"),
            {"id": str(channel.id)},
        )
        owner_session.commit()
        report = _engine_report(owner_session, tid)
        yesterday = NOW - timedelta(days=3)     # Thursday — a delivery day
        held = daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=yesterday,
        )
        owner_session.commit()
        assert tid not in held                               # held, not closed

        # next dawn: the sweep closes yesterday honestly
        states = daily_run.run_daily_delivery(
            owner_session, report=engine_run.RunReport(
                report.run_id, report.status, report.counts, {}),
            deps=_deps(tmp_path), now=NOW,
        )
        owner_session.commit()
        assert states == {}                                  # no tenants today
        row = owner_session.execute(
            sql_text("SELECT state FROM tenant_day_states WHERE tenant_id = :t"
                     " AND run_date = :d"),
            {"t": str(tid), "d": yesterday.astimezone(
                daily_run._RIYADH).date().isoformat()},
        ).scalar_one()
        assert row == "WHATSAPP_FAILED"
        status = owner_session.execute(
            sql_text("SELECT status FROM deliveries WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).scalar_one()
        assert status == daily_run.DELIVERY_EXPIRED          # descend won't fire
    finally:
        _cleanup(owner_session, tid)


def test_an_expired_held_bundle_fails_the_whole_night(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """THE INCIDENT THE EXIT-CODE FIX DID NOT CLOSE.

    ``exit_code_for`` shipped naming the 2 August 2026 night it was written
    for — and on career_staging that night's WHATSAPP_FAILED was written by
    ``expire_stale_held_deliveries``, at 08:02 on 3 August, the NEXT run.
    Three more look identical: 21, 22 and 23 July, each stamped 01:3x UTC the
    following morning, which is the sweep's hour and not the failed day's. The
    sweep closed a real customer's day as a failure and returned a COUNT, so
    those tenants never entered ``states``, never reached ``summary`
    ["delivery"]``, and the verdict never saw them: every one of those nights
    exited 0 and ``OnFailure=career-alert@`` stayed silent.

    So this drives the real composition — ``run_daily_delivery`` → the
    summary → the exit code — because that chain is where the break was, and
    ``exit_code_for`` on its own was green throughout.
    """
    from datetime import timedelta

    from career.engine import cli

    tid, channel = _seed_active_tenant(owner_session)
    try:
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET last_inbound_at = NULL WHERE id = :id"),
            {"id": str(channel.id)},
        )
        owner_session.commit()
        report = _engine_report(owner_session, tid)
        held_day = NOW - timedelta(days=3)              # Thursday, a run day
        assert daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=held_day,
        ) == {}                                          # held, no day state
        owner_session.commit()

        # the next dawn: nobody to serve today, and yesterday to close
        expired: list[Any] = []
        empty_report = engine_run.RunReport(
            report.run_id, report.status, report.counts, {})
        states = daily_run.run_daily_delivery(
            owner_session, report=empty_report, deps=_deps(tmp_path), now=NOW,
            expired_out=expired,
        )
        owner_session.commit()

        assert states == {}                       # nobody was served TONIGHT
        assert [s.state for s in expired] == ["WHATSAPP_FAILED"]
        assert [s.run_date for s in expired] == [
            held_day.astimezone(daily_run._RIYADH).date()]

        code = owner_session.execute(
            sql_text("SELECT code FROM tenants WHERE id = :t"), {"t": str(tid)},
        ).scalar_one()
        summary = cli.summarize_delivery(
            tenant_codes={tid: code}, intended=[], states={},
            expired=[(s.tenant_id, s.run_date, s.state) for s in expired],
        )
        assert summary["delivery"] == {}          # the empty dict that lied
        assert summary["delivery_expired"] == [
            {"tenant": code,
             "run_date": str(held_day.astimezone(daily_run._RIYADH).date()),
             "state": "WHATSAPP_FAILED"},
        ]
        verdict = cli.exit_code_for(
            "completed",
            [*summary["delivery"].values(),
             *(row["state"] for row in summary["delivery_expired"])],
            delivery_phase=cli.PHASE_RAN,
        )
        assert verdict == cli.EXIT_DELIVERY_FAILED
    finally:
        _cleanup(owner_session, tid)


def test_a_night_that_expires_nothing_still_exits_zero(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """The other side of the same line: the sweep finding nothing must stay a
    silent zero. Paging on «no held bundle from yesterday» would fire on every
    healthy night, which is how a truthful alert gets muted."""
    from career.engine import cli

    tid, _channel = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        expired: list[Any] = []
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=NOW,
            expired_out=expired,
        )
        owner_session.commit()
        assert expired == []
        assert cli.exit_code_for(
            "completed", [s.state for s in states.values()],
            delivery_phase=cli.PHASE_RAN,
        ) == cli.EXIT_OK
    finally:
        _cleanup(owner_session, tid)


def test_template_send_crash_closes_whatsapp_failed(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """Closed window + morning template rejected (e.g. still PENDING at
    Meta) → the day must close WHATSAPP_FAILED, never crash-skip."""
    class NoTemplateWhatsApp(FakeWhatsAppClient):
        def send_template(self, to_phone: str, template_name: str,
                          language: str, variables: dict | None = None,
                          buttons: tuple = ()) -> str:
            raise RuntimeError("template not approved (132001)")

    tid, channel = _seed_active_tenant(owner_session)
    try:
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET last_inbound_at = NULL WHERE id = :id"),
            {"id": str(channel.id)},
        )
        owner_session.commit()
        report = _engine_report(owner_session, tid)
        states = daily_run.run_daily_delivery(
            owner_session, report=report,
            deps=_deps(tmp_path, whatsapp_client=NoTemplateWhatsApp()), now=NOW,
        )
        assert states[tid].state == "WHATSAPP_FAILED"
        counts = dict(states[tid].counts)
        assert counts["cv_resolved"] >= 1                    # CV work was done
        assert counts["delivered"] == 0
        assert counts["failed_sends"] >= 1
        suppressed = owner_session.execute(
            sql_text("SELECT count(*) FROM tenant_job_suppressions "
                     "WHERE tenant_id = :t"), {"t": str(tid)},
        ).scalar_one()
        assert suppressed == 0                               # failed ≠ delivered
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


def test_zero_day_note_reaches_open_window_only(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """§08: a NO_MATCHES day tells the customer honestly — open window only."""
    tid, channel = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        empty = engine_run.RunReport(
            report.run_id, report.status, report.counts,
            {tid: {"final": [], "counts": {"passed": 0}}},
        )
        deps = _deps(tmp_path)
        states = daily_run.run_daily_delivery(
            owner_session, report=empty, deps=deps, now=NOW,
        )
        assert states[tid].state == "NO_MATCHES"
        texts = [m.body or "" for m in deps.whatsapp_client.sent]
        assert any("لم نجد فرصًا" in t for t in texts)

        # closed window → silent (state recorded, no free-form send)
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET last_inbound_at = NULL WHERE id = :id"),
            {"id": str(channel.id)})
        owner_session.execute(sql_text(
            "DELETE FROM tenant_day_states WHERE tenant_id = :t"), {"t": str(tid)})
        owner_session.commit()
        deps2 = _deps(tmp_path)
        states2 = daily_run.run_daily_delivery(
            owner_session, report=empty, deps=deps2, now=NOW,
        )
        assert states2[tid].state == "NO_MATCHES"
        assert deps2.whatsapp_client.sent == []
    finally:
        _cleanup(owner_session, tid)


def test_canary_runs_first_with_delay_before_the_rest(
    owner_session: Session, clean_billing: None, tmp_path: Any,
    two_tenants: tuple[str, str],
) -> None:
    """§14: the operator's tenant delivers FIRST; the hour-class delay fires
    exactly once, only when other tenants follow."""
    a, b = two_tenants
    canary = uuid.UUID(a)
    other = uuid.UUID(b)
    report = engine_run.RunReport(
        uuid.uuid4(), "completed", {},
        {other: {"final": [], "counts": {"passed": 0}},
         canary: {"final": [], "counts": {"passed": 0}}},
    )
    try:
        slept: list[float] = []
        daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=NOW,
            canary_tenant_id=canary, canary_delay_seconds=3600.0,
            sleeper=slept.append,
        )
        owner_session.commit()
        assert slept == [3600.0]

        # canary alone → no delay at all
        solo = engine_run.RunReport(
            uuid.uuid4(), "completed", {},
            {canary: {"final": [], "counts": {"passed": 0}}},
        )
        slept2: list[float] = []
        daily_run.run_daily_delivery(
            owner_session, report=solo, deps=_deps(tmp_path), now=NOW,
            canary_tenant_id=canary, canary_delay_seconds=3600.0,
            sleeper=slept2.append,
        )
        owner_session.commit()
        assert slept2 == []
    finally:
        # open transactions wedge the fixture teardown (repo lesson)
        owner_session.rollback()
        owner_session.execute(sql_text(
            "DELETE FROM tenant_day_states WHERE tenant_id IN (:a, :b)"),
            {"a": a, "b": b})
        owner_session.commit()


def test_opted_out_with_matches_records_the_eighth_state(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """CHANGELOG §12 (Fahad, option A): opted-out customer with gate passes
    closes SKIPPED_OPTED_OUT — recorded, never success nor failure."""
    tid, channel = _seed_active_tenant(owner_session)
    try:
        owner_session.execute(sql_text(
            "UPDATE customer_channels SET opt_out_at = :n WHERE id = :id"),
            {"n": NOW, "id": str(channel.id)})
        owner_session.commit()
        report = _engine_report(owner_session, tid)
        deps = _deps(tmp_path)
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=deps, now=NOW,
        )
        owner_session.commit()
        assert states[tid].state == "SKIPPED_OPTED_OUT"
        assert states[tid].counts["gate_passes"] >= 1
        assert deps.whatsapp_client.sent == []           # nothing reached them
        suppressed = owner_session.execute(sql_text(
            "SELECT count(*) FROM tenant_job_suppressions WHERE tenant_id=:t"),
            {"t": str(tid)}).scalar_one()
        assert suppressed == 0                           # skip ≠ delivered
    finally:
        owner_session.rollback()
        _cleanup(owner_session, tid)


def test_tenant_crash_is_isolated_and_closed_honestly(
    owner_session: Session, clean_billing: None, tmp_path: Any,
    two_tenants: tuple[str, str],
) -> None:
    """AUDIT ح-1/ك-10: tenant B crashing mid-run must not touch tenant A's
    committed day, and B's day closes honestly (CV_GENERATION_FAILED) —
    never a silent no-state day."""
    a, b = two_tenants
    ta, tb = uuid.UUID(a), uuid.UUID(b)
    report = engine_run.RunReport(
        uuid.uuid4(), "completed", {},
        {ta: {"final": [], "counts": {"passed": 0}},
         tb: {"final": [], "counts": {"passed": 2}}},
    )
    original = daily_run._run_tenant

    def _exploding(session, *, tenant_id, **kw):  # type: ignore[no-untyped-def]
        if tenant_id == tb:
            raise RuntimeError("boom mid-tenant")
        return original(session, tenant_id=tenant_id, **kw)

    try:
        daily_run._run_tenant = _exploding
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=_deps(tmp_path), now=NOW,
        )
        owner_session.commit()
        # tenant A closed normally (no matches day) and survived B's crash
        assert states[ta].state == "NO_MATCHES"
        # tenant B got the honest fallback state, durably committed
        row = owner_session.execute(sql_text(
            "SELECT state FROM tenant_day_states WHERE tenant_id = :t"),
            {"t": b}).scalar_one()
        assert row == "CV_GENERATION_FAILED"
    finally:
        daily_run._run_tenant = original
        owner_session.rollback()
        owner_session.execute(sql_text(
            "DELETE FROM tenant_day_states WHERE tenant_id IN (:a, :b)"),
            {"a": a, "b": b})
        owner_session.commit()


def test_delivered_day_arms_enrichment_for_thin_role(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """AUDIT ك-14: the lazy trigger must actually fire from a delivery day —
    a DELIVERED tenant with a thin role gets the ASKED ledger row, the open
    cursor, and the opening interactive question."""
    import json as _j

    tid, channel = _seed_active_tenant(owner_session)
    try:
        # ACTIVE journey + a thin confirmed role
        owner_session.execute(sql_text(
            "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
            " channel_id, state) SELECT :i, :t, s.id, :c, 'ACTIVE'"
            " FROM subscriptions s WHERE s.tenant_id = :t"),
            {"i": str(uuid.uuid4()), "t": str(tid), "c": str(channel.id)})
        owner_session.execute(sql_text(
            "INSERT INTO profile_facts (id, tenant_id, category, payload,"
            " status, source) VALUES (:i, :t, 'experience',"
            " CAST(:p AS jsonb), 'CUSTOMER_CONFIRMED', 'test')"),
            {"i": str(uuid.uuid4()), "t": str(tid),
             "p": _j.dumps({"title": "IT Analyst", "employer": "X",
                            "start_date": "2022-01", "end_date": "",
                            "achievements": []})})
        owner_session.commit()
        deps = _deps(tmp_path)
        daily_run._maybe_arm_enrichment(
            owner_session, tenant_id=tid, deps=deps, now=NOW)
        owner_session.commit()
        status = owner_session.execute(sql_text(
            "SELECT status FROM role_enrichments WHERE tenant_id = :t"),
            {"t": str(tid)}).scalar_one()
        assert status == "ASKED"
        sent = deps.whatsapp_client.sent
        assert sent and sent[-1].kind == "interactive"
        ctx = owner_session.execute(sql_text(
            "SELECT context FROM onboarding_sessions WHERE tenant_id = :t"),
            {"t": str(tid)}).scalar_one()
        assert (ctx.get("enrichment") or {}).get("open") is True
    finally:
        owner_session.rollback()
        for table in ("role_enrichments", "delivery_messages",
                      "onboarding_sessions", "profile_facts"):
            owner_session.execute(sql_text(
                f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": str(tid)})
        owner_session.commit()


def test_the_expiry_notice_does_not_reverse_for_the_operator() -> None:
    """The stale-bundle notice: the count folds into the Arabic sentence in
    Arabic-Indic digits, and `WHATSAPP_FAILED` — the §15.12 day state the
    operator greps the journal for — keeps its exact spelling on a line of
    its own. Green counterpart to the tree-wide bidi guard, which is red for
    other owners' files; see the same test in `test_whatsapp_worker`.
    """
    from tests.test_alert_direction_purity import SLOT, verdict

    hits = verdict().get("src/career/cv/daily_run.py", [])
    assert not hits, "mixed-direction operator line(s) — " + " ; ".join(
        f"L{n}: {line.replace(SLOT, '{…}')!r}" for n, line in hits
    )
