"""Job-card delivery acceptance tests (whitepaper §08/§09 + D2/D8) — before code.

The Arabic job card is a pure formatter; the displayed filename is D8
(FirstName LastName - Job Title.pdf, company appended on same-day
collision); the daily bundle binds each card to ITS document (visual
binding — never two cards in a row); a job whose required CV is unresolved
is EXCLUDED and counted as failed (a missing CV is never success); grouped
sending isolates failures (a failing card skips its own document only —
siblings deliver; card sent + document failed = the job FAILED).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.cv import deliver
from career.db.models import CustomerChannel
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.delivery import (
    DELIVERY_COMPLETED,
    DELIVERY_PARTIAL,
    deliver_adaptive,
)
from career.whatsapp.templates import DAILY_UTILITY

NOW = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)

JOB = {
    "title": "Senior Business Analyst",
    "company": "Alinma Digital",
    "location": "Riyadh, Saudi Arabia",
    "url": "https://careers.alinma.example/j/1?utm_source=x&utm_medium=y",
    "reasons": {"role_score": 94, "salary_status": "INFERRED_HIGH",
                "company_tier": 2},
}


# ── the Arabic card (pure) ───────────────────────────────────────────────────


def test_job_card_structure_and_labels() -> None:
    card = deliver.build_job_card(JOB, rank=1)
    assert card.splitlines()[0] == "#1 Senior Business Analyst"
    assert "🏢 Alinma Digital" in card
    assert "📍 Riyadh, Saudi Arabia" in card
    assert "🎯 المطابقة: 94/100 🟢" in card
    assert "الراتب" in card and "مرجّح مرتفع" in card
    assert "شركة قوية" in card
    # tracking params stripped from the visible link
    assert "utm_source" not in card
    assert "https://careers.alinma.example/j/1" in card


def test_job_card_score_emojis_and_missing_location() -> None:
    mid = dict(JOB, reasons={"role_score": 66, "salary_status": "UNKNOWN",
                             "company_tier": 4}, location=None)
    card = deliver.build_job_card(mid, rank=2)
    assert "66/100 🟡" in card
    assert "📍 الرياض، السعودية" in card       # documented default
    assert "غير معلن" in card
    low = dict(JOB, reasons={"role_score": 40, "salary_status": "INFERRED_LOW",
                             "company_tier": 1})
    assert "40/100 🔴" in deliver.build_job_card(low, rank=3)


# ── D8 displayed filename ────────────────────────────────────────────────────


def test_display_filename_d8() -> None:
    used: set[str] = set()
    first = deliver.display_filename(
        "Fahad Almulhim", "Business Analyst", "Acme", used=used
    )
    assert first == "Fahad Almulhim - Business Analyst.pdf"
    used.add(first)
    second = deliver.display_filename(
        "Fahad Almulhim", "Business Analyst", "Beta Bank", used=used
    )
    assert second == "Fahad Almulhim - Business Analyst - Beta Bank.pdf"


def test_display_filename_sanitizes_illegal_chars() -> None:
    name = deliver.display_filename(
        "Fahad Almulhim", 'BA / Lead: "Core"', "A|B", used=set()
    )
    assert "/" not in name and ":" not in name and '"' not in name
    assert name.endswith(".pdf")


# ── the daily bundle: binding order + honest exclusion ───────────────────────


def _jobs(n: int = 2, cv: bool = True) -> list[dict]:
    return [
        dict(JOB, url=f"https://careers.x.example/j/{i}",
             title=f"Role {i}",
             cv_key=(f"tenants/t/tailored_cvs/j{i}.pdf" if cv else None))
        for i in range(n)
    ]


def test_bundle_binds_each_card_to_its_document() -> None:
    bundle, failures = deliver.build_daily_bundle(
        _jobs(2), customer_name="Fahad Almulhim"
    )
    assert failures == []
    assert bundle["grouped"] is True
    assert "لا تقديم تلقائي" in bundle["header"]         # standing safety line
    jobs = bundle["jobs"]
    assert len(jobs) == 2
    for i, job in enumerate(jobs):
        assert job["card"]["kind"] == "text"
        assert job["document"]["kind"] == "document"
        assert job["document"]["filename"].startswith("Fahad Almulhim - ")
        assert f"Role {i}" in job["card"]["body"]


def test_bundle_excludes_cv_unresolved_jobs_as_failures() -> None:
    jobs = _jobs(3)
    jobs[1]["cv_key"] = None                             # required, unresolved
    bundle, failures = deliver.build_daily_bundle(jobs, customer_name="F A")
    assert len(bundle["jobs"]) == 2
    assert failures == [
        {"group": jobs[1]["url"], "reason": "cv_required_unresolved"}
    ]


# ── grouped sending: failure isolation (DB path through C4) ──────────────────


class FlakyWhatsApp(FakeWhatsAppClient):
    """Raises when sending a text containing the poison marker."""

    def __init__(self, poison: str) -> None:
        super().__init__()
        self._poison = poison

    def send_text(self, to_phone: str, body: str) -> str:
        if self._poison in body:
            raise RuntimeError("network blip")
        return super().send_text(to_phone, body)


def _channel(owner_session: Session) -> CustomerChannel:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR")
    })
    token = provision_order(
        owner_session, order_id, salla_client=client,
        product_catalog={"prod_pro": "professional"},
    ).activation_token
    assert token is not None
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    activate(owner_session, token=token, from_phone=phone, display_name=None,
             now=NOW, whatsapp_client=FakeWhatsAppClient(),
             admin_client=FakeTelegramAdminClient())
    ch = owner_session.execute(
        select(CustomerChannel).where(CustomerChannel.phone_e164 == phone)
    ).scalar_one()
    ch.last_inbound_at = NOW                             # window open
    owner_session.commit()
    return ch


def test_grouped_send_isolates_failures(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    ch = _channel(owner_session)
    bundle, _ = deliver.build_daily_bundle(_jobs(2), customer_name="F A")
    wa = FlakyWhatsApp(poison="Role 0")                  # first card dies
    delivery = deliver_adaptive(
        owner_session, ch, bundle, run_date=NOW.date(),
        whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW,
    )
    owner_session.commit()
    assert delivery.status == DELIVERY_PARTIAL           # honest, not COMPLETED
    kinds = [(m.kind, m.body or "") for m in wa.sent]
    # header + job2's card+document only; job1's DOCUMENT never attempted
    assert sum(1 for k, _ in kinds if k == "document") == 1
    assert not any("Role 0" in b for _, b in kinds)
    results = delivery.bundle["results"]
    assert results["failed"] == [_jobs(2)[0]["url"]]
    assert results["delivered"] == [_jobs(2)[1]["url"]]


def test_grouped_send_full_success_is_completed(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    ch = _channel(owner_session)
    bundle, _ = deliver.build_daily_bundle(_jobs(2), customer_name="F A")
    wa = FakeWhatsAppClient()
    delivery = deliver_adaptive(
        owner_session, ch, bundle, run_date=NOW.date(),
        whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW,
    )
    owner_session.commit()
    assert delivery.status == DELIVERY_COMPLETED
    kinds = [m.kind for m in wa.sent]
    # header, then card→document, card→document (visual binding order)
    assert kinds == ["text", "text", "document", "text", "document"]
    assert delivery.bundle["results"]["failed"] == []


# ── outcome events (D8 buttons → the measurement fuel) ───────────────────────


def test_record_outcome_rows_are_tenant_scoped(
    owner_session: Session, two_tenants: tuple[str, str]
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deliver.record_outcome(
        owner_session, tenant_id=tid, job_ref="https://x.example/j/1",
        outcome="applied", reason=None, now=NOW,
    )
    deliver.record_outcome(
        owner_session, tenant_id=tid, job_ref="https://x.example/j/2",
        outcome="ignored", reason="بعيدة عن تخصصي", now=NOW,
    )
    owner_session.commit()
    try:
        rows = owner_session.execute(
            sql_text("SELECT outcome, reason FROM outcome_events "
                     "WHERE tenant_id = :t ORDER BY outcome"),
            {"t": a},
        ).all()
        assert [r.outcome for r in rows] == ["applied", "ignored"]
        assert rows[1].reason == "بعيدة عن تخصصي"
    finally:
        owner_session.execute(
            sql_text("DELETE FROM outcome_events WHERE tenant_id = :t"), {"t": a}
        )
        owner_session.commit()


def test_parse_outcome_button() -> None:
    assert deliver.parse_outcome_button("applied:https://x.example/j/1") == (
        "applied", "https://x.example/j/1"
    )
    assert deliver.parse_outcome_button("ignored:https://x.example/j/2") == (
        "ignored", "https://x.example/j/2"
    )
    assert deliver.parse_outcome_button("مرحبا") is None
    assert deliver.parse_outcome_button("applied:") is None
