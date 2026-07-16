"""The funnel conversation — consents → upload → path → report (§04, C8).

Reuses the C5 authorities verbatim: the SAME consent purposes and gate, the
SAME §11 upload pipeline, the SAME extraction (PII stripped before any LLM).
The report evaluates the EXTRACTED facts as-is (the confirmation loop is the
subscription's); the PDF lands in tenant storage and goes out with the
Arabic summary and the upgrade CTA. Every reply is Arabic; every state is
resumable from the row.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, FunnelSession, ProfileFact
from career.funnel.evaluation import evaluate
from career.funnel.report import render_report_pdf, whatsapp_summary
from career.onboarding.consents import PURPOSES, record_consent
from career.onboarding.extraction import run_extraction, strip_pii
from career.onboarding.orchestrator import Deps
from career.onboarding.upload import process_cv_upload
from career.storage import tenant_key

logger = logging.getLogger("career.funnel")

STATE_CONSENT = "CONSENT_PENDING"
STATE_UPLOAD = "UPLOAD_PENDING"
STATE_PATH = "PATH_PENDING"
STATE_DONE = "DONE"

_AGREE = "أوافق"
_DECLINE = "لا أوافق"

_WELCOME = (
    "أهلًا بك في تحليل السيرة الذاتية! 📊\n"
    "خلال دقائق: ترفع سيرتك، تخبرنا بمسارك المستهدف، ويوصلك تقرير تقييم "
    "رقمي صريح مع أهم الملاحظات.\n"
    "قبل البدء نحتاج موافقتك على أغراض المعالجة — وحقوقك كاملة: السحب "
    "والتصدير والحذف في أي وقت."
)
_UPLOAD_PROMPT = (
    "ممتاز ✅ الآن أرسل سيرتك الذاتية كملف PDF أو DOCX (حتى 10MB)."
)
_PATH_PROMPT = (
    "قرأنا سيرتك بنجاح 👌\n"
    "وش المسار الوظيفي اللي تبي نقيّم ملاءمتك له؟ (مثال: محلل أعمال)"
)


def _required_purposes() -> list[Any]:
    return [p for p in PURPOSES if p.required]


def _session_for_channel(
    session: Session, channel_id: uuid.UUID
) -> tuple[FunnelSession, CustomerChannel] | None:
    channel = session.get(CustomerChannel, channel_id)
    if channel is None:
        return None
    row = session.execute(
        select(FunnelSession).where(FunnelSession.tenant_id == channel.tenant_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    return row, channel


def incomplete_funnel(session: Session, tenant_id: uuid.UUID) -> FunnelSession | None:
    row = session.execute(
        select(FunnelSession).where(FunnelSession.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if row is None or row.state == STATE_DONE:
        return None
    return row


def _prompt_consent(
    deps: Deps, channel: CustomerChannel, cursor: int
) -> None:
    purpose = _required_purposes()[cursor]
    deps.whatsapp_client.send_text(
        channel.phone_e164,
        f"{purpose.title_ar}\n{purpose.description_ar}\n\n"
        f"رد بـ«{_AGREE}» أو «{_DECLINE}».",
    )


def start_funnel(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    subscription_id: uuid.UUID,
    channel_id: uuid.UUID,
    deps: Deps,
    now: datetime,
) -> FunnelSession:
    existing = session.execute(
        select(FunnelSession).where(FunnelSession.tenant_id == tenant_id)
    ).scalar_one_or_none()
    channel = session.get(CustomerChannel, channel_id)
    if channel is None:  # activation just created it — absence is a bug
        raise LookupError(f"channel not found: {channel_id}")
    if existing is not None:
        return existing
    row = FunnelSession(
        id=uuid.uuid4(), tenant_id=tenant_id, subscription_id=subscription_id,
        channel_id=channel_id, state=STATE_CONSENT,
        context={"consent_cursor": 0}, updated_at=now,
    )
    session.add(row)
    session.flush()
    deps.whatsapp_client.send_text(channel.phone_e164, _WELCOME)
    _prompt_consent(deps, channel, 0)
    return row


def handle_funnel_text(
    session: Session, *, channel_id: uuid.UUID, text: str, deps: Deps,
    now: datetime,
) -> None:
    found = _session_for_channel(session, channel_id)
    if found is None:
        return
    row, channel = found
    body = text.strip()

    if row.state == STATE_CONSENT:
        cursor = int(row.context.get("consent_cursor", 0))
        purposes = _required_purposes()
        if body == _AGREE:
            record_consent(
                session, tenant_id=row.tenant_id,
                purpose=purposes[cursor].key, action="granted",
            )
            cursor += 1
            if cursor < len(purposes):
                row.context = {**row.context, "consent_cursor": cursor}
                _prompt_consent(deps, channel, cursor)
            else:
                row.state = STATE_UPLOAD
                deps.whatsapp_client.send_text(channel.phone_e164, _UPLOAD_PROMPT)
        elif body == _DECLINE:
            deps.whatsapp_client.send_text(
                channel.phone_e164,
                "هذه الموافقة ضرورية للتشغيل الأساسي — بدونها لا نستطيع قراءة "
                "سيرتك. حقوقك محفوظة: تسحبها وتحذف بياناتك متى شئت.",
            )
            _prompt_consent(deps, channel, cursor)
        else:
            _prompt_consent(deps, channel, cursor)

    elif row.state == STATE_UPLOAD:
        deps.whatsapp_client.send_text(channel.phone_e164, _UPLOAD_PROMPT)

    elif row.state == STATE_PATH:
        if not (2 <= len(body) <= 64):
            deps.whatsapp_client.send_text(
                channel.phone_e164,
                "اكتب المسار الوظيفي المستهدف (مثال: محلل أعمال).",
            )
            return
        _finish_report(session, row=row, channel=channel,
                       requested_path=body, deps=deps, now=now)

    row.updated_at = now
    session.flush()


def handle_funnel_document(
    session: Session, *, channel_id: uuid.UUID, media_id: str,
    filename: str | None, deps: Deps, now: datetime,
) -> None:
    found = _session_for_channel(session, channel_id)
    if found is None:
        return
    row, channel = found
    if row.state != STATE_UPLOAD:
        deps.whatsapp_client.send_text(
            channel.phone_e164, "استلمنا الملف — لكن خطوتنا الحالية مختلفة."
        )
        return

    fetched = deps.whatsapp_client.download_media(media_id)
    if fetched is None:
        deps.whatsapp_client.send_text(
            channel.phone_e164,
            "تعذر تنزيل الملف — أعد إرساله من فضلك.",
        )
        return
    data, media_filename = fetched
    upload = process_cv_upload(
        session, tenant_id=row.tenant_id, data=data,
        original_filename=filename or media_filename,
        scanner=deps.scanner, storage=deps.storage, limits=deps.upload_limits,
    )
    if upload.status != "processed":
        deps.whatsapp_client.send_text(
            channel.phone_e164,
            "ما قدرنا نقبل الملف (فحص السلامة رفضه أو الصيغة غير مدعومة) — "
            "أرسل PDF أو DOCX سليمًا من فضلك.",
        )
        return

    text_storage_key = upload.extracted_text_storage_key
    if not text_storage_key:  # processed uploads always carry it — honesty guard
        deps.whatsapp_client.send_text(
            channel.phone_e164,
            "تعذر قراءة نص الملف — أعد إرساله من فضلك.",
        )
        return
    text = deps.storage.get(text_storage_key).decode("utf-8")
    stripped = strip_pii(text, known_name=None)
    contact_found = any(k.startswith("[EMAIL_") for k in stripped.replacements)
    run_extraction(
        session, tenant_id=row.tenant_id, cv_text=text, known_name=None,
        extractor=deps.extractor,
    )
    row.context = {**row.context, "contact_found": contact_found}
    row.state = STATE_PATH
    row.updated_at = now
    session.flush()
    deps.whatsapp_client.send_text(channel.phone_e164, _PATH_PROMPT)


def _finish_report(
    session: Session, *, row: FunnelSession, channel: CustomerChannel,
    requested_path: str, deps: Deps, now: datetime,
) -> None:
    facts = list(session.execute(
        select(ProfileFact).where(ProfileFact.tenant_id == row.tenant_id)
    ).scalars().all())
    result = evaluate(
        facts, requested_path=requested_path,
        contact_found=bool(row.context.get("contact_found")),
    )

    import tempfile
    from pathlib import Path

    report_date = now.astimezone(UTC).date().isoformat()
    with tempfile.TemporaryDirectory(prefix=".funnel-") as tmp:
        pdf_path = render_report_pdf(
            result, report_date=report_date,
            output_path=Path(tmp) / "report.pdf",
        )
        pdf_bytes = pdf_path.read_bytes()
    report_key = tenant_key(
        str(row.tenant_id), "funnel_reports", f"{report_date}.pdf"
    )
    deps.storage.put(report_key, pdf_bytes, content_type="application/pdf")

    deps.whatsapp_client.send_document(
        channel.phone_e164, report_key,
        filename="تقرير تحليل السيرة.pdf",
        caption="تقريرك الكامل 📊",
    )
    deps.whatsapp_client.send_text(channel.phone_e164, whatsapp_summary(result))

    row.report = result.as_dict()
    row.state = STATE_DONE
    row.updated_at = now
    session.flush()
