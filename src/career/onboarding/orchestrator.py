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

logger = logging.getLogger("career.onboarding")


@dataclass
class Deps:
    """Everything the orchestrator talks to — injectable, no globals."""

    whatsapp_client: WhatsAppClient
    scanner: MalwareScanner
    storage: StorageAdapter
    extractor: ExtractorClient
    upload_limits: Limits | None = None


class JourneyNotFound(Exception):
    """No onboarding journey exists for this tenant/channel."""


# ── Arabic copy ──────────────────────────────────────────────────────────────

_CONSENT_YES = "أوافق"
_CONSENT_NO = "لا أوافق"
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
    "تم التفعيل ✅ انطلقنا!\n"
    "من الليلة نبدأ نبحث لك، وأول ما نجهّز فرصك بنرسلها هنا. "
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
_EXPORT_ACK = "استلمنا طلب تصدير بياناتك وسنرسلها خلال المهلة المعلنة (7 أيام)."
_NUDGE_REMINDER = "وقفنا عند خطوة بسيطة — نكمل إعداد خدمتك؟ 👇"

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


def _next_consent(session: Session, tenant_id: uuid.UUID) -> consents.ConsentPurpose | None:
    state = _consent_state(session, tenant_id)
    context_free = [p for p in consents.PURPOSES if not state[p.key]]
    # required first, then the optional one; answered (granted) are skipped —
    # a withdrawn/never-granted optional is re-presented once then recorded.
    return context_free[0] if context_free else None


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
        purpose = _next_consent(session, tenant_id)
        optional_done = (journey.context or {}).get("optional_done")
        if purpose is not None and optional_done and not purpose.required:
            purpose = None
        if purpose is not None:
            rights = consents.RIGHTS_TEXT_AR.format(policy_url="(الرابط في صفحة المنتج)")
            label = "اختياري" if not purpose.required else "مطلوبة للتشغيل"
            _send(
                session, deps, channel,
                f"{purpose.title_ar} ({label}):\n{purpose.description_ar}\n\n{rights}",
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
            _send(session, deps, channel, question.prompt_ar, buttons=buttons, now=now)
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
        fact = confirmation.next_fact_to_confirm(session, tenant_id=tenant_id)
        if fact is not None:
            prompt = confirmation.render_fact_prompt(fact)
            journey.context = {**(journey.context or {}), "current_fact_id": str(fact.id)}
            _send(
                session, deps, channel, prompt.prompt_ar,
                buttons=tuple(o.label_ar for o in prompt.options), now=now,
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
        _handle_privacy_command(session, journey, channel, deps, command, now=now)
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
    purpose = _next_consent(session, tenant_id)
    optional_done = (journey.context or {}).get("optional_done", False)

    if purpose is not None and not (optional_done and not purpose.required):
        if body == _CONSENT_YES:
            consents.record_consent(
                session, tenant_id=tenant_id, purpose=purpose.key, action="granted"
            )
        elif body == _CONSENT_NO:
            if purpose.required:
                _send(session, deps, channel, _CONSENT_REQUIRED_EXPLAIN, now=now)
                _prompt_current_step(session, journey, channel, deps, now=now)
                return
            # optional refusal: remember and move on — never re-nag (§12).
            journey.context = {**(journey.context or {}), "optional_done": True}
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
    """Accept the option id (tests/payload ids) or the Arabic label (real taps)."""
    for option in question.options:
        if body == option.id or body == option.label_ar:
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

    if context.get("awaiting_correction_for"):
        fact_id = uuid.UUID(context["awaiting_correction_for"])
        pending = session.get(ProfileFact, fact_id)
        if pending is not None:
            confirmation.correct_fact(
                session, tenant_id=tenant_id, fact_id=fact_id,
                corrected_payload={**(pending.payload or {}), "customer_correction": body},
            )
        journey.context = {k: v for k, v in context.items() if k != "awaiting_correction_for"}
        _prompt_current_step(session, journey, channel, deps, now=now)
        return

    verdict = _VERDICT_IDS.get(body)
    fact_id_raw = context.get("current_fact_id")
    if verdict is None or fact_id_raw is None:
        _prompt_current_step(session, journey, channel, deps, now=now)
        return
    fact_id = uuid.UUID(fact_id_raw)

    if verdict == "confirm":
        confirmation.confirm_fact(session, tenant_id=tenant_id, fact_id=fact_id)
    elif verdict == "reject":
        confirmation.reject_fact(session, tenant_id=tenant_id, fact_id=fact_id)
    else:  # correct → sub-flow: wait for the typed correction
        journey.context = {**context, "awaiting_correction_for": str(fact_id)}
        _send(session, deps, channel, _CORRECTION_PROMPT, now=now)
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
    session: Session, journey: OnboardingSession, channel: CustomerChannel,
    deps: Deps, command: str, *, now: datetime,
) -> None:
    tenant_id = journey.tenant_id
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
        privacy.fulfill_export(
            session, tenant_id=tenant_id, request_id=request.id,
            storage=deps.storage, now=now,
        )
        _send(session, deps, channel, _EXPORT_ACK, now=now)
    elif command == "delete_warn":
        _send(session, deps, channel, _DELETE_WARNING, now=now)
    elif command == "delete_execute":
        request = privacy.open_request(session, tenant_id=tenant_id, kind="delete", now=now)
        report = privacy.execute_deletion(
            session, tenant_id=tenant_id, request_id=request.id, now=now
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
        mid = deps.whatsapp_client.send_text(channel.phone_e164, _NUDGE_REMINDER)
        record_out(
            owner_session, tenant_id=journey.tenant_id, channel_id=channel.id,
            kind="text", wa_message_id=mid, now=now,
        )
        journey.last_reminder_at = now
        sent += 1
    owner_session.flush()
    return sent
