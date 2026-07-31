"""The onboarding orchestrator — one resumable conversation (whitepaper §05).

Wires the C5 building blocks into the C4 message loop: every inbound text or
document from a channel whose journey is incomplete lands here, is routed by
the journey's FSM state (plus a sub-cursor in ``context``), advances exactly
one step, and replies in Arabic. Everything is presence-driven, so the
conversation resumes from persisted state after any interruption.

Documented design decisions:

- **CONSENT_PENDING hosts consents AND the eleven questions.** The whitepaper's
  ten states have no basic-data state, and §05 orders consents → basic data →
  upload; both sub-flows therefore live inside CONSENT_PENDING with the
  sub-cursor (``context.phase``) distinguishing them. Exit → CV_UPLOAD_PENDING.
- **CV_PROCESSING is transient** — handled inline within the document turn:
  §11 pipeline → identity-stripped extraction → PROFILE_CONFIRMATION, or the
  documented regression back to CV_UPLOAD_PENDING with honest Arabic reasons.
- **Corrections are free text**: «تعديل» asks the customer to type the fix,
  which is stored as ``payload.customer_correction`` on top of the original
  (the original payload is preserved by the confirmation authority).
- **Standing privacy commands work at every state** («حالة اشتراكي», «وقف
  مؤقت», «تصدير بياناتي», «حذف بياناتي»). Deletion is two-step: the first
  message warns and asks for the exact phrase «أؤكد حذف بياناتي».
- **Button replies match by option id or Arabic label** — Meta interactive
  replies carry the tapped title; tests and future id-carrying payloads use
  ids; both resolve.
- «دعم» never reaches this module — the C4 worker escalates it first (§05:
  الدعم يقاطع من كل حالة).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import (
    CareerPathAssessment,
    CustomerChannel,
    CustomerProfile,
    OnboardingSession,
    ProfileFact,
)
from career.onboarding import collection, confirmation, consents, fsm, paths, policy, privacy
from career.onboarding.consents import ConsentMissing
from career.onboarding.extraction import (
    ExtractionFailed,
    ExtractorClient,
    run_extraction,
)
from career.onboarding.upload import Limits, MalwareScanner, process_cv_upload
from career.storage import StorageAdapter
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.delivery import record_out
from career.whatsapp.templates import ONBOARDING_REMINDER

logger = logging.getLogger("career.onboarding")


@dataclass
class Deps:
    """Everything the orchestrator talks to — injectable, no globals."""

    whatsapp_client: WhatsAppClient
    scanner: MalwareScanner
    storage: StorageAdapter
    extractor: ExtractorClient
    upload_limits: Limits | None = None
    #: F-ENRICH renderer (colloquial answer → grounded English bullet). None
    #: disables the enrichment branch — the rest of onboarding is unaffected.
    achievement_renderer: Any = None
    #: F-ENRICH icebreaker examples writer (3 scrubbed colloquial examples).
    #: None → the opening question ships without the examples menu.
    examples_writer: Any = None
    #: F-PANEL judge (§14-أ). None → the longest grounded candidate wins, so
    #: the customer is never blocked by a missing reviewer.
    bullet_judge: Any = None
    #: F-INTENT classifier (§15). None → the deterministic keyword classifier
    #: is used alone; understanding degrades, the conversation never breaks.
    intent_classifier: Any = None


class JourneyNotFound(Exception):
    """No onboarding journey exists for this tenant/channel."""


# ── Arabic copy ──────────────────────────────────────────────────────────────

_CONSENT_YES = "أوافق"
_BATCH_CONFIRM_ALL = "تأكيد الكل"
_BATCH_FIX_ITEM = "تعديل بند"
_BATCH_FIX_PROMPT = (
    "أرسل رقم البند والتصحيح في رسالة واحدة\n"
    "(مثال: 2 المسمى الصحيح هو Lead Business Analyst)"
)
_CONSENT_NO = "لا أوافق"
_ROADMAP = (
    "تمت الموافقة ✅\n"
    "رحلتك من هنا قصيرة:\n"
    "1️⃣ أسئلة سريعة (~3 دقائق، أغلبها أزرار)\n"
    "2️⃣ ترسل سيرتك الذاتية\n"
    "3️⃣ تأكيد سريع بضغطة\n"
    "4️⃣ ننطلق نبحث لك يوميًا 🚀"
)
_READING_CV = "استلمناها 📄 نقرأ سيرتك الآن — ثوانٍ وأرجع لك…"
_CONSENT_REQUIRED_EXPLAIN = (
    "هذي الموافقة ضرورية لتشغيل الخدمة — بدونها ما نقدر نكمل. "
    "لو عندك سؤال أرسل: دعم"
)
_UPLOAD_PROMPT = (
    "ممتاز، خلصنا الأسئلة 👌\n"
    "الحين أرسل سيرتك الذاتية كملف PDF أو Word (DOCX) وبنقرأها ونرجع لك "
    "بالمعلومات للتأكيد."
)
_UPLOAD_REJECTED = (
    "ما قدرنا نقبل الملف — تأكد أنه PDF أو DOCX سليم وبدون حماية، "
    "وأعد إرساله من فضلك."
)
_EXTRACTION_FAILED = (
    "استلمنا ملفك لكن تعذّرت قراءته الآن. أعد المحاولة بعد قليل، "
    "أو أرسل: دعم"
)
_CORRECTION_PROMPT = "اكتب التصحيح كما تريده أن يظهر:"
_GAP_EXPERIENCE = "قبل نكمل: وش أبرز خبرة عملية عندك؟ (المسمى وجهة العمل)"
_GAP_SKILL = "وش أهم مهارة مهنية عندك؟"
_DONE = (
    "انطلقنا! 🚀\n"
    "من الليلة نبحث لك كل يوم، ومن الأحد إلى الخميس صباحًا توصلك "
    "فرصك المختارة — كل فرصة معها سيرة ذاتية جاهزة باسمك، مفصّلة لها "
    "بالذات.\n"
    "لما تقدّم على وظيفة اضغط «قدمت» تحتها — يساعدنا نطوّر اختياراتنا "
    "لك.\n"
    "أوامر تفيدك بأي وقت: حالة اشتراكي · وقف مؤقت · دعم"
)
_DELETE_WARNING = (
    "⚠️ حذف بياناتك يزيل ملفك المهني وسيرتك وسجل المحادثة نهائيًا "
    "(تبقى السجلات المالية وسجل الموافقات لمتطلبات نظامية).\n"
    "إذا متأكد، أرسل حرفيًا: أؤكد حذف بياناتي"
)
_DELETE_DONE = "تم حذف بياناتك الشخصية. سجلاتك المالية محفوظة وفق المتطلبات النظامية."
_PAUSED = "تم الإيقاف المؤقت. الفترة الحالية لا تتمدد (§ الشروط). للعودة أرسل: استئناف"
_RESUMED = "تم الاستئناف ✅"
_EXPORT_ACK = "📁 هذي نسخة كاملة من بياناتك المحفوظة عندنا (ملف JSON)."
_NUDGE_REMINDER = "وقفنا عند خطوة بسيطة — نكمل إعداد خدمتك؟ 👇"
#: §15: after serving an off-topic ask mid-enrichment, tell the customer the
#: line is still waiting — nothing was lost, and they choose when to return.
_ENRICH_RESUME_HINT = (
    "وسطرك اللي كنا نجهزه محفوظ زي ما هو 👌\n"
    "متى ما حبيت نكمّله، اكتب لي وأنا جاهز"
)
_ENRICH_SUPPORT_HINT = (
    "أبشر — أرسل كلمة «دعم» وأوصلك بأحد من الفريق 🙏\n"
    "وسطرك اللي كنا نجهزه محفوظ زي ما هو"
)

_APPROVE_BUTTONS = ("اعتماد المقترح", "أبي مساري كما هو")
_POLICY_BUTTON = "تأكيد وبدء البحث"

# button/command ids accepted besides the Arabic labels
_APPROVE_IDS = {"approve", _APPROVE_BUTTONS[0]}
_OVERRIDE_IDS = {"override", _APPROVE_BUTTONS[1]}
_POLICY_IDS = {"confirm_policy", _POLICY_BUTTON}
_VERDICT_IDS = {
    "confirm": "confirm", "صحيح ✅": "confirm", "صحيح": "confirm",
    "correct": "correct", "يحتاج تعديل ✏️": "correct", "تعديل": "correct",
    "reject": "reject", "احذفها ❌": "reject", "احذفها": "reject",
}

#: Greetings / resume-nudges: never swallowed as free-text answers — the
#: pending question is re-asked instead (resumability UX, §05).
_GREETINGS = frozenset(
    {"مرحبا", "مرحبًا", "هلا", "اهلا", "أهلا", "السلام عليكم", "سلام",
     "نكمل", "كمل", "وين وقفنا", "hi", "hello", "hey"}
)

_PRIVACY_COMMANDS = {
    "حالة اشتراكي": "status",
    "وقف مؤقت": "pause",
    "استئناف": "resume",
    "تصدير بياناتي": "export",
    "حذف بياناتي": "delete_warn",
    "أؤكد حذف بياناتي": "delete_execute",
}


# ── plumbing ─────────────────────────────────────────────────────────────────


def get_journey(session: Session, *, tenant_id: uuid.UUID) -> OnboardingSession:
    journey = session.execute(
        select(OnboardingSession).where(OnboardingSession.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if journey is None:
        raise JourneyNotFound(str(tenant_id))
    return journey


def handle_standing_command(
    session: Session, *, channel_id: uuid.UUID, text: str,
    deps: Deps, now: datetime,
) -> bool:
    """§05/§12: the standing privacy commands work FOR LIFE — audit fix:
    they were only reachable while a journey was incomplete, so an ACTIVE
    customer (the permanent condition) could never pause/export/delete.
    Returns True when the text was a privacy command and was handled."""
    command = _PRIVACY_COMMANDS.get(text.strip())
    if command is None:
        return False
    channel = session.get(CustomerChannel, channel_id)
    if channel is None:
        return False
    # AUDIT ك-7: no journey required — funnel customers (cv_analysis) have
    # no OnboardingSession, yet the §05/§12 lifetime commands are theirs too.
    _handle_privacy_command(session, channel.tenant_id, channel, deps,
                            command, now=now)
    session.flush()
    return True


def handle_enrichment(
    session: Session, *, channel_id: uuid.UUID, text: str,
    deps: Deps, now: datetime,
) -> bool:
    """F-ENRICH (§13): route a reply while an enrichment session is open.
    Returns True when handled. No-op (False) when nothing is open or the
    renderer isn't wired — the caller falls through to its other branches."""
    from career.onboarding import enrichment as enr

    if deps.achievement_renderer is None:
        return False
    channel = session.get(CustomerChannel, channel_id)
    if channel is None:
        return False
    journey = session.execute(
        select(OnboardingSession).where(
            OnboardingSession.tenant_id == channel.tenant_id
        )
    ).scalar_one_or_none()
    if journey is None or journey.state != "ACTIVE":
        return False
    context = dict(journey.context or {})
    state = context.get("enrichment") or {}
    if not state.get("open"):
        return False

    tenant_id = channel.tenant_id
    body = text.strip()
    profile = session.execute(
        select(CustomerProfile).where(CustomerProfile.tenant_id == tenant_id)
    ).scalars().first()
    name = profile.cv_full_name if profile else None
    role_id = uuid.UUID(str(state["current"])) if state.get("current") else None
    pending = state.get("pending_fact_id")

    def _send(msg: str) -> None:
        mid = deps.whatsapp_client.send_text(channel.phone_e164, msg)
        record_out(session, tenant_id=tenant_id, channel_id=channel.id,
                   kind="text", wa_message_id=mid, now=now)

    def _send_buttons(msg: str, buttons: Any) -> None:
        mid = deps.whatsapp_client.send_interactive(
            channel.phone_e164, msg, buttons
        )
        record_out(session, tenant_id=tenant_id, channel_id=channel.id,
                   kind="interactive", wa_message_id=mid, now=now)

    # awaiting confirmation of a rendered bullet
    if pending:
        if enr.matches(body, enr.OK_LABELS) and role_id is not None:
            enr.confirm_answer(session, tenant_id=tenant_id,
                               pending_fact_id=uuid.UUID(str(pending)),
                               role_fact_id=role_id, now=now)
            enr.close_session(context)
            _send(enr.ack_thanks(name))
        elif enr.matches(body, enr.DEL_LABELS):
            enr.reject_answer(session, tenant_id=tenant_id,
                              pending_fact_id=uuid.UUID(str(pending)))
            context["enrichment"] = {
                **state, "pending_fact_id": None, "draft_fact_id": None,
            }
            _send_buttons(enr._DELETED_ASK, enr.RETRY_BUTTONS)
        elif enr.matches(body, enr.EDIT_LABELS):
            # keep draft_fact_id: the next render reuses the row (no orphans)
            context["enrichment"] = {**state, "pending_fact_id": None}
            _send_buttons(enr._EDIT_PROMPT, enr.RETRY_BUTTONS)
        elif enr.matches(body, enr.AGAIN_LABELS):
            context["enrichment"] = {**state, "pending_fact_id": None}
            _regenerate(session, deps, channel, context, role_id, "rephrase",
                        name, now, _send, _send_buttons)
        else:
            from career.onboarding import intent as intent_mod

            resolved, topic = _resolve_enrichment_intent(
                session, deps, tenant_id=tenant_id, body=body, state=state,
                pending=pending, known_name=name,
            )
            if resolved == intent_mod.OFF_TOPIC and _serve_off_topic(
                session, deps, channel, topic=topic, body=body, now=now,
                send=_send,
            ):
                session.flush()
                return True          # cursor untouched: they resume as-is
            if resolved == intent_mod.STOP and role_id is not None:
                enr.skip_role(session, tenant_id=tenant_id,
                              role_fact_id=role_id, now=now)
                enr.close_session(context)
                _send(enr._ACK_SKIP)
                journey.context = context
                session.flush()
                return True
            if resolved == intent_mod.DISPUTE:
                # they say the draft misstates something — never argue: drop it
                # and rebuild from their own words (constant 5 in spirit).
                enr.reject_answer(session, tenant_id=tenant_id,
                                  pending_fact_id=uuid.UUID(str(pending)))
                context["enrichment"] = {
                    **state, "pending_fact_id": None, "draft_fact_id": None,
                }
                _send_buttons(enr._DISPUTED_ASK, enr.RETRY_BUTTONS)
                journey.context = context
                session.flush()
                return True
            if resolved == intent_mod.QUESTION:
                _send_buttons(enr._ANSWER_MY_QUESTION, enr.RETRY_BUTTONS)
                journey.context = context
                session.flush()
                return True
            if resolved == intent_mod.AFFIRM:
                # warmth, not consent (constant 5) — re-show and ask for the tap
                draft = enr.draft_of(
                    session, tenant_id=tenant_id,
                    fact_id=uuid.UUID(str(pending)),
                )
                if draft is not None:
                    _send_buttons(enr.confirm_again_prompt(*draft),
                                  enr.CONFIRM_BUTTONS)
                else:
                    _send_buttons(enr._NEED_A_BIT_MORE, enr.RETRY_BUTTONS)
            elif resolved == intent_mod.REVISE:
                context["enrichment"] = {**state, "pending_fact_id": None}
                _regenerate(session, deps, channel, context, role_id,
                            enr.classify_edit_intent(body), name, now,
                            _send, _send_buttons)
            else:
                context["enrichment"] = {**state, "pending_fact_id": None}
                _handle_enrichment_text(session, deps, channel, context,
                                        role_id, body, name, now, _send,
                                        _send_buttons)
        journey.context = context
        session.flush()
        return True

    # awaiting the achievement answer (or a skip)
    if enr.matches(body, enr.SKIP_LABELS) and role_id is not None:
        enr.skip_role(session, tenant_id=tenant_id, role_fact_id=role_id, now=now)
        enr.close_session(context)
        _send(enr._ACK_SKIP)
        journey.context = context
        session.flush()
        return True

    if enr.matches(body, enr.AGAIN_LABELS) and state.get("arabic_source"):
        _regenerate(session, deps, channel, context, role_id, "rephrase",
                    name, now, _send, _send_buttons)
        journey.context = context
        session.flush()
        return True

    if state.get("arabic_source"):
        # mid-edit: «أبي أعدّل» cleared the draft, so this free text arrives
        # here — it must STILL be understood, or an editing note gets rendered
        # as if it were the achievement (the 29-July incident).
        from career.onboarding import intent as intent_mod

        resolved, topic = _resolve_enrichment_intent(
            session, deps, tenant_id=tenant_id, body=body, state=state,
            pending=None, known_name=name,
        )
        if resolved == intent_mod.OFF_TOPIC and _serve_off_topic(
            session, deps, channel, topic=topic, body=body, now=now, send=_send,
        ):
            session.flush()
            return True
        if resolved == intent_mod.QUESTION:
            _send_buttons(enr._ANSWER_MY_QUESTION, enr.RETRY_BUTTONS)
            journey.context = context
            session.flush()
            return True
        if resolved == intent_mod.REVISE:
            _regenerate(session, deps, channel, context, role_id,
                        enr.classify_edit_intent(body), name, now,
                        _send, _send_buttons)
            journey.context = context
            session.flush()
            return True
        if resolved == intent_mod.AFFIRM:
            draft_id = state.get("draft_fact_id")
            draft = (
                enr.draft_of(session, tenant_id=tenant_id,
                             fact_id=uuid.UUID(str(draft_id)))
                if draft_id else None
            )
            if draft is not None:
                context["enrichment"] = {**state, "pending_fact_id": draft_id}
                _send_buttons(enr.confirm_again_prompt(*draft),
                              enr.CONFIRM_BUTTONS)
            else:
                _send_buttons(enr._NEED_A_BIT_MORE, enr.RETRY_BUTTONS)
            journey.context = context
            session.flush()
            return True

    _handle_enrichment_text(session, deps, channel, context, role_id, body,
                            name, now, _send, _send_buttons)
    journey.context = context
    session.flush()
    return True


def _resolve_enrichment_intent(
    session: Session, deps: Deps, *, tenant_id: uuid.UUID, body: str,
    state: dict[str, Any], pending: Any, known_name: str | None = None,
) -> tuple[str, str]:
    """What does this reply want? (§15) Deterministic keywords stay the fast
    path and the fallback; the model handles the wide middle."""
    from career.onboarding import enrichment as enr
    from career.onboarding import intent as intent_mod

    deterministic = {
        enr.INTENT_AFFIRM: intent_mod.AFFIRM,
        enr.INTENT_INSTRUCTION: intent_mod.REVISE,
        enr.INTENT_NEW_ANSWER: intent_mod.ANSWER,
    }[enr.classify_reply(body, has_previous_answer=bool(state.get("arabic_source")))]

    draft_text: str | None = None
    fact_id = pending or state.get("draft_fact_id")
    if fact_id:
        found = enr.draft_of(session, tenant_id=tenant_id,
                             fact_id=uuid.UUID(str(fact_id)))
        draft_text = found[0] if found else None
    return intent_mod.resolve_intent(
        body, draft=draft_text, classifier=deps.intent_classifier,
        deterministic=deterministic, known_name=known_name,
    )


def _serve_off_topic(
    session: Session, deps: Deps, channel: CustomerChannel, *, topic: str,
    body: str, now: datetime, send: Any,
) -> bool:
    """Route a request that has nothing to do with the draft (§15). True when
    it was served — the enrichment cursor is left untouched, so the customer
    resumes exactly where they were."""
    from career.onboarding import intent as intent_mod

    command = intent_mod.command_for_topic(topic)
    if command is not None and handle_standing_command(
        session, channel_id=channel.id, text=command, deps=deps, now=now
    ):
        send(_ENRICH_RESUME_HINT)
        return True
    if topic in ("human", "billing"):
        # «دعم» is the one escalation path the whole product already trusts
        send(_ENRICH_SUPPORT_HINT)
        return True
    return False


def _dispatch_enrichment_result(
    session: Session, deps: Deps, channel: CustomerChannel,
    context: dict[str, Any], result: dict[str, Any], send: Any,
    send_buttons: Any, now: datetime,
) -> None:
    """ONE place maps a handle_answer result to a reply, so no status can ever
    fall through to silence (§14-ب). Every branch offers ≥2 forward paths."""
    from career.onboarding import enrichment as enr

    state = context.get("enrichment") or {}
    source = result.get("arabic_source") or state.get("arabic_source") or ""
    # persisted for EVERY status — that is what makes «كلامك محفوظ عندي» honest
    state = {**state, "arabic_source": source}

    if result.get("status") == "confirm":
        context["enrichment"] = {
            **state,
            "pending_fact_id": result["pending_fact_id"],
            "draft_fact_id": result["pending_fact_id"],
        }
        prompt = (
            enr.regenerated_prompt(result["english_bullet"],
                                   result["arabic_gloss"])
            if result.get("regenerated") else result["prompt"]
        )
        send_buttons(prompt, enr.CONFIRM_BUTTONS)
        return

    context["enrichment"] = state
    if result.get("status") == "soft_fail":
        send_buttons(enr._TRY_AGAIN_SOON, enr.RETRY_BUTTONS)
        return
    # no_bullet — and the defensive default for any future status
    send_buttons(
        enr._NEED_A_BIT_MORE,
        enr.RETRY_BUTTONS if source else enr.SKIP_ONLY_BUTTONS,
    )


def _regenerate(
    session: Session, deps: Deps, channel: CustomerChannel,
    context: dict[str, Any], role_id: uuid.UUID | None, edit_intent: str,
    name: str | None, now: datetime, send: Any, send_buttons: Any,
) -> None:
    """Re-run the panel over the STORED original answer with the customer's
    direction applied — never over their editing note (§14-ب)."""
    from career.onboarding import enrichment as enr

    state = context.get("enrichment") or {}
    source = state.get("arabic_source") or ""
    draft_id = state.get("draft_fact_id")
    if not source:
        send_buttons(enr._NEED_A_BIT_MORE, enr.SKIP_ONLY_BUTTONS)
        return
    if int(state.get("attempts", 0)) >= enr.MAX_PANEL_ATTEMPTS:
        # §14-ج: stop spending model calls; offer the best draft we have
        draft = (
            enr.draft_of(session, tenant_id=channel.tenant_id,
                         fact_id=uuid.UUID(str(draft_id)))
            if draft_id else None
        )
        if draft is not None:
            context["enrichment"] = {**state, "pending_fact_id": draft_id}
            send_buttons(enr.final_offer_prompt(*draft), enr.FINAL_BUTTONS)
        else:
            if role_id is not None:
                enr.skip_role(session, tenant_id=channel.tenant_id,
                              role_fact_id=role_id, now=now)
            enr.close_session(context)
            send(enr._LETS_MOVE_ON)
        return
    if role_id is None:
        send_buttons(enr._NEED_A_BIT_MORE, enr.RETRY_BUTTONS)
        return
    result = enr.handle_answer(
        session, tenant_id=channel.tenant_id, role_fact_id=role_id,
        arabic_answer=source, renderer=deps.achievement_renderer, now=now,
        known_name=name, judge=deps.bullet_judge, edit_intent=edit_intent,
        draft_fact_id=uuid.UUID(str(draft_id)) if draft_id else None,
    )
    context["enrichment"] = {**state, "attempts": int(state.get("attempts", 0)) + 1}
    _dispatch_enrichment_result(session, deps, channel, context, result,
                                send, send_buttons, now)


def _handle_enrichment_text(
    session: Session, deps: Deps, channel: CustomerChannel,
    context: dict[str, Any], role_id: uuid.UUID | None, body: str,
    name: str | None, now: datetime, send: Any, send_buttons: Any,
) -> None:
    from career.onboarding import enrichment as enr

    if role_id is None or not body:
        return
    # «١/٢/٣» adopts the matching icebreaker example as the answer
    state = context.get("enrichment") or {}
    picked = enr.pick_example(state, body)
    if picked is not None:
        body = picked
    draft_id = state.get("draft_fact_id")
    result = enr.handle_answer(
        session, tenant_id=channel.tenant_id, role_fact_id=role_id,
        arabic_answer=body, renderer=deps.achievement_renderer, now=now,
        known_name=name, judge=deps.bullet_judge,
        fallback_source=str(state.get("arabic_source") or ""),
        draft_fact_id=uuid.UUID(str(draft_id)) if draft_id else None,
    )
    context["enrichment"] = {**state, "attempts": int(state.get("attempts", 0)) + 1}
    _dispatch_enrichment_result(session, deps, channel, context, result,
                                send, send_buttons, now)


def _channel(session: Session, channel_id: uuid.UUID) -> CustomerChannel:
    channel = session.get(CustomerChannel, channel_id)
    if channel is None:
        raise JourneyNotFound(f"channel {channel_id}")
    return channel


def _send(
    session: Session, deps: Deps, channel: CustomerChannel, body: str,
    *, buttons: tuple[str, ...] = (), now: datetime,
) -> None:
    if buttons:
        mid = deps.whatsapp_client.send_interactive(channel.phone_e164, body, buttons)
        kind = "interactive"
    else:
        mid = deps.whatsapp_client.send_text(channel.phone_e164, body)
        kind = "text"
    record_out(
        session, tenant_id=channel.tenant_id, channel_id=channel.id,
        kind=kind, wa_message_id=mid, now=now,
    )


def _touch(journey: OnboardingSession, now: datetime) -> None:
    journey.last_interaction_at = now


def _advance(journey: OnboardingSession, target: str, now: datetime) -> None:
    fsm.validate_transition(journey.state, target)
    journey.state = target
    journey.state_entered_at = now
    journey.context = {}


# ── journey start (called right after a successful activation) ───────────────


def start_journey(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    subscription_id: uuid.UUID,
    channel_id: uuid.UUID,
    deps: Deps,
    now: datetime,
) -> OnboardingSession:
    """Create (or resume) the journey. Activation just proved the phone, so a
    new journey advances PAID_UNCLAIMED → PHONE_VERIFIED → CONSENT_PENDING and
    the first consent prompt goes out."""
    journey = session.execute(
        select(OnboardingSession).where(OnboardingSession.tenant_id == tenant_id)
    ).scalar_one_or_none()
    channel = _channel(session, channel_id)

    if journey is None:
        journey = OnboardingSession(
            id=uuid.uuid4(), tenant_id=tenant_id, subscription_id=subscription_id,
            channel_id=channel_id, state="PAID_UNCLAIMED",
            state_entered_at=now, last_interaction_at=now, context={},
        )
        session.add(journey)
        _advance(journey, "PHONE_VERIFIED", now)
        _advance(journey, "CONSENT_PENDING", now)
        session.flush()
        _prompt_current_step(session, journey, channel, deps, now=now)
    else:
        journey.channel_id = channel_id
        _touch(journey, now)
        _prompt_current_step(session, journey, channel, deps, now=now)
    session.flush()
    return journey


# ── prompting: ask whatever the persisted state needs next ───────────────────


def _consent_state(session: Session, tenant_id: uuid.UUID) -> dict[str, bool]:
    return consents.consent_state(session, tenant_id=tenant_id)


def _missing_required(
    session: Session, tenant_id: uuid.UUID
) -> list[consents.ConsentPurpose]:
    """CHANGELOG §10: the three required purposes are presented as ONE merged
    message; the optional stats purpose is never part of onboarding."""
    state = _consent_state(session, tenant_id)
    return [p for p in consents.PURPOSES if p.required and not state[p.key]]


def _merged_consent_text(missing: list[consents.ConsentPurpose]) -> str:
    rights = consents.RIGHTS_TEXT_AR.format(policy_url="(الرابط في صفحة المنتج)")
    bullets = "\n".join(f"• {p.title_ar}: {p.description_ar}" for p in missing)
    return (
        "قبل ما نبدأ — موافقة واحدة تغطي تشغيل الخدمة:\n"
        f"{bullets}\n\n{rights}"
    )


def _answers_so_far(session: Session, tenant_id: uuid.UUID) -> dict[str, Any]:
    profile = session.execute(
        select(CustomerProfile).where(CustomerProfile.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if profile is None:
        return {}
    answers: dict[str, Any] = {}
    for q in collection.QUESTIONS:
        value = getattr(profile, q.key)
        if value is not None:
            answers[q.key] = value
    # a skipped salary is stored as NULL — mark it answered via context flag
    return answers


def _has_extraction_facts(session: Session, tenant_id: uuid.UUID) -> bool:
    return session.execute(
        select(ProfileFact.id).where(
            ProfileFact.tenant_id == tenant_id,
            ProfileFact.source == "cv_extraction",
        ).limit(1)
    ).scalar_one_or_none() is not None


def _prompt_current_step(
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, *, now: datetime,
) -> None:
    state = journey.state
    tenant_id = journey.tenant_id

    if state == "CONSENT_PENDING":
        missing = _missing_required(session, tenant_id)
        if missing:
            _send(
                session, deps, channel, _merged_consent_text(missing),
                buttons=(_CONSENT_YES, _CONSENT_NO), now=now,
            )
            return
        # consents done → the questions
        answers = _answers_so_far(session, tenant_id)
        for key in (journey.context or {}).get("skipped", []):
            answers.setdefault(key, None)
        question = collection.next_question(answers)
        if question is not None:
            buttons = tuple(o.label_ar for o in question.options)[:3]
            total = len(collection.QUESTIONS)
            position = total - sum(
                1 for q in collection.QUESTIONS[
                    [q.key for q in collection.QUESTIONS].index(question.key):
                ] if q.key not in answers
            ) + 1
            prompt = f"📍 {position} من {total}\n{question.prompt_ar}"
            hidden = [o.label_ar for o in question.options[3:]
                      if o.value is not None]
            if hidden:
                # WhatsApp caps reply buttons at 3 — the rest stay REACHABLE
                # as typed answers (live lesson: they were silently unreachable)
                prompt += "\n(أو اكتب: " + "، ".join(hidden) + ")"
            _send(session, deps, channel, prompt, buttons=buttons, now=now)
            return
        _advance(journey, "CV_UPLOAD_PENDING", now)
        if _has_extraction_facts(session, tenant_id):
            # §04 inheritance: the funnel already read their CV — pass through
            # the upload leg step-by-step (no FSM jump) into confirmation.
            _advance(journey, "CV_PROCESSING", now)
            _advance(journey, "PROFILE_CONFIRMATION", now)
            _send(
                session, deps, channel,
                "قرأنا سيرتك سابقًا في تحليل الـCV — نراجع الحقائق معك الآن ✅",
                now=now,
            )
            _prompt_current_step(session, journey, channel, deps, now=now)
            return
        _send(session, deps, channel, _UPLOAD_PROMPT, now=now)
        return

    if state == "CV_UPLOAD_PENDING":
        _send(session, deps, channel, _UPLOAD_PROMPT, now=now)
        return

    if state == "PROFILE_CONFIRMATION":
        facts = confirmation.facts_awaiting(session, tenant_id=tenant_id)
        if facts:
            # CHANGELOG §10: ONE numbered summary + confirm-all — the live
            # canary measured the fact-by-fact flow at ~9 minutes.
            journey.context = {
                **(journey.context or {}),
                "batch_ids": [str(f.id) for f in facts],
            }
            _send(
                session, deps, channel,
                confirmation.render_batch_summary(facts),
                buttons=(_BATCH_CONFIRM_ALL, _BATCH_FIX_ITEM), now=now,
            )
            return
        _after_confirmation(session, journey, channel, deps, now=now)
        return

    if state == "CAREER_PATH_REVIEW":
        _present_assessment(session, journey, channel, deps, now=now)
        return

    if state == "SEARCH_POLICY_REVIEW":
        _present_policy(session, journey, channel, deps, now=now)
        return


# ── inbound text ─────────────────────────────────────────────────────────────


def handle_text(
    session: Session, *, channel_id: uuid.UUID, text: str, deps: Deps, now: datetime,
) -> None:
    channel = _channel(session, channel_id)
    tenant_id = channel.tenant_id
    journey = get_journey(session, tenant_id=tenant_id)
    _touch(journey, now)
    body = text.strip()

    # Standing commands work at every state (§05/§12).
    command = _PRIVACY_COMMANDS.get(body)
    if command is not None:
        _handle_privacy_command(session, journey.tenant_id, channel, deps,
                                command, now=now)
        session.flush()
        return

    context = dict(journey.context or {})

    if journey.state == "CONSENT_PENDING":
        _handle_consent_or_question(session, journey, channel, deps, body, now=now)
    elif journey.state == "CV_UPLOAD_PENDING":
        _send(session, deps, channel, _UPLOAD_PROMPT, now=now)
    elif journey.state == "PROFILE_CONFIRMATION":
        _handle_verdict(session, journey, channel, deps, body, context, now=now)
    elif journey.state == "CAREER_PATH_REVIEW":
        _handle_path_choice(session, journey, channel, deps, body, now=now)
    elif journey.state == "SEARCH_POLICY_REVIEW":
        _handle_policy_choice(session, journey, channel, deps, body, now=now)
    else:
        # ACTIVE or transient: nothing conversational to do here.
        return
    session.flush()


def _handle_consent_or_question(
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, body: str, *, now: datetime,
) -> None:
    tenant_id = journey.tenant_id
    missing = _missing_required(session, tenant_id)

    if missing:
        if body == _CONSENT_YES:
            # one tap → every required purpose granted, each its own event
            # (purpose separation lives in the ledger — CHANGELOG §10)
            for purpose in missing:
                consents.record_consent(
                    session, tenant_id=tenant_id, purpose=purpose.key,
                    action="granted",
                )
            _send(session, deps, channel, _ROADMAP, now=now)
        elif body == _CONSENT_NO:
            _send(session, deps, channel, _CONSENT_REQUIRED_EXPLAIN, now=now)
            _prompt_current_step(session, journey, channel, deps, now=now)
            return
        else:
            _prompt_current_step(session, journey, channel, deps, now=now)
            return
        _prompt_current_step(session, journey, channel, deps, now=now)
        return

    # questions phase
    answers = _answers_so_far(session, tenant_id)
    for key in (journey.context or {}).get("skipped", []):
        answers.setdefault(key, None)
    question = collection.next_question(answers)
    if question is None:  # shouldn't happen — prompt advances the state
        _prompt_current_step(session, journey, channel, deps, now=now)
        return
    if body.strip(" ؟?!.").lower() in _GREETINGS:
        # a greeting/resume nudge is never an answer — re-ask where we left off
        _prompt_current_step(session, journey, channel, deps, now=now)
        return
    raw = _resolve_question_input(question, body)
    try:
        value = collection.parse_answer(question.key, raw)
    except collection.AnswerInvalid as invalid:
        _send(session, deps, channel, invalid.reprompt_ar, now=now)
        return
    if value is None and question.skippable:
        journey.context = {
            **(journey.context or {}),
            "skipped": [*(journey.context or {}).get("skipped", []), question.key],
        }
    else:
        collection.apply_answer(session, tenant_id=tenant_id, key=question.key, value=value)
    _prompt_current_step(session, journey, channel, deps, now=now)


def _resolve_question_input(question: collection.Question, body: str) -> str:
    """Accept the option id (payload ids), the Arabic label (real taps), or
    the label truncated to WhatsApp's 20-char reply cap — live lesson from
    TEN-0002: «الدمام / الخبر / الظهران» arrived as its first 20 chars and
    was stored as garbage free text."""
    stripped = body.strip()
    for option in question.options:
        if stripped in (option.id, option.label_ar, option.label_ar[:20]):
            return option.id
    if question.skippable and body in ("تخطي", "تخطّي", collection.SKIP):
        return collection.SKIP
    return body


# ── inbound document (the CV) ────────────────────────────────────────────────


def handle_document(
    session: Session, *, channel_id: uuid.UUID, media_id: str,
    filename: str | None, deps: Deps, now: datetime,
) -> None:
    channel = _channel(session, channel_id)
    journey = get_journey(session, tenant_id=channel.tenant_id)
    _touch(journey, now)

    if journey.state != "CV_UPLOAD_PENDING":
        _prompt_current_step(session, journey, channel, deps, now=now)
        session.flush()
        return

    _send(session, deps, channel, _READING_CV, now=now)
    media = deps.whatsapp_client.download_media(media_id)
    if media is None:
        _send(session, deps, channel, _UPLOAD_REJECTED, now=now)
        session.flush()
        return
    data, media_filename = media

    _advance(journey, "CV_PROCESSING", now)
    try:
        row = process_cv_upload(
            session, tenant_id=channel.tenant_id, data=data,
            original_filename=filename or media_filename,
            scanner=deps.scanner, storage=deps.storage,
            limits=deps.upload_limits,
        )
    except ConsentMissing:
        # defense in depth — consents were checked on entry to the state
        journey.state = "CONSENT_PENDING"
        journey.state_entered_at = now
        _prompt_current_step(session, journey, channel, deps, now=now)
        session.flush()
        return

    if row.status != "processed" or not row.extracted_text_storage_key:
        journey.state = fsm.regress_on_failure("CV_PROCESSING")
        journey.state_entered_at = now
        _send(session, deps, channel, _UPLOAD_REJECTED, now=now)
        session.flush()
        return

    cv_text = deps.storage.get(row.extracted_text_storage_key).decode("utf-8")
    profile = session.execute(
        select(CustomerProfile).where(CustomerProfile.tenant_id == channel.tenant_id)
    ).scalar_one_or_none()
    known_name = profile.cv_full_name if profile is not None else None
    try:
        run_extraction(
            session, tenant_id=channel.tenant_id, cv_text=cv_text,
            known_name=known_name, extractor=deps.extractor,
        )
    except ExtractionFailed:
        logger.warning("cv extraction failed", exc_info=True)
        journey.state = fsm.regress_on_failure("CV_PROCESSING")
        journey.state_entered_at = now
        _send(session, deps, channel, _EXTRACTION_FAILED, now=now)
        session.flush()
        return

    _advance(journey, "PROFILE_CONFIRMATION", now)
    _prompt_current_step(session, journey, channel, deps, now=now)
    session.flush()


# ── fact confirmation ────────────────────────────────────────────────────────


def _handle_verdict(
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, body: str, context: dict[str, Any], *, now: datetime,
) -> None:
    tenant_id = journey.tenant_id

    if context.get("awaiting_gap"):
        # the gap answer arrives while still in PROFILE_CONFIRMATION
        gap = context["awaiting_gap"]
        payload = (
            {"title": body, "employer": None} if gap == "experience" else {"name": body}
        )
        confirmation.add_conversation_fact(
            session, tenant_id=tenant_id, category=gap, payload=payload
        )
        journey.context = {k: v for k, v in context.items() if k != "awaiting_gap"}
        _after_confirmation(session, journey, channel, deps, now=now)
        return

    if context.get("awaiting_item_fix"):
        # expected shape: «<رقم البند> <التصحيح>» in one message
        match = re.match(r"\s*(\d+)[\s.\-:]+(.+)", body, re.DOTALL)
        batch_ids = context.get("batch_ids") or []
        if match and 1 <= int(match.group(1)) <= len(batch_ids):
            index = int(match.group(1))
            fact_id = uuid.UUID(batch_ids[index - 1])
            pending = session.get(ProfileFact, fact_id)
            if pending is not None and pending.status == "EXTRACTED":
                confirmation.correct_fact(
                    session, tenant_id=tenant_id, fact_id=fact_id,
                    corrected_payload={
                        **(pending.payload or {}),
                        "customer_correction": match.group(2).strip(),
                    },
                )
            journey.context = {
                k: v for k, v in context.items() if k != "awaiting_item_fix"
            }
            _send(session, deps, channel, "تم التعديل ✅", now=now)
            _prompt_current_step(session, journey, channel, deps, now=now)
            return
        _send(session, deps, channel, _BATCH_FIX_PROMPT, now=now)
        return

    if body == _BATCH_CONFIRM_ALL:
        confirmation.confirm_all(session, tenant_id=tenant_id)
        _after_confirmation(session, journey, channel, deps, now=now)
        return
    if body == _BATCH_FIX_ITEM:
        journey.context = {**context, "awaiting_item_fix": True}
        _send(session, deps, channel, _BATCH_FIX_PROMPT, now=now)
        return
    _prompt_current_step(session, journey, channel, deps, now=now)


def _after_confirmation(
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, *, now: datetime,
) -> None:
    """Confirmation finished — ask only about genuine gaps, then move on."""
    tenant_id = journey.tenant_id
    gaps = confirmation.pending_gaps(session, tenant_id=tenant_id)
    context = dict(journey.context or {})

    if gaps:
        gap = gaps[0]
        journey.context = {**context, "awaiting_gap": gap}
        _send(
            session, deps, channel,
            _GAP_EXPERIENCE if gap == "experience" else _GAP_SKILL, now=now,
        )
        return
    _advance(journey, "CAREER_PATH_REVIEW", now)
    _present_assessment(session, journey, channel, deps, now=now)


# ── path review ──────────────────────────────────────────────────────────────


def _present_assessment(
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, *, now: datetime,
) -> None:
    tenant_id = journey.tenant_id
    profile = session.execute(
        select(CustomerProfile).where(CustomerProfile.tenant_id == tenant_id)
    ).scalar_one_or_none()
    requested = (profile.requested_path if profile else None) or "غير محدد"
    assessment = paths.assess(session, tenant_id=tenant_id, requested_path=requested)
    journey.context = {**(journey.context or {}), "assessment_id": str(assessment.id)}

    requested_info = (assessment.suggested or {}).get("requested", {})
    closest = (assessment.suggested or {}).get("closest", [])
    lines = [
        f"قيّمنا مسارك المطلوب «{requested}»: درجة الملاءمة {assessment.fit_score}/100.",
    ]
    strengths = requested_info.get("strengths") or []
    gaps = requested_info.get("gaps") or []
    if strengths:
        lines.append("نقاط قوتك: " + " · ".join(strengths[:2]))
    if gaps:
        lines.append("فجوات: " + " · ".join(gaps[:2]))
    if closest:
        labels = {f.key: f.label_ar for f in paths.DEFAULT_FAMILIES}
        lines.append(
            "أقرب المسارات لملفك: "
            + "، ".join(f"{labels.get(c['path'], c['path'])} ({c['score']})" for c in closest)
        )
    if (assessment.fit_score or 0) < paths.WEAK_FIT_THRESHOLD:
        lines.append(
            "بصراحة (§ لا اختراع): الفرص على هذا المسار بتكون أقل والتطابق أضعف، "
            "ولن نخترع خبرات. تقدر تعتمد المقترح، أو تكمل على مسارك بعلمك بذلك "
            "(~80% فرص واقعية و~20% طموحة)."
        )
    _send(session, deps, channel, "\n".join(lines), buttons=_APPROVE_BUTTONS, now=now)


def _handle_path_choice(
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, body: str, *, now: datetime,
) -> None:
    tenant_id = journey.tenant_id
    context = dict(journey.context or {})

    assessment_id_raw = context.get("assessment_id")
    if assessment_id_raw is None:
        _present_assessment(session, journey, channel, deps, now=now)
        return
    assessment_id = uuid.UUID(assessment_id_raw)
    assessment = session.execute(
        select(CareerPathAssessment).where(CareerPathAssessment.id == assessment_id)
    ).scalar_one_or_none()
    if assessment is None:
        _present_assessment(session, journey, channel, deps, now=now)
        return

    closest = [c["path"] for c in (assessment.suggested or {}).get("closest", [])]
    top = closest + [None, None, None]

    if body in _APPROVE_IDS:
        paths.approve(
            session, tenant_id=tenant_id, assessment_id=assessment_id,
            primary=top[0], secondary=top[1], stretch=top[2],
        )
    elif body in _OVERRIDE_IDS:
        requested_key = (assessment.suggested or {}).get("requested", {}).get("path")
        primary = requested_key if requested_key and requested_key != "custom" else top[0]
        needs_override = (assessment.fit_score or 0) < paths.WEAK_FIT_THRESHOLD
        paths.approve(
            session, tenant_id=tenant_id, assessment_id=assessment_id,
            primary=primary, secondary=top[0] if top[0] != primary else top[1],
            customer_override=needs_override,
            stretch_ratio_percent=20 if needs_override else None,
        )
    else:
        _present_assessment(session, journey, channel, deps, now=now)
        return

    _advance(journey, "SEARCH_POLICY_REVIEW", now)
    _present_policy(session, journey, channel, deps, now=now)


# ── policy card and activation ───────────────────────────────────────────────


def _present_policy(
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, *, now: datetime,
) -> None:
    draft = policy.build_draft_policy(session, tenant_id=journey.tenant_id)
    journey.context = {**(journey.context or {}), "policy_id": str(draft.id)}
    card = policy.render_summary_card(draft)
    _send(session, deps, channel, card.text_ar, buttons=(card.confirm_button_ar,), now=now)


def _handle_policy_choice(
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, body: str, *, now: datetime,
) -> None:
    if body not in _POLICY_IDS:
        _present_policy(session, journey, channel, deps, now=now)
        return
    policy_id_raw = (journey.context or {}).get("policy_id")
    if policy_id_raw is None:
        _present_policy(session, journey, channel, deps, now=now)
        return
    policy.confirm_policy(
        session, tenant_id=journey.tenant_id, policy_id=uuid.UUID(policy_id_raw)
    )
    _advance(journey, "READY_FOR_ACTIVATION", now)
    policy.activate(session, tenant_id=journey.tenant_id, now=now)
    _send(session, deps, channel, _DONE, now=now)


# ── standing privacy commands ────────────────────────────────────────────────


def _handle_privacy_command(
    session: Session, tenant_id: uuid.UUID, channel: CustomerChannel,
    deps: Deps, command: str, *, now: datetime,
) -> None:
    if command == "status":
        _send(
            session, deps, channel,
            privacy.subscription_status_summary(session, tenant_id=tenant_id, now=now),
            now=now,
        )
    elif command == "pause":
        privacy.open_request(session, tenant_id=tenant_id, kind="pause", now=now)
        privacy.pause_subscription(session, tenant_id=tenant_id)
        _send(session, deps, channel, _PAUSED, now=now)
    elif command == "resume":
        privacy.resume_subscription(session, tenant_id=tenant_id)
        _send(session, deps, channel, _RESUMED, now=now)
    elif command == "export":
        request = privacy.open_request(session, tenant_id=tenant_id, kind="export", now=now)
        key = privacy.fulfill_export(
            session, tenant_id=tenant_id, request_id=request.id,
            storage=deps.storage, now=now,
        )
        # audit fix: the bundle was written to storage and marked fulfilled
        # but never reached the customer — deliver it as a WhatsApp document
        # (falls back to the ack text if the send fails; still recorded).
        try:
            mid = deps.whatsapp_client.send_document(
                channel.phone_e164, key,
                filename="بياناتي.json", caption=_EXPORT_ACK,
            )
            record_out(session, tenant_id=tenant_id, channel_id=channel.id,
                       kind="document", wa_message_id=mid, now=now)
        except Exception:  # noqa: BLE001 — never lose the request on a send blip
            logger.warning("export document send failed", exc_info=True)
            _send(session, deps, channel, _EXPORT_ACK, now=now)
    elif command == "delete_warn":
        _send(session, deps, channel, _DELETE_WARNING, now=now)
    elif command == "delete_execute":
        request = privacy.open_request(session, tenant_id=tenant_id, kind="delete", now=now)
        report = privacy.execute_deletion(
            session, tenant_id=tenant_id, request_id=request.id, now=now,
            storage=deps.storage,
        )
        for key in report.storage_keys_to_purge:
            try:
                deps.storage.delete(key)
            except FileNotFoundError:  # pragma: no cover — already gone
                pass
        deps.whatsapp_client.send_text(channel.phone_e164, _DELETE_DONE)
        # channel rows are gone — no record_out (its channel was deleted).


# ── the >24h stall reminder runner (owner role, spans tenants) ───────────────


def send_due_reminders(owner_session: Session, *, deps: Deps, now: datetime) -> int:
    """Nudge every stalled journey once per stall (§05). Returns count sent."""
    sent = 0
    journeys = owner_session.execute(select(OnboardingSession)).scalars().all()
    for journey in journeys:
        if not fsm.is_reminder_due(
            state=journey.state,
            last_interaction_at=journey.last_interaction_at,
            last_reminder_at=journey.last_reminder_at,
            now=now,
        ):
            continue
        if journey.channel_id is None:
            continue
        channel = owner_session.get(CustomerChannel, journey.channel_id)
        if channel is None or channel.opt_out_at is not None:
            continue
        # audit fix: a >24h stall means the 24h window is CLOSED by
        # definition — only the APPROVED template can reach the customer.
        mid = deps.whatsapp_client.send_template(
            channel.phone_e164,
            ONBOARDING_REMINDER.name, ONBOARDING_REMINDER.language,
        )
        record_out(
            owner_session, tenant_id=journey.tenant_id, channel_id=channel.id,
            kind="template", wa_message_id=mid,
            template_name=ONBOARDING_REMINDER.name, now=now,
        )
        journey.last_reminder_at = now
        sent += 1
    owner_session.flush()
    return sent
