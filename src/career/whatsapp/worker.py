"""WhatsApp inbound worker (whatsapp §08) — the 200-deferred processor.

Runs as the owner role (routes by phone before the tenant is known, spans
tenants). For each received webhook_events row it parses the Meta payload and:
- routes inbound messages: activation, STOP (opt-out, immediate), support
  (escalate to admin), or OTHER (opens the window → descend a pending delivery);
- applies delivery status callbacks to delivery_messages.

Per-message idempotency is by wa_message_id (inbound_messages unique); a
redelivered message is a no-op.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import (
    CustomerChannel,
    DeliveryMessage,
    InboundMessage,
    OnboardingSession,
    SupportEvent,
    Tenant,
    WebhookEvent,
)
from career.onboarding import orchestrator
from career.telegram import messages as admin_msg
from career.telegram.admin import TelegramAdminClient
from career.whatsapp.activation_flow import (
    ActivationStatus,
    activate,
    activate_by_order_phone,
)
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.delivery import descend_pending_delivery
from career.whatsapp.inbound import InboundKind, classify_inbound

logger = logging.getLogger("career.whatsapp")

_UNRECOGNIZED = "أرسل رمز التفعيل من صفحة الشكر بعد الدفع للبدء."
_STOP_CONFIRM = "تم إيقاف الرسائل. لن نرسل لك بعد الآن. لإعادة التفعيل أرسل: دعم"


def _ten_code(session: Session, tenant_id: uuid.UUID) -> str:
    tenant = session.get(Tenant, tenant_id)
    return tenant.code if tenant is not None else "(unknown)"


def _channel_for_phone(session: Session, phone: str) -> CustomerChannel | None:
    return session.execute(
        select(CustomerChannel).where(
            CustomerChannel.provider == "whatsapp",
            CustomerChannel.phone_e164 == phone,
        )
    ).scalar_one_or_none()


def _inbound_exists(session: Session, wamid: str) -> bool:
    return session.execute(
        select(InboundMessage.id).where(InboundMessage.wa_message_id == wamid)
    ).first() is not None


def _record_inbound(
    session: Session, *, tenant_id: uuid.UUID, channel_id: uuid.UUID, wamid: str,
    message_type: str, text_body: str | None, classification: str,
    payload: dict[str, Any], now: datetime,
) -> InboundMessage:
    inbound = InboundMessage(
        id=uuid.uuid4(), tenant_id=tenant_id, channel_id=channel_id,
        wa_message_id=wamid, message_type=message_type, text_body=text_body,
        classification=classification, payload=payload, processed_at=now,
    )
    session.add(inbound)
    session.flush()
    return inbound


def _button_id_of(msg: dict[str, Any]) -> str | None:
    """The machine id of a tapped button (outcome buttons carry it) —
    distinct from the human label _text_of returns."""
    inter = msg.get("interactive", {}) or {}
    reply = inter.get("button_reply") or inter.get("list_reply") or {}
    raw = reply.get("id")
    return raw if isinstance(raw, str) else None


def _text_of(msg: dict[str, Any]) -> str | None:
    mtype = msg.get("type", "")
    raw: Any = None
    if mtype == "text":
        raw = msg.get("text", {}).get("body")
    elif mtype == "button":
        raw = msg.get("button", {}).get("text")
    elif mtype == "interactive":
        inter = msg.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        raw = reply.get("title")
    return raw if isinstance(raw, str) else None


def _incomplete_journey(session: Session, tenant_id: uuid.UUID) -> OnboardingSession | None:
    journey = session.execute(
        select(OnboardingSession).where(OnboardingSession.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if journey is None or journey.state == "ACTIVE":
        return None
    return journey


def _handle_message(
    session: Session, msg: dict[str, Any], *,
    whatsapp_client: WhatsAppClient, admin_client: TelegramAdminClient, now: datetime,
    onboarding: orchestrator.Deps | None = None,
) -> None:
    wamid = msg.get("id")
    from_phone = msg.get("from")
    if not wamid or not from_phone:
        return
    if _inbound_exists(session, wamid):
        return  # idempotent — already handled

    message_type = str(msg.get("type", ""))
    text_body = _text_of(msg)
    kind, token = classify_inbound(text_body)

    def _finish_activation(result: Any) -> None:
        """Shared post-activation handover (whitepaper §05): record the
        inbound, then start the funnel (cv_analysis) or the onboarding."""
        if not (result.tenant_id and result.channel_id):
            return
        _record_inbound(
            session, tenant_id=uuid.UUID(result.tenant_id),
            channel_id=uuid.UUID(result.channel_id), wamid=wamid,
            message_type=message_type, text_body=text_body,
            classification="activation", payload=msg, now=now,
        )
        if (
            onboarding is not None
            and result.subscription_id is not None
            and result.status
            in (ActivationStatus.ACTIVATED, ActivationStatus.ALREADY_LINKED)
        ):
            from career.db.models import Subscription
            from career.funnel import flow as funnel_flow

            sub = session.get(Subscription, uuid.UUID(result.subscription_id))
            if sub is not None and sub.plan_code == "cv_analysis":
                funnel_flow.start_funnel(
                    session,
                    tenant_id=uuid.UUID(result.tenant_id),
                    subscription_id=uuid.UUID(result.subscription_id),
                    channel_id=uuid.UUID(result.channel_id),
                    deps=onboarding, now=now,
                )
            else:
                orchestrator.start_journey(
                    session,
                    tenant_id=uuid.UUID(result.tenant_id),
                    subscription_id=uuid.UUID(result.subscription_id),
                    channel_id=uuid.UUID(result.channel_id),
                    deps=onboarding, now=now,
                )
        session.commit()

    if kind is InboundKind.ACTIVATION and token is not None:
        result = activate(
            session, token=token, from_phone=from_phone, display_name=None, now=now,
            whatsapp_client=whatsapp_client, admin_client=admin_client,
        )
        _finish_activation(result)
        return

    channel = _channel_for_phone(session, from_phone)
    if channel is None:
        # CHANGELOG §11 zero-touch claim: ANY reply from the exact phone on a
        # paid unclaimed order activates it — the reply is the proof.
        claimed = activate_by_order_phone(
            session, from_phone=from_phone, display_name=None, now=now,
            whatsapp_client=whatsapp_client, admin_client=admin_client,
        )
        if claimed is not None:
            _finish_activation(claimed)
            return
        # Unknown number, no claimable order → generic help (in-window).
        whatsapp_client.send_text(from_phone, _UNRECOGNIZED)
        return

    channel.last_inbound_at = now  # any inbound opens the 24h window
    opted_out = channel.opt_out_at is not None

    if kind is InboundKind.STOP:
        channel.opt_out_at = now
        _record_inbound(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                        wamid=wamid, message_type=message_type, text_body=text_body,
                        classification="stop", payload=msg, now=now)
        whatsapp_client.send_text(from_phone, _STOP_CONFIRM)
        session.commit()
        return

    if kind is InboundKind.SUPPORT:
        inbound = _record_inbound(
            session, tenant_id=channel.tenant_id, channel_id=channel.id, wamid=wamid,
            message_type=message_type, text_body=text_body, classification="support",
            payload=msg, now=now,
        )
        session.add(SupportEvent(
            id=uuid.uuid4(), tenant_id=channel.tenant_id, channel_id=channel.id,
            inbound_message_id=inbound.id, kind="support_request", status="open",
        ))
        admin_client.send_admin(admin_msg.support_request(_ten_code(session, channel.tenant_id)))
        session.commit()
        return

    # OTHER — record; route to the onboarding journey when one is running,
    # else this tap descends a pending delivery (the post-ACTIVE behavior).
    _record_inbound(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                    wamid=wamid, message_type=message_type, text_body=text_body,
                    classification="other", payload=msg, now=now)
    if opted_out:
        session.commit()
        return
    from career.funnel import flow as funnel_flow

    funnel = (
        funnel_flow.incomplete_funnel(session, channel.tenant_id)
        if onboarding else None
    )
    if onboarding is not None and funnel is not None:
        if message_type == "document":
            document = msg.get("document", {}) or {}
            funnel_flow.handle_funnel_document(
                session, channel_id=channel.id,
                media_id=str(document.get("id", "")),
                filename=document.get("filename"), deps=onboarding, now=now,
            )
        else:
            funnel_flow.handle_funnel_text(
                session, channel_id=channel.id, text=text_body or "",
                deps=onboarding, now=now,
            )
        session.commit()
        return
    journey = _incomplete_journey(session, channel.tenant_id) if onboarding else None
    if onboarding is not None and journey is not None:
        if message_type == "document":
            document = msg.get("document", {}) or {}
            orchestrator.handle_document(
                session, channel_id=channel.id,
                media_id=str(document.get("id", "")),
                filename=document.get("filename"), deps=onboarding, now=now,
            )
        else:
            orchestrator.handle_text(
                session, channel_id=channel.id, text=text_body or "",
                deps=onboarding, now=now,
            )
    else:
        # audit fix: standing privacy commands must survive ACTIVE (§05/§12)
        if onboarding is not None and text_body and \
                orchestrator.handle_standing_command(
                    session, channel_id=channel.id, text=text_body,
                    deps=onboarding, now=now):
            session.commit()
            return

        # F-ENRICH (§13): an open enrichment session consumes this reply
        if onboarding is not None and text_body and \
                orchestrator.handle_enrichment(
                    session, channel_id=channel.id, text=text_body,
                    deps=onboarding, now=now):
            session.commit()
            return

        from career.cv import deliver as cv_deliver
        from career.cv.daily_run import close_from_delivery

        outcome = cv_deliver.parse_outcome_button(
            _button_id_of(msg) or text_body
        )
        if outcome is not None:
            outcome_kind, job_ref = outcome
            cv_deliver.record_outcome(
                session, tenant_id=channel.tenant_id, job_ref=job_ref,
                outcome=outcome_kind, reason=None, now=now,
            )
            whatsapp_client.send_text(
                channel.phone_e164,
                "شكرًا! سجلنا ملاحظتك — تساعدنا نحسّن اختياراتنا لك. 🙏",
            )
        else:
            landed = descend_pending_delivery(
                session, channel, whatsapp_client=whatsapp_client, now=now
            )
            if landed is not None:
                # the held day closes when its bundle actually lands
                close_from_delivery(session, delivery=landed, now=now)
    session.commit()


def _handle_status(session: Session, st: dict[str, Any], *, now: datetime) -> None:
    wamid = st.get("id")
    status = st.get("status")
    if not wamid or not status:
        return
    for dm in session.execute(
        select(DeliveryMessage).where(DeliveryMessage.wa_message_id == wamid)
    ).scalars():
        dm.status = str(status)
        dm.status_updated_at = now


def process_pending_whatsapp(
    owner_session: Session, *,
    whatsapp_client: WhatsAppClient, admin_client: TelegramAdminClient,
    now: datetime, limit: int = 100,
    onboarding: orchestrator.Deps | None = None,
) -> dict[str, int]:
    events = list(owner_session.execute(
        select(WebhookEvent)
        .where(WebhookEvent.provider == "whatsapp",
               WebhookEvent.processing_status == "received")
        .order_by(WebhookEvent.received_at)
        .limit(limit)
    ).scalars().all())

    counts = {"messages": 0, "statuses": 0, "failed": 0}
    for ev in events:
        # audit fix: one poisoned event must never wedge the whole queue —
        # without this isolation a single raise left the event 'received'
        # and every later customer frozen behind an infinite retry.
        try:
            payload: dict[str, Any] = ev.payload or {}
            for entry in payload.get("entry", []) or []:
                for change in entry.get("changes", []) or []:
                    value = change.get("value", {}) or {}
                    for msg in value.get("messages", []) or []:
                        _handle_message(owner_session, msg,
                                        whatsapp_client=whatsapp_client,
                                        admin_client=admin_client, now=now,
                                        onboarding=onboarding)
                        counts["messages"] += 1
                    for st in value.get("statuses", []) or []:
                        _handle_status(owner_session, st, now=now)
                        counts["statuses"] += 1
            ev.processing_status = "processed"
        except Exception:  # noqa: BLE001 — isolate, record, move on
            logger.error("whatsapp event processing failed", exc_info=True)
            owner_session.rollback()
            ev.processing_status = "failed"       # honest, no silent retry loop
            counts["failed"] += 1
            try:
                admin_client.send_admin("⚠️ رسالة واتساب واردة فشلت معالجتها "
                                        "وعُزلت — راجع السجل")
            except Exception:  # noqa: BLE001
                logger.warning("admin note failed", exc_info=True)
        ev.processed_at = func.now()
        ev.attempt_count = ev.attempt_count + 1
        owner_session.commit()
    return counts
