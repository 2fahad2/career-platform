"""WhatsApp inbound worker (whatsapp §08) — the 200-deferred processor.

Runs as the owner role (routes by phone before the tenant is known, spans
tenants). For each received webhook_events row it parses the Meta payload and:
- routes inbound messages: activation, STOP (opt-out, immediate), support
  (escalate to admin), or OTHER (opens the window → descend a pending delivery);
- applies delivery status callbacks to delivery_messages.

Per-message idempotency is by wa_message_id (inbound_messages unique); a
redelivered message is a no-op.

A failure is not a verdict on the event. Since 0022 this module distinguishes
a TRANSIENT failure — Claude timed out, Meta answered 503, the database blinked
— which is deferred behind a clock and tried again up to :data:`MAX_ATTEMPTS`,
from an event that is genuinely POISONED, which is terminal and paged. The
model, and deliberately the vocabulary, are `salla/provisioning.py`'s. The one
thing neither retry nor budget may do is repeat something the customer already
received, and :class:`_SendLedger` is what makes that answerable rather than
assumed.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
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
from career.whatsapp.client import WhatsAppClient, WhatsAppSendError
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

#: What a SILENCED customer hears when he writes to us and it is not a
#: standing privacy command.
#:
#: This branch used to commit and return with zero outbound, and the argument
#: for that was ``classify_inbound``'s: RESUME is matched on the WHOLE message
#: — deliberately, because a false resume un-mutes someone on record as having
#: asked for silence — and «the miss costs nothing to speak of, because the
#: opt-out confirmation prints تشغيل الرسائل». That holds for a customer who
#: still has that message on screen. «طيب تشغيل الرسائل من فضلك» is OTHER, and
#: the customer who typed it had asked to come back, in words, and heard
#: NOTHING. So the way back is re-printed instead of assumed remembered.
#:
#: It is a reply, not a send: it answers a message he just wrote, inside the
#: window he just opened, and it changes nothing about his instruction — the
#: narrow RESUME match still owns the only switch, and no guess here un-mutes
#: anybody. «دعم» is named because SUPPORT is resolved before this branch, so
#: it genuinely still reaches a human while he is silenced.
_OPTED_OUT_WAY_BACK = (
    "وصلتني رسالتك 👌\n"
    "بس رسائلنا موقوفة بطلبك، فما راح يوصلك شي منا\n"
    "ترجع بأي وقت — أرسل:\n"
    "تشغيل الرسائل\n"
    "وإذا تبي تكلم أحد من الفريق أرسل: دعم"
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


#: Media that is a PERSON speaking to us, and the Arabic noun for it. A voice
#: note, a photo of a job offer, a clip: each one is somebody putting a
#: question to us in the only form they had to hand, and each was answered
#: with «I can only read written text» and reached nobody (D1.2).
#:
#: The set is deliberately not «everything that is not text». A sticker is a
#: 👍, a reaction is a tap by another name, and a location or a contact card
#: is data — none of them is a message a human is waiting on an answer to, and
#: escalating them would spend the one open ticket a real question needs. A
#: DOCUMENT is not here either, and that asymmetry is the point of the branch
#: below: a file already pages the operator for EVERY tier, because an
#: unmerged updated CV silently poisons every later CV, so a لمّاح+ document
#: is already in front of him and a second alert about the same file is noise.
_SPOKEN_MEDIA: dict[str, str] = {
    "audio": "رسالة صوتية",
    "voice": "رسالة صوتية",
    "ptt": "رسالة صوتية",
    "image": "صورة",
    "video": "مقطع",
}


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


def _typed_outcome(text: str | None) -> str | None:
    """The §20 outcome a customer TYPED instead of tapping, or None.

    The map is derived from ``outcome_followup.BUTTONS`` at call time rather
    than copied here: the labels the customer sees and the labels we accept
    are then the same object, and a relabelled button cannot leave a stale
    second spelling behind in this file.

    Matched on the WHOLE folded message and nothing less. `normalize_ar`
    collapses «ما ردّوا» / «ما ردوا» and drops the emoji off «جاني مقابلة 🎉»,
    so the three labels arrive here in one form each — but a message that
    merely CONTAINS one of them is not an answer, and this module's two live
    incidents were both a substring reading of an ordinary word.
    """
    from career.arabic import normalize_ar
    from career.cv import outcome_followup as followup

    folded = normalize_ar(text)
    if not folded:
        return None
    for button_id, label in followup.BUTTONS:
        if normalize_ar(label) == folded:
            return followup.parse_answer(button_id)
    return None


def _start_handover(
    session: Session, *, tenant_id: uuid.UUID, subscription_id: uuid.UUID,
    channel_id: uuid.UUID, onboarding: orchestrator.Deps, now: datetime,
) -> None:
    """Whitepaper §05: the funnel (cv_analysis) or the onboarding journey.

    Extracted so the two callers cannot drift. It runs from
    :func:`_handle_message`'s activation path normally, and from the RESUME
    branch for the one customer the activation path deliberately refused to
    run it for — see :func:`_deferred_handover`.
    """
    from career.db.models import Subscription
    from career.funnel import flow as funnel_flow

    sub = session.get(Subscription, subscription_id)
    start = (
        funnel_flow.start_funnel
        if sub is not None and sub.plan_code == "cv_analysis"
        else orchestrator.start_journey
    )
    start(session, tenant_id=tenant_id, subscription_id=subscription_id,
          channel_id=channel_id, deps=onboarding, now=now)


def _deferred_handover(
    session: Session, channel: CustomerChannel, *,
    onboarding: orchestrator.Deps | None, now: datetime,
) -> None:
    """The handover a silenced buyer's activation held back, run now that he
    has un-silenced himself.

    Held back, never dropped: without this, refusing to onboard him at
    activation would leave a paying customer activated and permanently
    un-onboarded, which is a worse failure than the one it fixes.

    It runs at most once, and the condition is the honest one — NEITHER a
    journey NOR a funnel row exists, i.e. the handover has never run for this
    tenant. A customer who opted out AFTER onboarding has both rows and is
    untouched: nothing here re-prompts anybody who is already somewhere.
    """
    from career.db.models import FunnelSession

    if onboarding is None or channel.subscription_id is None:
        return
    for model in (OnboardingSession, FunnelSession):
        if session.execute(
            select(model.id).where(model.tenant_id == channel.tenant_id).limit(1)
        ).first() is not None:
            return
    _start_handover(
        session, tenant_id=channel.tenant_id,
        subscription_id=channel.subscription_id, channel_id=channel.id,
        onboarding=onboarding, now=now,
    )


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
        inbound, then start the funnel (cv_analysis) or the onboarding.

        ``result.opted_out`` — a customer who paid while his channel is STILL
        silenced by his own standing instruction — is the field
        `activation_flow` added «for the worker's owner to decide», and until
        now nothing in the tree read it. Three things were wrong with running
        the handover for him, and they compound:

        * the activation sends him ONE line saying why nothing will arrive,
          and the consent gate went out on top of it seconds later. The order
          is proven, and it buries the only sentence that explains his silence
          under a conversation he did not ask for;
        * the journey is a CONVERSATION with someone who told us to stop
          talking. Every prompt is a send to a channel on record as opted out;
        * and it runs to completion, so he reaches ACTIVE — «fully onboarded»
          — with every prompt unread, which is a lie told to our own operator
          screens as much as to him.

        So the handover is DEFERRED, not skipped: :func:`_deferred_handover`
        runs it the moment he sends «تشغيل الرسائل». Skipping it outright
        would leave a paying customer activated and permanently un-onboarded.
        """
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
            and not result.opted_out
            and result.status
            in (ActivationStatus.ACTIVATED, ActivationStatus.ALREADY_LINKED)
        ):
            _start_handover(
                session,
                tenant_id=uuid.UUID(result.tenant_id),
                subscription_id=uuid.UUID(result.subscription_id),
                channel_id=uuid.UUID(result.channel_id),
                onboarding=onboarding, now=now,
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
        # hears about it (TEN code only, §15.13). The code gets a LINE of its
        # own (§16, the same shape as `salla.provisioning._announce_renewal`):
        # Fahad's client reverses any line that mixes Arabic with Latin, and
        # the code — the only part that says WHOSE line just went quiet — is
        # exactly the part that moves when it does.
        try:
            admin_client.send_admin(
                "🔇 عميل أوقف الرسائل\n"
                f"{_ten_code(session, tenant_id)}\n"
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
        # The handover his activation held back, if he is that customer.
        # AFTER the confirmation, so «رجعت لك الرسائل» is what he reads first
        # and the consent gate follows it rather than burying it — the exact
        # ordering mistake `_finish_activation` was fixed for.
        _deferred_handover(session, channel, onboarding=onboarding, now=now)
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
    inbound = _record_inbound(
        session, tenant_id=channel.tenant_id, channel_id=channel.id,
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
        # …and everything else used to be answered with NOTHING AT ALL. See
        # `_OPTED_OUT_WAY_BACK`: a near-miss resume («طيب تشغيل الرسائل من
        # فضلك») lands here, and silence to a customer asking to come back is
        # the one reply we cannot defend.
        _reply(whatsapp_client, channel.phone_e164, _OPTED_OUT_WAY_BACK)
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
        # Did this arrive from a CARD, or from a person typing? The two are
        # answered and escalated by different rules below, and the question
        # cannot be asked of `effective` — an interactive reply carries the
        # human label as its title, so it is «readable» text by every other
        # test in this file.
        tapped = (
            message_type in ("button", "interactive")
            or _button_id_of(msg) is not None
        )

        # §20: the answer to the outcome question. Checked BEFORE the delivery
        # buttons and before enrichment for the same reason those two beat
        # enrichment — a reply we asked for must never be eaten by another
        # open conversation, or the datum is lost and cannot be re-collected.
        from career.cv import outcome_followup as followup

        answer = followup.parse_answer(effective)
        # CONSUMES the tap either way. Falling through when nothing was
        # pending sent «oc_interview» into the enrichment branch, whose
        # final else treats unmatched text as the customer's achievement
        # — so a double tap, or a tap on an old card (WhatsApp keeps them
        # tappable forever), wrote a button id into the achievement bank
        # and paid for an LLM call to render it. Constant 5.
        pending = (
            followup.pending_job_ref(session, tenant_id=channel.tenant_id)
            if answer is not None else None
        )
        if answer is None and not tapped:
            # …and the same question answered by TYPING what the button says.
            # `parse_answer` reads machine ids only, so «ما ردّوا» typed by a
            # customer who scrolled past the card — or whose client rendered
            # the buttons as plain text — fell through everything and became a
            # direct-line ticket raised against him: our own question turned
            # into a complaint, and the §20 datum that can never be
            # re-collected thrown away.
            #
            # Unlike a tap, a typed label is consumed ONLY while a question is
            # actually open. A tap can only have come from our card; a
            # sentence is just a sentence, and answering «مسجّلة عندنا 👍» to
            # someone who happened to write «اعتذروا» about something else is
            # the over-eager reading this file has already paid for twice. So
            # a typed label with nothing pending falls through untouched, and
            # is heard as the ordinary message it is.
            typed = _typed_outcome(text_body)
            if typed is not None:
                pending = followup.pending_job_ref(
                    session, tenant_id=channel.tenant_id)
                answer = typed if pending is not None else None
        if answer is not None:
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

        # ── did the customer actually SAY something to us? ──────────────────
        #
        # These two lines used to be one condition with `landed` in it, and
        # that was my mistake and the expensive one. `landed` is a DELIVERY
        # event: it says a held bundle went out on the back of this inbound.
        # It says nothing whatever about what the customer wrote — and since
        # `descend_pending_delivery` fires on ANY inbound while a bundle sits
        # PENDING_WINDOW, the FIRST message after a quiet day is exactly the
        # message that lands one. So the single likeliest message a
        # re-engaging لمّاح+ customer sends — a real question, on the morning
        # his held bundle is waiting — reached NOBODY: no ticket, no page, no
        # card, and the only thing he got back was a wall of unrelated jobs.
        # The two facts are now computed independently and neither can hide
        # the other.
        wrote = readable and not tapped              # he typed words
        spoke = not tapped and message_type in _SPOKEN_MEDIA   # …or recorded them

        if wrote or spoke:
            # لمّاح+ sells «اكتب لي وقت ما تحتاج», and before this the tier had
            # no direct line at all: an ACTIVE customer's ordinary message
            # classified as OTHER, got the fallback, and reached nobody — so the
            # 449 plan reduced to what 199 buys plus a star. It is escalated
            # HERE, the last branch, and not by a keyword: this file carries two
            # live incidents of a command set built on a common Arabic noun
            # («مساعده», and a comma-separated skills answer), and a keyword
            # tight enough to be safe matches almost nothing a person writes.
            #
            # A TAP is never escalated, and that is a rule now rather than an
            # accident of branch order. A tap can only have come from a card WE
            # sent, so it is by construction a reply to our own question — and
            # WhatsApp keeps every card tappable forever, so «مضبوط ✅» from an
            # enrichment card three months old used to open a ticket about a
            # customer who had said nothing. With one open ticket per customer
            # that stale tap then SPENT the slot his real question needed the
            # following week. Nothing is lost by refusing: the operator cannot
            # act on «he tapped a button», and the tier sells writing to him.
            #
            # By the time control reaches here the funnel, the journey, the
            # standing commands, the outcome buttons (tapped AND typed) and the
            # enrichment session have each declined this message, so it is not
            # an answer to a question of ours. `escalate_direct_message` refuses
            # again on its own terms — including, since this audit, while an
            # enrichment session is open — so the guarantee is structural and
            # not an artefact of this branch's position.
            from career.promises import career_session as _career_session

            _career_session.escalate_direct_message(
                session,
                tenant_id=channel.tenant_id,
                channel_id=channel.id,
                now=now,
                # the ticket points at the message that caused it: without it
                # the operator's queue says «somebody is waiting» and nothing
                # about what he is waiting on.
                inbound_message_id=inbound.id,
                unreadable=None if wrote else _SPOKEN_MEDIA[message_type],
                admin_client=admin_client,
            )
            # …and nothing is added to what the customer hears. Whatever the
            # branches below would have said to him, they still say: nothing
            # here guarantees a human is awake, and «أحد من الفريق بيتواصل
            # معك» from a robot at 2am is the promise this escalation exists
            # to stop making falsely.

        # closure audit: an ACTIVE customer who typed anything else got NO
        # reply at all — five realistic messages produced zero outbound. A
        # paying customer must never wonder whether we are still here. The
        # reply now tells THIS customer what they will actually receive:
        # only a live pass hears the daily promise.
        if wrote:
            # Answered even when a bundle just landed, for the same reason the
            # escalation is: the bundle is tonight's scheduled delivery, not a
            # reply to a sentence somebody typed. «وصلتني رسالتك 👌» is the only
            # line in this system that answers the message itself.
            _reply(whatsapp_client, channel.phone_e164,
                   _fallback_text(session, channel.tenant_id))
        elif is_document:
            # the file is not silence-worthy even when a held bundle landed:
            # an updated CV dropped without a word meant every later CV was
            # built from the stale profile and nobody knew.
            _reply(whatsapp_client, channel.phone_e164, _DOCUMENT_ACTIVE)
            try:
                admin_client.send_admin(
                    "📎 عميل أرسل ملفًا بعد التفعيل\n"
                    f"{_ten_code(session, channel.tenant_id)}\n"
                    "قد تكون سيرة محدّثة، راجعه"
                )
            except Exception:  # noqa: BLE001 — alerting never blocks the reply
                logger.warning("document notice failed", exc_info=True)
        elif spoke:
            # a voice note / photo / clip from a paying customer: the same
            # silence, one type further out — answered, never pretended. Not
            # gated on `landed` either: he is being told we cannot open what he
            # sent, and a bundle landing does not make that less true.
            _reply(whatsapp_client, channel.phone_e164,
                   _media_reply(message_type, _MEDIA_ACTIVE))
        elif landed is None:
            # What is left is a TAP we matched nothing to, or a shape that is
            # not a message at all — a sticker, a reaction, a location, a
            # contact card. Both are answered only when no bundle landed:
            # a tap's whole purpose is to open the window, and the bundle it
            # just pulled down IS its answer.
            _reply(whatsapp_client, channel.phone_e164,
                   _fallback_text(session, channel.tenant_id) if tapped
                   else _media_reply(message_type, _MEDIA_ACTIVE))
    session.commit()


#: THE receipt ladder — one authority, and this module is it, because this is
#: the only place a receipt is ever WRITTEN to a delivery_messages row.
#: ``scripts/replay_lost_events.py`` imports it rather than keeping a second
#: copy: it shipped with its own ladder that ordered `failed` ABOVE `read`,
#: which is the reverse of this one on the single comparison that matters, and
#: the consequence was that the recovery tool refused to recover the exact
#: class of receipt it exists for — a `delivered`/`read` destroyed while the
#: row sat `failed` was skipped as «already at or past this receipt», leaving
#: a message the customer had read on record as a failure and out of the
#: spend. Two ladders that disagree are not two opinions; one of them is a
#: bug, and there is a test that fails the day a second copy appears again.
#:
#: Meta does NOT guarantee the ORDER in which delivery receipts arrive —
#: retries and webhook redelivery routinely hand us a «sent» after a «read» —
#: so a receipt is a claim about a POINT on this ladder, never «the current
#: truth», and only a higher rung may be written.
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
RECEIPT_ORDER: dict[str, int] = {
    "sent": 1, "failed": 2, "delivered": 3, "read": 4,
}


def receipt_rank(status: str | None) -> int:
    """Where a receipt stands on :data:`RECEIPT_ORDER`; 0 for anything we do
    not know. Public because the replay tool has to answer the same question
    about the same rows and must answer it identically."""
    return RECEIPT_ORDER.get(str(status or "").lower(), 0)


def _handle_status(session: Session, st: dict[str, Any], *, now: datetime) -> None:
    wamid = st.get("id")
    status = st.get("status")
    if not wamid or not status:
        return
    rank = receipt_rank(status)
    if rank == 0:
        # DELIBERATELY still not written. The column feeds two money paths —
        # `cv/close.whatsapp_spend` bills everything whose status is not
        # `failed`, and the watchtower's message-status panel is read as the
        # truth about what reached customers — so a status nobody has placed
        # on the ladder would be billed and displayed by accident, on the
        # strength of a name we have never seen. Refusing to guess is right.
        #
        # Whispering about it is not. This was a `logger.warning`, and warnings
        # do not leave this box: the operator's harvester forwards «ERROR:»
        # lines only (the same rule that put the Meta code on the ERROR line in
        # cv/daily_run). A rung Meta adds that we never record is a permanent
        # blind spot in the delivery ledger, and the only way anyone finds out
        # is if this line is loud enough to arrive.
        logger.error(
            "unknown whatsapp receipt status %r — NOT recorded; add it to "
            "RECEIPT_ORDER or the ledger stays blind to it", status,
        )
    for dm in session.execute(
        select(DeliveryMessage).where(DeliveryMessage.wa_message_id == wamid)
    ).scalars():
        if rank <= receipt_rank(dm.status):
            continue  # out of order, or the same receipt twice — a no-op
        dm.status = str(status)
        # the stamp belongs to the receipt that actually WON, so a superseded
        # duplicate never makes a row look freshly updated
        dm.status_updated_at = now


#: How many times ONE event may be attempted before the worker stops trying.
#: Five is the first attempt plus the whole ladder below: a Meta 5xx or a
#: Claude timeout still failing forty minutes later is not a blip, and going on
#: past that spends real Claude calls reliving the same failure.
MAX_ATTEMPTS = 5

#: The wait before attempts 2, 3, 4 and 5. The first step is far longer than
#: the loop's 3-second poll on purpose: a lone failing event left 'received'
#: with no clock comes straight back to the front of the queue, and twenty
#: attempts a minute — each possibly an LLM turn — is exactly the retry storm
#: that made `failed` terminal in the first place. The last step is only half
#: an hour so a renewed token or a recovered Meta is picked up on its own,
#: without the operator doing anything.
_BACKOFF_SECONDS = (30, 120, 600, 1800)

def _retryable_http(status: int) -> bool:
    """The statuses Meta or Anthropic return that mean «ask again later».

    408, 409, 425 and 429 are the Anthropic SDK's own retry set and mean the
    same thing at Meta; anything 5xx is their side of the wire. Everything
    else — 400, 401, 403, 404 — is a statement about the REQUEST, and asking
    again with the same request gets the same answer, five times, on a clock.
    """
    return status in (408, 409, 425, 429) or status >= 500


def _graph_http_status(exc: BaseException) -> int | None:
    """The HTTP status inside a :class:`WhatsAppSendError`, if it carries one.

    The client flattens Meta's answer into the exception TEXT («graph HTTP 503
    (code 131026)») and keeps no attribute, so reading it back out of the
    string is the only way to tell a 503 from a 400 without editing a module
    this change does not own. The word boundary matters: Meta's own numeric
    error code sits in the same string and is five or six digits long, so it
    can never be mistaken for a status. A message with no status at all —
    «no storage wired for document refs» — returns None and is treated as
    poison, which is right: that is a wiring bug, not weather.
    """
    match = re.search(r"\b([1-5]\d\d)\b", str(exc))
    return int(match.group(1)) if match else None


def _is_transient(exc: BaseException) -> bool:
    """Would attempting this event again plausibly succeed?

    The two-tier model is `salla/provisioning.py`'s and the vocabulary is
    deliberately the same one: a TRANSIENT failure is the world being briefly
    unavailable and the event is fine, so it is deferred and tried again; a
    POISONED event is one we could actually look at and could not process, so
    it is terminal and the operator hears about it. What differs from Salla is
    which side is the safe default, and here it is POISON.

    Salla's default is «retry, never destroy a paid order»: an order dropped
    on the floor is money taken for a service never provisioned, and a Salla
    retry re-reads an API and writes idempotently — it cannot embarrass anyone.
    An inbound WhatsApp turn is the opposite trade. A retry re-runs a
    conversation: it can re-answer a customer, re-ask a question they already
    answered, and pay for another Claude call to do it. And a message that is
    never re-driven is not lost the way an order is — the customer is a live
    human in an open 24h window who writes again, and the operator is told the
    same minute. So an exception type we have not thought about lands as
    poison, and only a named, argued list is retried:

    * `WhatsAppSendError` carrying a 429 or a 5xx — Meta refused the send
      itself, so nothing reached the customer and repeating it is honest.
      Carrying a 4xx (a bad template name, an expired token) it is poison.
    * Anything from the `anthropic` SDK with no status (a connection reset, a
      timeout) or with a retryable one. A `RateLimitError` at 06:00 during the
      daily fan-out is the single likeliest transient failure this worker has.
    * `sqlalchemy` `OperationalError` / `InterfaceError` — the connection died
      or the server restarted. `IntegrityError`, `ProgrammingError` and
      `DataError` are deterministic: the same rows and the same SQL fail the
      same way forever.
    * `OSError`, which in this codebase means the transport layer: the builtin
      `TimeoutError` and `ConnectionError` are subclasses of it, and so is
      every `requests` transport exception.

    `AttributeError`, `KeyError`, `TypeError`, `ValueError` — the shapes a
    malformed Meta payload and a bug in our own routing both take — are poison
    by falling off the end, which is the behaviour the 2026-07-19 row and
    `test_poisoned_event_is_isolated_not_retried_forever` already expect.

    This function is called from an `except` block and must never raise: a
    classifier that throws would turn one lost message into a wedged queue.
    """
    try:
        if isinstance(exc, WhatsAppSendError):
            status = _graph_http_status(exc)
            return status is not None and _retryable_http(status)

        module = (type(exc).__module__ or "").split(".")[0]
        if module == "anthropic":
            # The SDK raises APIConnectionError / APITimeoutError with no
            # status and APIStatusError subclasses with one; both are honest
            # about which they are, so neither needs importing to be read.
            status = getattr(exc, "status_code", None)
            if not isinstance(status, int):
                return True
            return _retryable_http(status)

        # `retryable` is SallaApiError's own word for exactly this question —
        # the fallback text path reads the subscription through the salla
        # package, so its errors can surface here.
        retryable = getattr(exc, "retryable", None)
        if isinstance(retryable, bool):
            return retryable

        from sqlalchemy.exc import InterfaceError, OperationalError

        if isinstance(exc, OperationalError | InterfaceError):
            return True
        return isinstance(exc, OSError)
    except Exception:  # noqa: BLE001 — an unclassifiable failure is poison
        logger.warning("failure classification failed", exc_info=True)
        return False


def _failure_detail(exc: BaseException) -> str:
    """The exception's CLASS name, and never `str(exc)`.

    This value is written to a row the operator reads. Exception messages in
    this codebase routinely carry a phone number, a Meta id or a fragment of
    what the customer typed, and constant 13 admits neither to a log nor to
    the admin channel. A class name is a fact about our code, not about them.
    """
    cls = type(exc)
    module = (cls.__module__ or "").split(".")[0]
    return f"{module}.{cls.__name__}"[:64]


class _SendLedger:
    """Has this attempt already said something the retry would say again?

    THE constraint on retrying an inbound turn: a second attempt re-runs the
    payload from the top, and `_handle_message` skips a message only when its
    `inbound_messages` row is on disk (`_inbound_exists`). Everything a failed
    attempt did to the database is rolled back — everything it said to the
    customer is not. So the question that decides whether a retry is allowed
    is not «what kind of exception» but «did we already speak, with nothing
    committed to prove it».

    It is answered by counting rather than by listing paths, because a list of
    safe paths is a list that rots the first time a branch grows a send. Every
    `send*` call on the wrapped client is counted, and the count is reset at
    the start of each message: sends made for an EARLIER message in the same
    event do not block the retry, because that message committed its inbound
    row and the retry will skip it entirely.

    A send that raised is counted only when delivery is genuinely unknowable.
    A `WhatsAppSendError` carrying an HTTP status is Meta's complete refusal —
    the message was never accepted, so retrying it duplicates nothing, and
    that is precisely the Meta-5xx case this whole change exists to retry. A
    timeout or a dropped connection is the opposite: the send may well have
    landed, and we do not repeat what we cannot rule out.

    ``unrecorded`` is the sticky half. A path that speaks WITHOUT writing an
    inbound row — the «أرسل رمز التفعيل» reply to an unknown number, a
    samples reply, an activation that sent a welcome before it could commit —
    leaves nothing for the retry to skip on, so from that point the whole
    event is unsafe even if a later message fails cleanly.
    """

    def __init__(self, inner: WhatsAppClient) -> None:
        self._inner = inner
        self._message_sends = 0
        self._unrecorded = False

    def start_event(self) -> None:
        self._message_sends = 0
        self._unrecorded = False

    def start_message(self) -> None:
        self._message_sends = 0

    def note_unrecorded(self) -> None:
        self._unrecorded = True

    @property
    def spoke(self) -> bool:
        """Did the message being handled right now already send something?"""
        return self._message_sends > 0

    @property
    def would_repeat(self) -> bool:
        """Would a retry of this event put a message in front of a customer
        that the failed attempt already put there?"""
        return self._unrecorded or self._message_sends > 0

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):           # never proxy our own internals
            raise AttributeError(name)
        attr = getattr(self._inner, name)
        if not name.startswith("send") or not callable(attr):
            return attr                    # download_media and friends: pass

        def _counted(*args: Any, **kwargs: Any) -> Any:
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — re-raised untouched
                refused = (isinstance(exc, WhatsAppSendError)
                           and _graph_http_status(exc) is not None)
                if not refused:
                    self._message_sends += 1   # delivery unknowable
                raise
            self._message_sends += 1
            return result

        return _counted


def _defer_event(ev: WebhookEvent, *, attempts: int, now: datetime,
                 exc: BaseException) -> None:
    """Leave the event exactly where it is, behind a clock.

    The counterpart of :func:`_dead_letter`, and `salla/provisioning.py`'s
    `_defer_webhook` with the one thing that file does not need: a time.
    Salla defers WHOLESALE — the condition it retries on (Salla unreachable)
    applies to every waiting order at once, so it breaks the batch and sleeps
    the process. A WhatsApp event fails alone while every other event around
    it is healthy, so «still 'received'» without «not before» is a three-second
    retry loop. `processing_status` stays 'received' — the only status the
    query below selects — and `processed_at` stays NULL, because it was not.
    """
    ev.attempt_count = attempts
    ev.next_attempt_at = now + timedelta(
        seconds=_BACKOFF_SECONDS[min(attempts - 1, len(_BACKOFF_SECONDS) - 1)]
    )
    ev.failure_kind = "transient"
    ev.failure_detail = _failure_detail(exc)


def _dead_letter(session: Session, ev: WebhookEvent, admin_client: Any, *,
                 kind: str, attempts: int, exc: BaseException) -> None:
    """Stop trying, say so out loud, and leave the row able to explain itself.

    Terminal is still `processing_status = 'failed'`: it is the status
    `scripts/replay_lost_events.py` selects and the status its safety rule is
    written against (it refuses to re-drive a `failed` MESSAGES event, on the
    reasoning this class implements — the worker may already have answered the
    customer). Inventing a second terminal name would make every existing
    reader blind to half the dead. The reason lives in `failure_kind` instead:

    `poison`        — an event we could look at and could not process.
    `exhausted`     — retryable, retried, still failing after MAX_ATTEMPTS.
    `already_spoke` — retryable, but this attempt had already sent something
                      to the customer, so a retry would say it twice.

    «Visible» means three things, because a status nobody reads is what the
    2026-07-19 row proves: an ERROR line (the watchtower's error screen
    harvests our own ERROR lines out of journalctl — a WARNING would not
    arrive), a Telegram notice naming WHOSE turn was lost by TEN code, and the
    row itself, which :func:`dead_letter_summary` reads for a console screen.
    """
    session.rollback()
    ev.processing_status = "failed"
    ev.attempt_count = attempts
    ev.processed_at = func.now()
    ev.next_attempt_at = None          # nothing is waiting on a clock any more
    ev.failure_kind = kind
    ev.failure_detail = _failure_detail(exc)
    logger.error(
        "whatsapp event dead-lettered (%s, attempt %d): %s",
        kind, attempts, _failure_detail(exc), exc_info=True,
    )
    reason = {
        "poison": "الحدث نفسه ما نقدر نعالجه",
        "exhausted": "جرّبنا كل المحاولات وما نجحت",
        "already_spoke": "رددنا على العميل قبل الفشل، وما نعيد عشان ما نرسل مرتين",
    }.get(kind, "توقفنا عن المحاولة")
    try:
        # WHOSE turn was lost. The unnamed notice made the operator read the
        # journal to find out, and «راجع السجل» is not an instruction anyone
        # can act on at 6am. The TEN codes sit on their own line: a Latin code
        # inside an Arabic sentence is scrambled by his client (§16).
        admin_client.send_admin(
            "⚠️ رسالة واتساب واردة توقّفنا عن معالجتها\n"
            f"{_event_ten_codes(session, ev.payload or {})}\n"
            f"السبب: {reason}"
        )
    except Exception:  # noqa: BLE001
        logger.warning("admin note failed", exc_info=True)


def dead_letter_summary(session: Session, *, provider: str = "whatsapp",
                        ) -> dict[str, int]:
    """The dead letter queue, counted by reason — PII-free by construction.

    Written for the watchtower: the console owns no screen for this yet, and
    a dead letter nobody can list is the failure this change exists to end.
    Returns `{failure_kind: count}` plus `total`, so a screen is a render
    away and an operator with a psql prompt has the same answer.
    """
    rows = session.execute(
        select(WebhookEvent.failure_kind, func.count(WebhookEvent.id))
        .where(WebhookEvent.provider == provider,
               WebhookEvent.processing_status == "failed")
        .group_by(WebhookEvent.failure_kind)
    ).all()
    summary = {str(kind or "unknown"): int(count) for kind, count in rows}
    summary["total"] = sum(summary.values())
    return summary


def process_pending_whatsapp(
    owner_session: Session, *,
    whatsapp_client: WhatsAppClient, admin_client: TelegramAdminClient,
    now: datetime, limit: int = 100,
    onboarding: orchestrator.Deps | None = None,
) -> dict[str, int]:
    events = list(owner_session.execute(
        select(WebhookEvent)
        .where(WebhookEvent.provider == "whatsapp",
               WebhookEvent.processing_status == "received",
               # a deferred event is invisible until its clock runs out, so a
               # backing-off row can never hold the head of the queue (the
               # batch is ordered by received_at and `limit` is what it fills)
               or_(WebhookEvent.next_attempt_at.is_(None),
                   WebhookEvent.next_attempt_at <= now))
        .order_by(WebhookEvent.received_at)
        .limit(limit)
    ).scalars().all())

    # Every send this worker can cause goes through the ledger — including the
    # conversation's own, which is the majority of them: the orchestrator and
    # the funnel send through `Deps.whatsapp_client`, so handing them the raw
    # client would have left the LLM branches, the ones that actually time
    # out, invisible to the retry-safety check.
    ledger = _SendLedger(whatsapp_client)
    deps = (None if onboarding is None
            else dataclasses.replace(onboarding, whatsapp_client=ledger))

    counts = {"messages": 0, "statuses": 0, "failed": 0, "deferred": 0}
    for ev in events:
        # audit fix: one poisoned event must never wedge the whole queue —
        # without this isolation a single raise left the event 'received'
        # and every later customer frozen behind an infinite retry.
        ledger.start_event()
        try:
            payload: dict[str, Any] = ev.payload or {}
            for entry in payload.get("entry", []) or []:
                for change in entry.get("changes", []) or []:
                    value = change.get("value", {}) or {}
                    for msg in value.get("messages", []) or []:
                        ledger.start_message()
                        _handle_message(owner_session, msg,
                                        whatsapp_client=ledger,
                                        admin_client=admin_client, now=now,
                                        onboarding=deps)
                        counts["messages"] += 1
                        if ledger.spoke and not _inbound_exists(
                                owner_session, str(msg.get("id") or "")):
                            # it answered and recorded nothing, so a retry has
                            # nothing to skip on — the whole event is now
                            # unsafe to repeat, however the next one fails
                            ledger.note_unrecorded()
                    for st in value.get("statuses", []) or []:
                        _handle_status(owner_session, st, now=now)
                        counts["statuses"] += 1
            ev.processing_status = "processed"
            ev.processed_at = func.now()
            ev.attempt_count = ev.attempt_count + 1
            ev.next_attempt_at = None
            # failure_kind is deliberately NOT cleared: a row that succeeded on
            # its third attempt should still say what it survived.
        except Exception as exc:  # noqa: BLE001 — classify, record, move on
            attempts = ev.attempt_count + 1
            if not _is_transient(exc):
                _dead_letter(owner_session, ev, admin_client,
                             kind="poison", attempts=attempts, exc=exc)
                counts["failed"] += 1
            elif ledger.would_repeat:
                # The one refusal that is not about the exception at all. This
                # is the same rule `replay_lost_events.py` applies by hand to
                # a `failed` messages event — «the worker already sent, then
                # rolled back; a replay would message the customer twice» —
                # except that here the worker KNOWS, so it does not have to
                # assume the worst about every event of that shape.
                _dead_letter(owner_session, ev, admin_client,
                             kind="already_spoke", attempts=attempts, exc=exc)
                counts["failed"] += 1
            elif attempts >= MAX_ATTEMPTS:
                _dead_letter(owner_session, ev, admin_client,
                             kind="exhausted", attempts=attempts, exc=exc)
                counts["failed"] += 1
            else:
                # The world is briefly broken and this event is fine. Nothing
                # terminal, nothing thrown away, nobody paged — the operator
                # does not need to know that Meta was slow for thirty seconds.
                owner_session.rollback()
                _defer_event(ev, attempts=attempts, now=now, exc=exc)
                counts["deferred"] += 1
                logger.warning(
                    "whatsapp event deferred (attempt %d): %s",
                    attempts, _failure_detail(exc), exc_info=True,
                )
        owner_session.commit()
    return counts
