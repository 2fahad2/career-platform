"""Worker ↔ orchestrator wiring tests — raw Meta webhook payloads end-to-end.

With onboarding deps injected, a successful activation hands over to the
journey (first consent prompt goes out); subsequent texts route to the
orchestrator instead of the delivery descend; documents route to the upload
pipeline. Without deps (None), the worker behaves exactly as C4 shipped —
the existing C4 worker tests keep guarding that path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent
from career.onboarding import orchestrator
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.storage import FilesystemStorageAdapter
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.worker import process_pending_whatsapp
from tests.test_onboarding_extraction import _FakeAnthropic  # noqa: F401 — idiom parity
from tests.test_onboarding_orchestrator import FakeExtractor
from tests.test_onboarding_upload import CleanScanner, _pdf_with_text

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
CATALOG = {"prod_basic": "basic"}


def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "messages": messages, "statuses": [],
    }}]}]}


def _insert_event(owner_session: Session, payload: dict[str, Any]) -> None:
    owner_session.add(WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="messages",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload=payload, processing_status="received",
    ))
    owner_session.commit()


def _provision_token(owner_session: Session) -> tuple[str, str]:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_basic", Decimal("149.00"), "SAR")
    })
    result = provision_order(
        owner_session, order_id, salla_client=client, product_catalog=CATALOG
    )
    assert result.activation_token is not None and result.tenant_id is not None
    return result.activation_token, result.tenant_id


def _deps(tmp_path) -> orchestrator.Deps:
    client = FakeWhatsAppClient()
    client.media["media-cv"] = (_pdf_with_text("Business Analyst at Acme"), "cv.pdf")
    return orchestrator.Deps(
        whatsapp_client=client,
        scanner=CleanScanner(),
        storage=FilesystemStorageAdapter(tmp_path),
        extractor=FakeExtractor(),
    )


def _run(owner_session: Session, deps: orchestrator.Deps | None,
         wa: FakeWhatsAppClient, now: datetime = NOW) -> None:
    process_pending_whatsapp(
        owner_session, whatsapp_client=wa,
        admin_client=FakeTelegramAdminClient(), now=now, onboarding=deps,
    )


def test_activation_hands_over_to_the_journey(
    owner_engine, tmp_path, clean_billing
) -> None:
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    wa = deps.whatsapp_client
    with Session(owner_engine) as s:
        token, tenant_id = _provision_token(s)
        _insert_event(s, _payload([{
            "id": f"wamid-{uuid.uuid4()}", "from": phone, "type": "text",
            "text": {"body": f"تفعيل {token}"},
        }]))
        _run(s, deps, wa)  # type: ignore[arg-type]

        journey = s.execute(
            sql_text("SELECT state FROM onboarding_sessions WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        ).scalar_one()
        assert journey == "CONSENT_PENDING"
    bodies = [m.body or "" for m in wa.sent]  # type: ignore[attr-defined]
    assert any("تم التفعيل" in b for b in bodies)        # C4 welcome preserved
    assert any("التشغيل الأساسي" in b for b in bodies)   # journey took over


def test_texts_route_to_the_journey_not_the_descend(
    owner_engine, tmp_path, clean_billing
) -> None:
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    wa = deps.whatsapp_client
    with Session(owner_engine) as s:
        token, tenant_id = _provision_token(s)
        _insert_event(s, _payload([{
            "id": f"wamid-{uuid.uuid4()}", "from": phone, "type": "text",
            "text": {"body": f"تفعيل {token}"},
        }]))
        _run(s, deps, wa)  # type: ignore[arg-type]
        _insert_event(s, _payload([{
            "id": f"wamid-{uuid.uuid4()}", "from": phone, "type": "text",
            "text": {"body": "أوافق"},
        }]))
        _run(s, deps, wa)  # type: ignore[arg-type]
        consents = s.execute(
            sql_text(
                "SELECT count(*) FROM consent_events "
                "WHERE action='granted' AND tenant_id = :tid"
            ),
            {"tid": tenant_id},
        ).scalar_one()
    assert consents == 3  # one tap grants all three purposes (CHANGELOG §10)


def test_document_routes_to_the_upload_pipeline(
    owner_engine, tmp_path, clean_billing
) -> None:
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    wa = deps.whatsapp_client
    with Session(owner_engine) as s:
        token, tenant_id = _provision_token(s)
        _insert_event(s, _payload([{
            "id": f"wamid-{uuid.uuid4()}", "from": phone, "type": "text",
            "text": {"body": f"تفعيل {token}"},
        }]))
        _run(s, deps, wa)  # type: ignore[arg-type]
        # fast-forward: grant consents directly, park at upload
        for p in ("basic_processing", "external_providers", "daily_messages"):
            s.execute(
                sql_text(
                    "INSERT INTO consent_events (id, tenant_id, purpose, action) "
                    "VALUES (:id, :tid, :p, 'granted')"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tenant_id), "p": p},
            )
        s.execute(
            sql_text(
                "UPDATE onboarding_sessions SET state='CV_UPLOAD_PENDING', "
                "context='{}'::jsonb WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        s.commit()
        _insert_event(s, _payload([{
            "id": f"wamid-{uuid.uuid4()}", "from": phone, "type": "document",
            "document": {"id": "media-cv", "filename": "cv.pdf",
                         "mime_type": "application/pdf"},
        }]))
        _run(s, deps, wa)  # type: ignore[arg-type]
        state = s.execute(
            sql_text("SELECT state FROM onboarding_sessions WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        ).scalar_one()
        uploads = s.execute(
            sql_text("SELECT status FROM cv_uploads WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        ).scalars().all()
    assert state == "PROFILE_CONFIRMATION"
    assert uploads == ["processed"]
