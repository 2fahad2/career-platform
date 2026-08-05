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
from career.whatsapp.inbound import (
    InboundKind,
    classify_inbound,
    has_readable_text,
    media_ack,
)

logger = logging.getLogger("career.whatsapp")

_UNRECOGNIZED = "أرسل رمز التفعيل من صفحة الشكر بعد الدفع للبدء."
#: Honest on both counts. The old text promised «دعم» would bring them back —
#: it does not, and nothing did: no code path anywhere cleared opt_out_at, so
#: a customer who typed «إلغاء الاشتراك» was silenced FOREVER while their
#: subscription and their billing carried on untouched.
_STOP_CONFIRM = (
    "تم إيقاف الرسائل ✅ ما راح نرسل لك شي بعد الآن\n"
    "ترجع بأي وقت — أرسل:\n"
    "تشغيل الرسائل\n"
    "وإذا قصدك توقف الاشتراك والدفع نفسه، أرسل: دعم — ونتولاها معك"
)

_RESUME_CONFIRM = (
    "رجعت لك الرسائل ✅\n"
    "بنكمل معك عادي من هنا"
)


#: «دعم» is promised everywhere as the way to reach a human — so it must
#: answer the customer, not just page the operator (closure audit).
_SUPPORT_ACK = (
    "وصلتنا رسالتك 🙏\n"
    "أحد من الفريق بيتواصل معك بأقرب وقت — وأنت اكتب لنا أي وقت"
)
#: The last line of defence against silence for a customer who is REALLY on
#: the daily service — status ACTIVE on a renewable pass, the exact pair
#: engine/families.py requires to put them in tonight's search. Promising the
#: morning delivery to anyone else is a lie the system itself contradicts.
_ACTIVE_FALLBACK = (
    "وصلتني رسالتك 👌\n"
    "أنا معك يوميًا: كل صباح أرسل لك فرصك المختارة ومعها سيرتك جاهزة لها\n"
    "وتقدر تكتب بأي وقت:\n"
    "حالة اشتراكي · وقف مؤقت · تصدير بياناتي · دعم"
)
#: A one-shot analysis customer (cv_analysis) is NOT on the daily service and
#: never will be without buying it — daily_job_limit = 0 by design. Telling
#: them «أنا معك يوميًا» right after their report reads as an enrolment
#: confirmation and leaves them waiting every morning for nothing.
_FUNNEL_FALLBACK = (
    "وصلتني رسالتك 👌\n"
    "خدمتك معنا هي تحليل السيرة الذاتية — وتقريرك وصلك\n"
    "وما أنت مشترك في البحث اليومي، فما راح توصلك فرص كل صباح\n"
    "وإذا تبي نشتغل عنك يوميًا ونجهّز لك سيرة مخصصة لكل فرصة، أرسل: دعم "
    "ونرتبها لك\n"
    "وتقدر تكتب بأي وقت: تصدير بياناتي · حذف بياناتي · دعم"
)
#: Paused: the service is off by their own hand, and the fallback used to
#: offer «وقف مؤقت» a second time without ever naming the way back.
_PAUSED_FALLBACK = (
    "وصلتني رسالتك 👌\n"
    "اشتراكك موقوف مؤقتًا الحين، فما راح توصلك فرص الصباح\n"
    "وترجع لك من نفس النقطة إذا أرسلت: استئناف\n"
    "وتقدر تكتب بأي وقت: حالة اشتراكي · تصدير بياناتي · دعم"
)
#: Expired / grace / canceled / suspended — service off, and «حالة اشتراكي»
#: is the reply that already carries the renewal link (privacy §16).
_INACTIVE_FALLBACK = (
    "وصلتني رسالتك 👌\n"
    "اشتراكك غير فعّال حاليًا، فما راح توصلك فرص الصباح\n"
    "أرسل: حالة اشتراكي — تشوف حالتك وطريقة التجديد\n"
    "وإذا تبي مساعدة أرسل: دعم"
)
#: No live subscription row we can speak for — say exactly that instead of
#: guessing a service level.
_UNKNOWN_PLAN_FALLBACK = (
    "وصلتني رسالتك 👌\n"
    "ما قدرت أتأكد من حالة اشتراكك من هنا\n"
    "أرسل: حالة اشتراكي — وإذا ما ظهر شي أرسل: دعم وأحد من الفريق يتابع معك"
)

#: Media we cannot read. Every branch says the same two true things: your
#: message arrived, and I cannot open it — then names the next step. §16: the
#: two Latin format names each sit ALONE on their own line.
_MEDIA_CONVERSATION = (
    "\nبس ما أقدر أقرأ إلا النص المكتوب — الصوت والصور والملصقات ما تنفتح عندي\n"
    "اكتب لي إجابتك نصًا وبنكمل من نفس النقطة 🙏"
)
_MEDIA_UPLOAD = (
    "\nبس ما أقدر أقرأ سيرتك من صورة ولا من رسالة صوتية — أحتاجها ملفًا مرفقًا\n"
    "أرسلها بإحدى الصيغتين:\n"
    "PDF\n"
    "DOCX"
)
_MEDIA_ACTIVE = (
    "\nبس ما أقدر أقرأ إلا النص المكتوب — الصوت والصور والملصقات ما تنفتح عندي\n"
    "اكتب لي سؤالك نصًا وأنا معك\n"
    "وإذا تبي تكلم أحد من الفريق أرسل: دعم"
)
#: An ACTIVE customer sending a file is almost always sending an updated CV.
#: We cannot merge it from here — and dropping it silently meant every later
#: CV was built from the stale profile without anyone knowing.
_DOCUMENT_ACTIVE = (
    "وصلني الملف 👌\n"
    "بس ما أقدر أحدّث ملفك المهني من هنا — ما دخل شي على بياناتك\n"
    "إذا كان الملف سيرتك الجديدة أرسل: دعم — وأحد من الفريق يحدّثها لك"
)


def _ten_code(session: Session, tenant_id: uuid.UUID) -> str:
    tenant = session.get(Tenant, tenant_id)
    return tenant.code if tenant is not None else "(unknown)"


def _event_ten_codes(session: Session, payload: dict[str, Any]) -> str:
    """The TEN codes an event touches, for the operator's failure notice.

    «A WhatsApp message failed» with no name told the operator that someone
    somewhere lost their turn and left them to find out who by reading the
    journal. The payload carries phone numbers, never the code, so it is
    resolved here — and only the code ever leaves this function (§15.13).
    """
    from career.whatsapp.phones import phone_variants

    phones: list[str] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                raw = msg.get("from")
                if isinstance(raw, str):
                    phones.append(raw)
            for st in value.get("statuses", []) or []:
                raw = st.get("recipient_id")
                if isinstance(raw, str):
                    phones.append(raw)
    codes: list[str] = []
    try:
        for phone in phones:
            channel = session.execute(
                select(CustomerChannel).where(
                    CustomerChannel.provider == "whatsapp",
                    CustomerChannel.phone_e164.in_(phone_variants(phone)),
                )
            ).scalars().first()
            if channel is None:
                continue
            code = _ten_code(session, channel.tenant_id)
            if code not in codes:
                codes.append(code)
    except Exception:  # noqa: BLE001 — the alert matters more than the name
        logger.warning("tenant code lookup failed", exc_info=True)
    return " · ".join(codes) if codes else "(رقم غير مرتبط بعميل)"


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


def _fallback_text(session: Session, tenant_id: uuid.UUID) -> str:
    """The truth about what THIS customer will actually receive.

    The daily service runs for exactly one pair — status ACTIVE on a
    renewable plan (engine/families.py) — so that is the only pair allowed to
    hear the daily promise. Everyone else hears their real situation and the
    real next step.
    """
    from career.salla import subscriptions as sub_states
    from career.salla.renewal import RENEWABLE_PLANS, current_subscription

    try:
        subscription = current_subscription(session, tenant_id)
    except Exception:  # noqa: BLE001 — a lookup that fails must not mute us
        logger.warning("fallback plan lookup failed", exc_info=True)
        return _UNKNOWN_PLAN_FALLBACK
    if subscription is None:
        return _UNKNOWN_PLAN_FALLBACK
    if subscription.plan_code not in RENEWABLE_PLANS:
        return _FUNNEL_FALLBACK          # a one-shot analysis, never a service
    if subscription.status == sub_states.ACTIVE:
        return _ACTIVE_FALLBACK
    if subscription.status == sub_states.PAUSED:
        return _PAUSED_FALLBACK
    if subscription.status in (sub_states.ONBOARDING, sub_states.PAID_UNCLAIMED,
                               sub_states.PENDING_PAYMENT):
        return _UNKNOWN_PLAN_FALLBACK    # setup never finished — don't promise
    return _INACTIVE_FALLBACK


def _media_reply(message_type: str, tail: str) -> str:
    """Acknowledge what arrived, admit we cannot read it, name the next step."""
    return media_ack(message_type) + tail


def _reply(whatsapp_client: WhatsAppClient, phone: str, body: str) -> None:
    """An ack must never crash the turn (the inbound is already recorded)."""
    try:
        whatsapp_client.send_text(phone, body)
    except Exception:  # noqa: BLE001
        logger.warning("customer reply failed", exc_info=True)


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
        # A prospect asking to see the work before paying — the store copy
        # promises «راسلنا واتساب بكلمة عينة», so keep it here, before the
        # activation-code fallback (no tenant exists yet, nothing is written).
        from career.whatsapp import samples as wa_samples

        if wa_samples.is_sample_request(text_body) and wa_samples.send_samples(
            whatsapp_client, from_phone
        ):
            return
        # Unknown number, no claimable order → generic help (in-window).
        whatsapp_client.send_text(from_phone, _UNRECOGNIZED)
        return

    channel.last_inbound_at = now  # any inbound opens the 24h window
    opted_out = channel.opt_out_at is not None

    if kind is InboundKind.STOP:
        channel.opt_out_at = now
        tenant_id = channel.tenant_id
        _record_inbound(session, tenant_id=tenant_id, channel_id=channel.id,
                        wamid=wamid, message_type=message_type, text_body=text_body,
                        classification="stop", payload=msg, now=now)
        # COMMIT FIRST, then talk. An opt-out is a compliance fact and the
        # confirmation is a courtesy: the send used to come first and
        # unwrapped, so a 429 or a 5xx from Meta threw out of here into the
        # event loop's rollback, opt_out_at went back to NULL, and the
        # customer who had just been told «ما راح نرسل لك شي» received the
        # next morning's delivery. A duplicate confirmation costs nothing; a
        # lost opt-out is the one failure we may not have.
        session.commit()
        _reply(whatsapp_client, from_phone, _STOP_CONFIRM)
        # A paying customer going quiet is the single loudest churn signal we
        # get, and «إلغاء الاشتراك» is in the STOP set — they may well mean
        # cancel the BILLING, which no message of ours can do. The operator
        # hears about it (TEN code only, §15.13).
        try:
            admin_client.send_admin(
                f"🔇 عميل أوقف الرسائل {_ten_code(session, tenant_id)} — "
                "لو كان قصده إلغاء الاشتراك فالفوترة ما زالت شغالة، راجعه"
            )
        except Exception:  # noqa: BLE001 — alerting never blocks the opt-out
            logger.warning("stop alert failed", exc_info=True)
        return

    if kind is InboundKind.RESUME and opted_out:
        # The way back that did not exist. Clearing this flag is the ONLY
        # thing that re-opens the channel — the subscription-level «استئناف»
        # in the privacy commands is a DIFFERENT switch, and a customer who
        # flipped both needs both. Deliberately gated on `opted_out`: when
        # they are not silenced, «استئناف» must fall through to the privacy
        # command that resumes a PAUSED subscription, which is what it has
        # always meant.
        channel.opt_out_at = None
        _record_inbound(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                        wamid=wamid, message_type=message_type, text_body=text_body,
                        classification="resume", payload=msg, now=now)
        # Same ordering as STOP and for the same reason: the flag is the fact,
        # the message is the courtesy. A send failure here used to roll the
        # channel back to silenced — the customer had asked to come back, was
        # told nothing, and stayed muted with no way of knowing it.
        # a silenced customer who comes back may ALSO be paused at the
        # subscription level; serve that with the standing command they
        # already know rather than making them guess a second word.
        session.commit()
        _reply(whatsapp_client, from_phone, _RESUME_CONFIRM)
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
        # Local import, like the other call site in this module: the salla
        # package imports back into whatsapp at module scope.
        from career.salla.renewal import current_subscription

        sub = current_subscription(session, channel.tenant_id)
        ten_code = _ten_code(session, channel.tenant_id)
        plan_code = sub.plan_code if sub is not None else None
        phone = channel.phone_e164
        # The ticket is the durable half — commit it before either transport
        # is touched. The Telegram page was unwrapped and ran first, so a
        # Telegram blip discarded the support_events row along with the
        # inbound: the customer who asked for a human got no ack, no ticket,
        # and no operator. Both sends below are best-effort over a fact that
        # is already recorded.
        session.commit()
        try:
            admin_client.send_admin(
                admin_msg.support_request(ten_code, plan_code)
            )
        except Exception:  # noqa: BLE001 — the ticket is already recorded
            logger.warning("support escalation failed", exc_info=True)
        # closure audit: «دعم» is the escape hatch printed in every error
        # message and on the store page — and it used to page the operator
        # while answering the CUSTOMER with nothing at all.
        _reply(whatsapp_client, phone, _SUPPORT_ACK)
        return

    # OTHER — record; route to the onboarding journey when one is running,
    # else this tap descends a pending delivery (the post-ACTIVE behavior).
    _record_inbound(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                    wamid=wamid, message_type=message_type, text_body=text_body,
                    classification="other", payload=msg, now=now)
    from career.funnel import flow as funnel_flow

    if opted_out:
        # closure audit: opting out silenced the DATA RIGHTS too — export,
        # delete, status and resume all died, though §12 and the privacy page
        # promise them for life and PDPL does not let an opt-out revoke them.
        # Opting out stops MARKETING, never the customer's own requests.
        if onboarding is not None and text_body and \
                orchestrator.handle_standing_command(
                    session, channel_id=channel.id, text=text_body,
                    deps=onboarding, now=now):
            session.commit()
            return
        session.commit()
        return

    # AUDIT ك-7: standing privacy commands work for EVERY paying customer at
    # EVERY stage — funnel customers included (PDPL + the welcome message's
    # own promise). Before this, «حذف بياناتي» during the funnel was consumed
    # as a career-path answer, and after it there was no journey so the
    # handler refused — the commands were dead for the funnel lifecycle.
    if onboarding is not None and text_body and \
            orchestrator.handle_standing_command(
                session, channel_id=channel.id, text=text_body,
                deps=onboarding, now=now):
        session.commit()
        return

    # A voice note, a photo, a sticker, a location — the customer said
    # something, but nothing WE can read. Routing it on as "" is not neutral:
    # mid-confirmation it is committed as a blank CUSTOMER_CONFIRMED fact
    # (§15.5 — nothing enters the achievement bank without an explicit human
    # confirmation) and the question is never asked again. Documents keep
    # their own real handler; everything else gets the truth.
    readable = has_readable_text(text_body)
    is_document = message_type == "document"

    funnel = (
        funnel_flow.incomplete_funnel(session, channel.tenant_id)
        if onboarding else None
    )
    if onboarding is not None and funnel is not None:
        if is_document:
            document = msg.get("document", {}) or {}
            funnel_flow.handle_funnel_document(
                session, channel_id=channel.id,
                media_id=str(document.get("id", "")),
                filename=document.get("filename"), deps=onboarding, now=now,
            )
        elif readable and text_body is not None:
            funnel_flow.handle_funnel_text(
                session, channel_id=channel.id, text=text_body,
                deps=onboarding, now=now,
            )
        else:
            _reply(whatsapp_client, channel.phone_e164, _media_reply(
                message_type,
                _MEDIA_UPLOAD if funnel.state == funnel_flow.STATE_UPLOAD
                else _MEDIA_CONVERSATION,
            ))
        session.commit()
        return
    journey = _incomplete_journey(session, channel.tenant_id) if onboarding else None
    if onboarding is not None and journey is not None:
        if is_document:
            document = msg.get("document", {}) or {}
            orchestrator.handle_document(
                session, channel_id=channel.id,
                media_id=str(document.get("id", "")),
                filename=document.get("filename"), deps=onboarding, now=now,
            )
        elif readable and text_body is not None:
            orchestrator.handle_text(
                session, channel_id=channel.id, text=text_body,
                deps=onboarding, now=now,
            )
        else:
            _reply(whatsapp_client, channel.phone_e164, _media_reply(
                message_type,
                _MEDIA_UPLOAD if journey.state == "CV_UPLOAD_PENDING"
                else _MEDIA_CONVERSATION,
            ))
    else:
        # audit fix: standing privacy commands must survive ACTIVE (§05/§12)
        if onboarding is not None and text_body and \
                orchestrator.handle_standing_command(
                    session, channel_id=channel.id, text=text_body,
                    deps=onboarding, now=now):
            session.commit()
            return

        from career.cv import deliver as cv_deliver
        from career.cv.daily_run import close_from_delivery

        # AUDIT ك-6: branch priority is load-bearing. Outcome buttons and the
        # held-bundle descend must BEAT the enrichment branch — otherwise an
        # open enrichment session eats «قدمت» taps (outcome never recorded)
        # and blocks a held delivery from landing for up to 72h.
        effective = _button_id_of(msg) or text_body

        # §20: the answer to the outcome question. Checked BEFORE the delivery
        # buttons and before enrichment for the same reason those two beat
        # enrichment — a reply we asked for must never be eaten by another
        # open conversation, or the datum is lost and cannot be re-collected.
        from career.cv import outcome_followup as followup

        answer = followup.parse_answer(effective)
        if answer is not None:
            # CONSUMES the tap either way. Falling through when nothing was
            # pending sent «oc_interview» into the enrichment branch, whose
            # final else treats unmatched text as the customer's achievement
            # — so a double tap, or a tap on an old card (WhatsApp keeps them
            # tappable forever), wrote a button id into the achievement bank
            # and paid for an LLM call to render it. Constant 5.
            pending = followup.pending_job_ref(
                session, tenant_id=channel.tenant_id)
            if pending is not None:
                thanks = followup.record_answer(
                    session, tenant_id=channel.tenant_id, job_ref=pending,
                    outcome=answer, now=now,
                )
            else:
                thanks = followup.ALREADY_ANSWERED_AR
            # the answer is the datum §20 exists to collect and it cannot be
            # asked for twice — record it, then thank them. Sending first meant
            # a Meta hiccup rolled the recorded outcome back and the tap was
            # spent: the card is answered, so the question never returns.
            phone = channel.phone_e164
            session.commit()
            _reply(whatsapp_client, phone, thanks)
            return

        outcome = cv_deliver.parse_outcome_button(effective)
        if outcome is not None:
            outcome_kind, job_ref = outcome
            cv_deliver.record_outcome(
                session, tenant_id=channel.tenant_id, job_ref=job_ref,
                outcome=outcome_kind, reason=None, now=now,
            )
            # same ordering as the outcome answer above: the recorded feedback
            # survives a failed thank-you, never the other way round.
            phone = channel.phone_e164
            session.commit()
            _reply(
                whatsapp_client, phone,
                "شكرًا! سجلنا ملاحظتك — تساعدنا نحسّن اختياراتنا لك. 🙏",
            )
            return

        landed = descend_pending_delivery(
            session, channel, whatsapp_client=whatsapp_client, now=now
        )
        if landed is not None:
            # the held day closes when its bundle actually lands; the same
            # inbound may ALSO be an enrichment answer — fall through.
            closed = close_from_delivery(session, delivery=landed, now=now)
            if closed is not None:
                # AUDIT ك-15: held days were skipped by the nightly rollup
                from career.cv import close as close_mod

                try:
                    close_mod.rollup_costs(
                        session, tenant_id=landed.tenant_id,
                        day=closed.run_date,
                    )
                except Exception:  # noqa: BLE001 — accounting never blocks
                    logger.warning("descend cost rollup failed", exc_info=True)

        # F-ENRICH (§13): an open enrichment session consumes this reply —
        # button taps route by machine id (also when the tap carries no
        # title, minor audit fix), typed labels keep working.
        if onboarding is not None and effective and \
                orchestrator.handle_enrichment(
                    session, channel_id=channel.id, text=effective,
                    deps=onboarding, now=now):
            session.commit()
            return

        # closure audit: an ACTIVE customer who typed anything else got NO
        # reply at all — five realistic messages produced zero outbound. A
        # paying customer must never wonder whether we are still here. The
        # reply now tells THIS customer what they will actually receive:
        # only a live pass hears the daily promise.
        if landed is None and (readable or effective):
            # `effective` covers a tap whose payload carried no title — an
            # intent we could read, just not one we matched.
            _reply(whatsapp_client, channel.phone_e164,
                   _fallback_text(session, channel.tenant_id))
        elif is_document:
            # the file is not silence-worthy even when a held bundle landed:
            # an updated CV dropped without a word meant every later CV was
            # built from the stale profile and nobody knew.
            _reply(whatsapp_client, channel.phone_e164, _DOCUMENT_ACTIVE)
            try:
                admin_client.send_admin(
                    f"📎 عميل أرسل ملفًا بعد التفعيل "
                    f"{_ten_code(session, channel.tenant_id)} — "
                    "قد تكون سيرة محدّثة، راجعه"
                )
            except Exception:  # noqa: BLE001 — alerting never blocks the reply
                logger.warning("document notice failed", exc_info=True)
        elif landed is None:
            # a voice note / photo / sticker from a paying customer: the same
            # silence, one type further out — answered, never pretended.
            _reply(whatsapp_client, channel.phone_e164,
                   _media_reply(message_type, _MEDIA_ACTIVE))
    session.commit()


#: The receipt ladder. Meta does NOT guarantee the ORDER in which delivery
#: receipts arrive — retries and webhook redelivery routinely hand us a
#: «sent» after a «read» — so a receipt is a claim about a POINT on this
#: ladder, never «the current truth», and only a higher rung may be written.
#:
#: `failed` sits between `sent` and `delivered`, deliberately, and it is the
#: only rung that needed an argument. It is not last: a send can only fail
#: while it is merely sent — nothing that has reached the handset can
#: un-arrive — so a `failed` after `sent` is the ordinary race and must win,
#: while a `delivered`/`read` after a `failed` is positive proof of receipt
#: for this exact wa_message_id and outranks a failure that, by Meta's own
#: model, cannot have happened to it (almost always the receipt belongs to a
#: different send attempt). And it is not first either: that is the direction
#: with money on it — `cv/close.whatsapp_spend` bills every template whose
#: status is not `failed`, so letting a stale `sent` overwrite a `failed`
#: silently turned an undelivered message into a billed one.
#:
#: Unknown rungs rank 0: a status we do not know cannot displace one we do.
_RECEIPT_ORDER: dict[str, int] = {
    "sent": 1, "failed": 2, "delivered": 3, "read": 4,
}


def _receipt_rank(status: str | None) -> int:
    return _RECEIPT_ORDER.get(str(status or "").lower(), 0)


def _handle_status(session: Session, st: dict[str, Any], *, now: datetime) -> None:
    wamid = st.get("id")
    status = st.get("status")
    if not wamid or not status:
        return
    rank = _receipt_rank(status)
    if rank == 0:
        logger.warning("unknown whatsapp receipt status: %s", status)
    for dm in session.execute(
        select(DeliveryMessage).where(DeliveryMessage.wa_message_id == wamid)
    ).scalars():
        if rank <= _receipt_rank(dm.status):
            continue  # out of order, or the same receipt twice — a no-op
        dm.status = str(status)
        # the stamp belongs to the receipt that actually WON, so a superseded
        # duplicate never makes a row look freshly updated
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
                # WHOSE turn was lost. The unnamed notice made the operator
                # read the journal to find out, and «راجع السجل» is not an
                # instruction anyone can act on at 6am.
                admin_client.send_admin(
                    "⚠️ رسالة واتساب واردة فشلت معالجتها وعُزلت "
                    f"{_event_ten_codes(owner_session, ev.payload or {})} "
                    "— راجع السجل"
                )
            except Exception:  # noqa: BLE001
                logger.warning("admin note failed", exc_info=True)
        ev.processed_at = func.now()
        ev.attempt_count = ev.attempt_count + 1
        owner_session.commit()
    return counts
