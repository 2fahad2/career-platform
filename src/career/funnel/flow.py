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

from career.db.models import (
    CustomerChannel,
    FunnelSession,
    ProfileFact,
    SupportEvent,
    Tenant,
)
from career.funnel.evaluation import evaluate
from career.funnel.report import render_report_pdf, whatsapp_summary
from career.onboarding.consents import PURPOSES, missing_required, record_consent
from career.onboarding.consents import _load_records as _consent_records
from career.onboarding.extraction import (
    ExtractionFailed,
    PiiLeak,
    infer_header_name,
    run_extraction,
    strip_pii,
)
from career.onboarding.orchestrator import (
    CONSENT_ACK,
    CONSENT_AGREE,
    CONSENT_AGREE_ID,
    CONSENT_DECLINE,
    CONSENT_DECLINE_ID,
    Deps,
    classify_consent_reply,
)
from career.onboarding.upload import process_cv_upload
from career.storage import tenant_key
from career.support import MUTING_STATUSES, OPEN

logger = logging.getLogger("career.funnel")

STATE_CONSENT = "CONSENT_PENDING"
STATE_UPLOAD = "UPLOAD_PENDING"
STATE_PATH = "PATH_PENDING"
STATE_DONE = "DONE"

_AGREE = "أوافق"
_DECLINE = "لا أوافق"

#: Real interactive buttons, the same shape onboarding has always sent at the
#: same gate. AUDIT 2026-08: the funnel got the copy WITHOUT the buttons and
#: asked the customer to type an Arabic word with a hamza on it, then compared
#: what arrived byte-for-byte. Both button ids and the typed words resolve.
_CONSENT_BUTTONS: tuple[tuple[str, str], ...] = (
    (CONSENT_AGREE_ID, _AGREE),
    (CONSENT_DECLINE_ID, _DECLINE),
)

#: How many unresolved replies before a human is put on it — unreadable ones
#: and refusals alike, since both leave the customer standing at the gate.
#: The gate is the FIRST step of a service the customer has already paid for:
#: walling them in silently past this point is not an option we get to keep.
_MAX_CONSENT_ATTEMPTS = 3
_CONSENT_ATTEMPTS_KEY = "consent_attempts"
CONSENT_STUCK_KIND = "funnel_consent_stuck"

_CONSENT_DECLINED_EXPLAIN = (
    "هذه الموافقة ضرورية للتشغيل الأساسي — بدونها لا نستطيع قراءة "
    "سيرتك. حقوقك محفوظة: تسحبها وتحذف بياناتك متى شئت."
)
#: First miss: a short, honest nudge — not the identical wall a second time.
_CONSENT_NOT_CLEAR = "أحتاج ردّك على الموافقة عشان نكمل 🙏"
#: An acknowledgement («تمام»، «أوك») is not a consent. Say what is missing.
_CONSENT_ACK_ASK = (
    "أبشر 👌\n"
    "بس أحتاج كلمة الموافقة صريحة عشان تنحفظ في سجل موافقاتك"
)
#: Second miss: stop repeating, start explaining exactly what to do.
_CONSENT_HELP = (
    "خلّها بضغطة وحدة 👇\n"
    "اختر من الأزرار تحت، أو اكتب كلمة وحدة:\n"
    "«أوافق» ونبدأ فورًا، أو «لا أوافق» ونوقف هنا"
)
#: Third miss: a human takes it. Every promise here is one we keep — a ticket
#: row is written, the operator channel is pinged when it is wired, and «دعم»
#: is the escalation the whole product already trusts (the C4 worker
#: intercepts it before any conversation and pages the operator), so this is a
#: door that really opens.
_CONSENT_STUCK = (
    "ما أبي أعلّقك أكثر 🙏\n"
    "سجّلت حالتك عند الفريق ونتابعها معك\n"
    "وإذا تبي أحد يكلمك الحين اكتب: دعم\n"
    "وتقدر تكمل بضغطة من الزر تحت"
)

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


def _consent_wall() -> str:
    """CHANGELOG §10: one merged message for the three required purposes."""
    bullets = "\n".join(
        f"• {p.title_ar}: {p.description_ar}" for p in _required_purposes()
    )
    return (
        "قبل ما نبدأ — موافقة واحدة تغطي تشغيل الخدمة:\n"
        f"{bullets}\n\n"
        f"اضغط الزر تحت، أو اكتب «{_AGREE}» أو «{_DECLINE}»."
    )


def _send_consent(deps: Deps, channel: CustomerChannel, body: str) -> None:
    """Every message this gate sends carries the buttons — including the ones
    that follow a reply we could not read, so the way forward is always one
    tap away and never depends on spelling."""
    deps.whatsapp_client.send_interactive(
        channel.phone_e164, body, _CONSENT_BUTTONS
    )


def _prompt_consent(
    deps: Deps, channel: CustomerChannel, *, lead: str = ""
) -> None:
    _send_consent(deps, channel, f"{lead}\n{_consent_wall()}" if lead
                  else _consent_wall())


def _unresolved_consent(
    session: Session, *, row: FunnelSession, channel: CustomerChannel,
    deps: Deps, verdict: str,
) -> None:
    """The way OUT of the gate — for EVERY verdict that is not a grant.

    The old ``else`` re-sent the identical wall, forever, with no counter, no
    «لم أفهم» and no escalation — for a customer who had already paid 29
    riyals. Now every attempt says something new and the third one puts a
    human on it, while «أوافق» keeps working at any point.

    AUDIT 2026-08-05: DECLINE used to walk straight past this function. It
    explained why the consent is needed and re-sent the wall, and it did that
    on the first refusal and on the hundredth — the counter never moved, so
    the escalation ladder could not be reached from that branch at all. That
    is the loop the ladder exists to end, and a customer who reads the wall as
    a refusal every time is exactly the customer who most needs a human. Every
    non-grant verdict is counted here now: there is no consent outcome left
    that can repeat unbounded.
    """
    context = dict(row.context or {})
    attempts = int(context.get(_CONSENT_ATTEMPTS_KEY, 0)) + 1
    row.context = {**context, _CONSENT_ATTEMPTS_KEY: attempts}

    if attempts >= _MAX_CONSENT_ATTEMPTS:
        _escalate_consent(session, row=row, channel=channel, deps=deps,
                          attempts=attempts)
        return
    if verdict == CONSENT_DECLINE:
        # A refusal was understood perfectly — it earns the reason, not the
        # «I did not follow you» copy, at every attempt before the last.
        lead = _CONSENT_DECLINED_EXPLAIN
    elif attempts >= 2:
        lead = _CONSENT_HELP
    else:
        lead = _CONSENT_ACK_ASK if verdict == CONSENT_ACK else _CONSENT_NOT_CLEAR
    _prompt_consent(deps, channel, lead=lead)


def _escalate_consent(
    session: Session, *, row: FunnelSession, channel: CustomerChannel,
    deps: Deps, attempts: int,
) -> None:
    """Hand the stall to the operator — once per stall — and keep answering.

    One OPEN ticket per tenant: a customer who keeps writing must never stop
    getting a reply (no silent failure, §15.12), but must not page the
    operator on every message either. The log line carries the TEN code only
    (§13 — never a phone, never a name).
    """
    already_open = session.execute(
        select(SupportEvent.id).where(
            SupportEvent.tenant_id == row.tenant_id,
            SupportEvent.kind == CONSENT_STUCK_KIND,
            SupportEvent.status.in_(sorted(MUTING_STATUSES)),
        ).limit(1)
    ).scalar_one_or_none()
    if already_open is None:
        session.add(SupportEvent(
            id=uuid.uuid4(), tenant_id=row.tenant_id, channel_id=channel.id,
            kind=CONSENT_STUCK_KIND, status=OPEN,
        ))
        tenant = session.get(Tenant, row.tenant_id)
        code = tenant.code if tenant is not None else "unknown tenant"
        logger.warning(
            "funnel consent unresolved after %d replies — %s", attempts, code,
        )
        if deps.admin_client is not None:
            try:
                deps.admin_client.send_admin(
                    f"⚠️ {code} · funnel consent stuck · "
                    f"{attempts} unresolved replies · paid cv_analysis"
                )
            except Exception:  # noqa: BLE001 — the ticket is already written
                logger.warning("consent stall notify failed", exc_info=True)
    _send_consent(deps, channel, _CONSENT_STUCK)


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
        if existing.state != STATE_DONE:
            return existing               # resume the running journey
        # AUDIT FIX B: a paying SECOND analysis restarts the journey — the
        # required consents carry over presence-driven, so it reopens at
        # the first still-missing step (usually the upload).
        existing.subscription_id = subscription_id
        still_missing = missing_required(
            _consent_records(session, tenant_id)
        )
        if still_missing:
            existing.state = STATE_CONSENT
            existing.context = {}
            session.flush()
            deps.whatsapp_client.send_text(channel.phone_e164, _WELCOME)
            _prompt_consent(deps, channel)
        else:
            existing.state = STATE_UPLOAD
            existing.context = {}
            session.flush()
            deps.whatsapp_client.send_text(
                channel.phone_e164,
                "أهلًا بعودتك! 📊 تحليل جديد لسيرتك — " + _UPLOAD_PROMPT,
            )
        existing.updated_at = now
        return existing
    row = FunnelSession(
        id=uuid.uuid4(), tenant_id=tenant_id, subscription_id=subscription_id,
        channel_id=channel_id, state=STATE_CONSENT,
        context={}, updated_at=now,
    )
    session.add(row)
    session.flush()
    deps.whatsapp_client.send_text(channel.phone_e164, _WELCOME)
    _prompt_consent(deps, channel)
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
        # Byte equality against «أوافق» lived here. «اوافق» — the spelling the
        # default Saudi Android keyboard produces — fell into the else and got
        # the identical wall back, forever (audit 2026-08). The reading is the
        # onboarding authority verbatim: obvious spellings pass, negation
        # never does, and an acknowledgement is never rounded up to a grant.
        verdict = classify_consent_reply(body)
        if verdict == CONSENT_AGREE:
            # one tap → the three required purposes, each its own event
            for purpose in _required_purposes():
                record_consent(
                    session, tenant_id=row.tenant_id,
                    purpose=purpose.key, action="granted",
                )
            row.state = STATE_UPLOAD
            row.context = {k: v for k, v in (row.context or {}).items()
                           if k != _CONSENT_ATTEMPTS_KEY}
            deps.whatsapp_client.send_text(channel.phone_e164, _UPLOAD_PROMPT)
        else:
            # DECLINE, ACK and UNCLEAR all end up here on purpose — see the
            # incident in _unresolved_consent. Only a grant leaves the gate.
            _unresolved_consent(
                session, row=row, channel=channel, deps=deps, verdict=verdict,
            )

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
    # §15.8 + the published privacy page: «اسمك لا يُرسل إلى أي نموذج». The
    # funnel never asks for a name, so recover it from the CV header —
    # otherwise the name line reached the model verbatim (closure audit ح-3).
    header_name = infer_header_name(text)
    stripped = strip_pii(text, known_name=header_name)
    contact_found = any(k.startswith("[EMAIL_") for k in stripped.replacements)
    try:
        run_extraction(
            session, tenant_id=row.tenant_id, cv_text=text,
            known_name=header_name, extractor=deps.extractor,
        )
    except (ExtractionFailed, PiiLeak):
        # Both mean the same thing to the customer: we could not read this
        # file. PiiLeak specifically means the backstop refused to send text
        # that still carried a name — since 2026-08-05 it can refuse a layout
        # the stripper's heading vocabulary missed, which is the fail-closed
        # direction §15.8 demands. Neither may escape as an unhandled
        # exception: the funnel session would stay in UPLOAD_PENDING with no
        # reply at all, which is the silent failure §15.12 forbids. The log
        # line carries the reason only — never any of the document.
        logger.warning("funnel cv extraction failed", exc_info=True)
        deps.whatsapp_client.send_text(
            channel.phone_e164,
            "تعذر قراءة الملف — أعد إرساله من فضلك، وإذا تكرر اكتب: دعم",
        )
        return
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
    # audit fix: a second same-day analysis must never overwrite the first
    # stored artifact — the key carries the full timestamp.
    stamp = now.astimezone(UTC).strftime("%Y-%m-%d-%H%M%S")
    report_key = tenant_key(
        str(row.tenant_id), "funnel_reports", f"{stamp}.pdf"
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
