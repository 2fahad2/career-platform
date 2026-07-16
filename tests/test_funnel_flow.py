"""The funnel journey (§04, C8) — purchase → consents → upload → report.

The §13-C8 exit condition's first half: from a paid cv_analysis order to a
delivered Arabic report within one conversation, all through the SAME worker
entry the production webhook feeds — the funnel reuses the C5 authorities
(consents, §11 upload, extraction) verbatim and never touches the
onboarding FSM.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.onboarding import extraction, orchestrator
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.storage import FilesystemStorageAdapter
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.worker import _handle_message
from tests.test_onboarding_upload import CleanScanner, _pdf_with_text

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

FACTS = extraction.ExtractedFacts(
    experiences=[{"title": "Senior Business Analyst", "employer": "Alpha Bank",
                  "start_date": "2021-03", "end_date": "Present",
                  "description": "Requirements and UAT.",
                  "achievements": ["Cut rework by 30%."]}],
    education=[{"degree": "BSc", "field_of_study": "MIS",
                "institution": "KSU", "graduation_year": 2017}],
    certifications=[{"name": "PMI-PBA", "issuer": "PMI", "issue_date": "2022-05"}],
    skills=["SQL", "Power BI", "BPMN", "Requirements", "UAT"],
    languages=[{"language": "English", "proficiency": "Fluent"}],
    achievements=[],
)


class FakeExtractor:
    def extract(self, cv_text: str) -> extraction.ExtractedFacts:
        return FACTS


def _deps(tmp_path) -> orchestrator.Deps:
    client = FakeWhatsAppClient()
    client.media["media-9"] = (
        _pdf_with_text("Senior Business Analyst fahad@example.com"), "cv.pdf"
    )
    return orchestrator.Deps(
        whatsapp_client=client,
        scanner=CleanScanner(),
        storage=FilesystemStorageAdapter(tmp_path),
        extractor=FakeExtractor(),
    )


def _provision_funnel(owner: Session) -> str:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_cv", Decimal("29"), "SAR")
    })
    result = provision_order(
        owner, order_id, salla_client=client,
        product_catalog={"prod_cv": "cv_analysis"},
    )
    assert result.activation_token is not None
    return result.activation_token


def _msg_text(phone: str, body: str) -> dict:
    return {"id": f"wamid.{uuid.uuid4().hex}", "from": phone,
            "type": "text", "text": {"body": body}}


def _msg_doc(phone: str, media_id: str) -> dict:
    return {"id": f"wamid.{uuid.uuid4().hex}", "from": phone,
            "type": "document",
            "document": {"id": media_id, "filename": "cv.pdf"}}


def test_purchase_to_report_in_one_conversation(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()

    def say(body: str, minutes: int) -> None:
        _handle_message(
            owner_session, _msg_text(phone, body),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=minutes), onboarding=deps,
        )

    # activation routes to the FUNNEL, not the onboarding
    say(f"تفعيل {token}", 0)
    sent = [m.body or "" for m in deps.whatsapp_client.sent]
    assert any("تحليل السيرة" in b for b in sent)          # funnel welcome
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        # the three required consents
        for i in range(3):
            say("أوافق", 1 + i)
        assert any("أرسل سيرتك" in (m.body or "")
                   for m in deps.whatsapp_client.sent)

        # the CV document through the §11 pipeline
        _handle_message(
            owner_session, _msg_doc(phone, "media-9"),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=5), onboarding=deps,
        )
        assert any("المسار الوظيفي" in (m.body or "")
                   for m in deps.whatsapp_client.sent)

        # the requested path → the report lands: PDF + Arabic summary
        say("محلل أعمال", 6)
        kinds = [m.kind for m in deps.whatsapp_client.sent]
        assert "document" in kinds
        doc = [m for m in deps.whatsapp_client.sent if m.kind == "document"][-1]
        assert "funnel_reports/" in str(doc.document_ref)
        summary = deps.whatsapp_client.sent[-1].body or ""
        assert "الدرجة العامة" in summary
        assert "البحث اليومي" in summary                    # the upgrade CTA

        # the report persisted on the row; the day is DONE and resumable-safe
        row = owner_session.execute(
            sql_text("SELECT state, report FROM funnel_sessions "
                     "WHERE tenant_id = :t"), {"t": str(tenant_id)},
        ).one()
        assert row.state == "DONE"
        assert row.report["overall"] > 0
        # facts stored EXTRACTED — the upgrade inheritance raw material
        count = owner_session.execute(
            sql_text("SELECT count(*) FROM profile_facts WHERE tenant_id = :t "
                     "AND status = 'EXTRACTED'"), {"t": str(tenant_id)},
        ).scalar_one()
        assert count >= 5
    finally:
        owner_session.rollback()          # never leave a wedged transaction
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()


def test_consent_refusal_explains_and_holds(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()
    _handle_message(
        owner_session, _msg_text(phone, f"تفعيل {token}"),
        whatsapp_client=deps.whatsapp_client, admin_client=admin,
        now=NOW, onboarding=deps,
    )
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        _handle_message(
            owner_session, _msg_text(phone, "لا أوافق"),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=1), onboarding=deps,
        )
        assert any("ضرورية" in (m.body or "")
                   for m in deps.whatsapp_client.sent)
        state = owner_session.execute(
            sql_text("SELECT state FROM funnel_sessions WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar_one()
        assert state == "CONSENT_PENDING"                   # did not advance
    finally:
        owner_session.rollback()          # never leave a wedged transaction
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()


def test_rejected_file_returns_honestly(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    deps.whatsapp_client.media["media-bad"] = (b"MZ\x90\x00 not a pdf", "cv.pdf")
    admin = FakeTelegramAdminClient()
    _handle_message(
        owner_session, _msg_text(phone, f"تفعيل {token}"),
        whatsapp_client=deps.whatsapp_client, admin_client=admin,
        now=NOW, onboarding=deps,
    )
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        for i in range(3):
            _handle_message(
                owner_session, _msg_text(phone, "أوافق"),
                whatsapp_client=deps.whatsapp_client, admin_client=admin,
                now=NOW + timedelta(minutes=1 + i), onboarding=deps,
            )
        _handle_message(
            owner_session, _msg_doc(phone, "media-bad"),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=5), onboarding=deps,
        )
        assert any("ما قدرنا نقبل الملف" in (m.body or "")
                   for m in deps.whatsapp_client.sent)
        state = owner_session.execute(
            sql_text("SELECT state FROM funnel_sessions WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar_one()
        assert state == "UPLOAD_PENDING"                    # still waiting
    finally:
        owner_session.rollback()          # never leave a wedged transaction
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()
