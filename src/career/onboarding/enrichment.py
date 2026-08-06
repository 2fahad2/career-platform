"""Thin-role enrichment flow (F-ENRICH, CHANGELOG §13, D14).

Post-activation, opportunity-triggered, once-ever. A role with fewer than two
confirmed achievements is «thin»; when the nightly worker actually tailors a
CV for a thin role (or the 3-day sweep fires), we enqueue ONE friendly Saudi-
colloquial nudge for that role. The customer's colloquial answer becomes a
grounded English bullet (achievement_render), is shown back for confirmation
(English + Arabic gloss), and only then joins the bank as CUSTOMER_CONFIRMED.

Never blocks delivery. Never adds a turn to onboarding. Never re-asks a role.
The FSM is untouched — this is an out-of-band branch beside the terminal
ACTIVE state, gated on ``journey.context["enrichment"]``.

All customer-facing copy is Saudi colloquial, approved by the operator
(deviation D14 message set), and each Arabic line is kept direction-pure
(the English bullet lands on its own line) per the bidi discipline.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.arabic import normalize_ar
from career.cv.close import LlmMeter
from career.db.models import (
    ProfileFact,
    RoleEnrichment,
)
from career.onboarding.achievement_render import (
    AchievementRenderer,
    classify_edit_intent,
)

#: re-exported for the orchestrator (enr.classify_edit_intent) — the closed
#: intent set lives with the renderer, the conversation reads it from here.
__all__ = ["classify_edit_intent", "classify_reply", "handle_answer", "matches"]
from career.onboarding.bullet_panel import PANEL_ANGLES, run_panel
from career.onboarding.confirmation import BANK_STATUSES

_logger = logging.getLogger("career.enrichment")

THIN_ACHIEVEMENT_THRESHOLD = 2
ENRICH_SESSION_TTL = timedelta(hours=72)
SWEEP_AFTER_DAYS = 3

# ── the approved Saudi-colloquial copy (D14) ─────────────────────────────────


def _ask(role_title: str) -> str:
    return (
        f"تمام 👌 شفت خبرتك كـ{role_title} وصراحة فيها شغل حلو.\n"
        "خلّني أطلّعها لك بأحلى صورة قدام صاحب العمل — "
        "وش أكثر شي كنت مسؤول عنه أو تفتخر إنك سويته؟\n"
        "ولو فيه رقم يوصف شغلك (كم مشروع، كم تحسّنت النتيجة) يزيدها قوة، "
        "ولو ما فيه عادي ما يخالف 🙂"
    )


_SKIP_ROLE = "تخطّي هذا الدور"
_NOTHING = "ما عندي إضافة"
_STOP_ALL = "كفى خلصنا"
_CONFIRM_OK = "مضبوط ✅"
_CONFIRM_EDIT = "أبي أعدّل ✏️"
_CONFIRM_DEL = "احذفها ❌"
_AGAIN = "صيغة ثانية 🔄"

#: Interactive button ids (≤256 chars) — the tap arrives as the id via
#: _button_id_of; typed labels keep working as a fallback for old clients.
BTN_SKIP = "enr_skip"
BTN_NONE = "enr_none"
BTN_OK = "enr_ok"
BTN_EDIT = "enr_edit"
BTN_DEL = "enr_del"
BTN_AGAIN = "enr_again"

#: (id, title) pairs for send_interactive — titles ≤20 chars (Graph limit).
OPENING_BUTTONS: tuple[tuple[str, str], ...] = (
    (BTN_SKIP, _SKIP_ROLE),
    (BTN_NONE, _NOTHING),
)
CONFIRM_BUTTONS: tuple[tuple[str, str], ...] = (
    (BTN_OK, _CONFIRM_OK),
    (BTN_EDIT, _CONFIRM_EDIT),
    (BTN_DEL, _CONFIRM_DEL),
)
#: Recovery keyboards — every non-confirm message carries ≥2 forward paths.
RETRY_BUTTONS: tuple[tuple[str, str], ...] = (
    (BTN_AGAIN, _AGAIN), (BTN_SKIP, _SKIP_ROLE),
)
FINAL_BUTTONS: tuple[tuple[str, str], ...] = (
    (BTN_OK, _CONFIRM_OK), (BTN_SKIP, _SKIP_ROLE),
)
SKIP_ONLY_BUTTONS: tuple[tuple[str, str], ...] = ((BTN_SKIP, _SKIP_ROLE),)

#: §14-ج: no model call past this many panel turns for one role.
MAX_PANEL_ATTEMPTS = 4

# ── copy that never dead-ends (§14-ب) ───────────────────────────────────────
# Rules these must all keep: no «ما قدرت / ما فهمت / تعذّر / فشل», never blame
# the customer, and always ≥2 concrete forward paths. A static test enforces
# it so a future edit cannot quietly reintroduce a dead end.

_EDIT_PROMPT = (                       # the ROOT CAUSE of the 29-July incident:
    "تمام 👌 قل لي وش تبي أعدّل وأنا أعيد صياغتها\n"
    "مثال: خلّها أقوى · اختصرها · بسّطها · صيغة ثانية\n"
    "ولو تبي تكتبها بكلماتك من جديد، اكتبها وأنا أصيغها لك"
)

_NEED_A_BIT_MORE = (                   # replaces the dead end
    "وصلني كلامك وأنا معك 🙏\n"
    "عشان أطلّعها بأقوى صورة، عطني تفصيلة صغيرة: وش كان دورك بالضبط، "
    "ووش تغيّر بعد شغلك؟\n"
    "أو قل لي بس «خلّها أقوى» وأنا أعيد الصياغة\n"
    "وإن كان الوقت ما يناسبك، اضغط «تخطّي هذا الدور» ونكمل ولا يهمك 👌"
)

_TRY_AGAIN_SOON = (                    # model boundary down, or a PII refusal
    "كلامك وصلني ومحفوظ عندي 🙏\n"
    "الصياغة تأخّرت شوي من عندي — اضغط «صيغة ثانية» بعد دقيقة وأجهّزها لك\n"
    "أو اضغط «تخطّي هذا الدور» ونكمل ولا يهمك 👌"
)

_DELETED_ASK = (
    "تمام، شلتها ✅\n"
    "نجرّب صيغة ثانية من نفس كلامك، ولا نعدّي هالدور؟"
)

_DISPUTED_ASK = (                      # §15: they said the draft is wrong
    "عذرًا 🙏 وشكرًا إنك صححتني — شلت السطر.\n"
    "قل لي بكلماتك وش الصح وأنا أصيغه لك من جديد\n"
    "أو اضغط «تخطّي هذا الدور» ونكمل ولا يهمك 👌"
)

_ANSWER_MY_QUESTION = (                # §15: they asked us something
    "سؤالك وصلني 🙏\n"
    "باختصار: نأخذ كلامك أنت ونصيغه سطرًا إنجليزيًا لسيرتك، وما نضيف "
    "ولا معلومة من عندنا — وما يدخل سيرتك إلا بضغطة منك\n"
    "ولو تبي أحد من الفريق يشرح لك أكثر، أرسل كلمة «دعم»\n"
    "ونكمل متى ما جهزت 👌"
)

_LETS_MOVE_ON = (                      # attempts exhausted with no draft
    "ما عليك، نكمل الحين وسيرتك زينة بدون هالسطر 👌\n"
    "وبنرجع لك بفرصك المختارة كل صباح مثل ما اتفقنا 🌟"
)


def regenerated_prompt(english: str, arabic_gloss: str) -> str:
    return (
        "سمعتك 👌 هذي صيغة جديدة:\n"
        f"{english}\n"
        f"ومعناه بالعربي: {arabic_gloss}\n"
        "كذا أحسن؟"
    )


def confirm_again_prompt(english: str, arabic_gloss: str) -> str:
    """A typed «تمام» is warmth, not consent — re-show and ask for the tap
    instead of promoting (constant 5, §14-ج)."""
    return (
        "أبشر 🙏 بس أحتاج ضغطة منك عشان أضيفها لسيرتك\n"
        f"{english}\n"
        f"ومعناه بالعربي: {arabic_gloss}\n"
        "اضغط «مضبوط ✅» وتدخل سيرتك، أو «أبي أعدّل ✏️» وأعيد الصياغة"
    )


def final_offer_prompt(english: str, arabic_gloss: str) -> str:
    """The attempt cap: stop regenerating, offer the best draft we have."""
    return (
        "جرّبنا كذا صيغة، وهذي أقربها لكلامك 👇\n"
        f"{english}\n"
        f"ومعناه بالعربي: {arabic_gloss}\n"
        "لو تناسبك اضغط «مضبوط ✅» وتدخل سيرتك\n"
        "ولو ما ناسبتك اضغط «تخطّي هذا الدور» ونكمل ولا يهمك 👌"
    )

#: The name sits on ITS OWN LINE — most customers' names are Latin in our
#: records (they come off the CV header), and a mixed line scrambles in the
#: customer's client. And with no name at all the old text read «تسلم يا 🙏»,
#: a broken sentence delivered at the one moment we are thanking them; the
#: nameless form is now a complete sentence in its own right.
_ACK_THANKS_NAMED = "تسلم يا\n[name]\n🙏 هالمعلومة فرقت مرة وبتقوّي سيرتك فعلاً."
_ACK_THANKS_PLAIN = "تسلم 🙏 هالمعلومة فرقت مرة وبتقوّي سيرتك فعلاً."
_ACK_SKIP = "تمام، عدّينا هالجزء وسيرتك زينة 👍"
_ACK_DONE = "خلّصنا، مشكور على وقتك 🌟"


def _confirm_prompt(english: str, arabic_gloss: str) -> str:
    return (
        "جهّزنا لك هذا السطر لسيرتك (بالإنجليزي):\n"
        f"{english}\n"
        f"ومعناه بالعربي: {arabic_gloss}\n"
        "كذا مضبوط؟"
    )


# ── detection ────────────────────────────────────────────────────────────────


def _split_by_recency(facts: list[ProfileFact]) -> list[ProfileFact]:
    """Current roles (no/placeholder end date) first, then ended roles by
    LATEST end date first. AUDIT ك-5: the previous single-key ascending sort
    picked the OLDEST-ended role before recent ones — and burned the
    once-ever ledger on the wrong role."""
    current: list[ProfileFact] = []
    ended: list[ProfileFact] = []
    for fact in facts:
        end = str((fact.payload or {}).get("end_date") or "").strip()
        if not end or end.lower() in ("present", "current", "الحالي", "حاليا"):
            current.append(fact)
        else:
            ended.append(fact)
    ended.sort(key=lambda f: str((f.payload or {}).get("end_date") or ""),
               reverse=True)
    return current + ended


def thin_roles(session: Session, *, tenant_id: uuid.UUID) -> list[ProfileFact]:
    """The confirmed experience facts with < THRESHOLD achievements (counting
    both inline achievements and confirmed conversation-achievement facts),
    limited to the two most-recent — never asks about older roles."""
    exp = list(session.execute(
        select(ProfileFact).where(
            ProfileFact.tenant_id == tenant_id,
            ProfileFact.category == "experience",
            ProfileFact.status.in_(sorted(BANK_STATUSES)),
        )
    ).scalars().all())
    # count enrichment achievements already linked per role
    linked: dict[str, int] = {}
    for a in session.execute(
        select(ProfileFact).where(
            ProfileFact.tenant_id == tenant_id,
            ProfileFact.category == "achievement",
            ProfileFact.status.in_(sorted(BANK_STATUSES)),
        )
    ).scalars():
        rid = str((a.payload or {}).get("experience_fact_id") or "")
        if rid:
            linked[rid] = linked.get(rid, 0) + 1

    def count(fact: ProfileFact) -> int:
        inline = len((fact.payload or {}).get("achievements") or [])
        return inline + linked.get(str(fact.id), 0)

    recent = _split_by_recency(exp)[:2]
    return [f for f in recent if count(f) < THIN_ACHIEVEMENT_THRESHOLD]


def _already_handled(session: Session, *, tenant_id: uuid.UUID, fact_id: uuid.UUID) -> bool:
    return session.execute(
        select(RoleEnrichment.id).where(
            RoleEnrichment.tenant_id == tenant_id,
            RoleEnrichment.fact_id == fact_id,
        )
    ).first() is not None


# ── enqueue (called by the nightly worker after a thin role is tailored) ─────


def enqueue_enrichment(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    role_fact_id: uuid.UUID,
    journey_context: dict[str, Any],
    trigger: str,
    now: datetime,
) -> bool:
    """Mark the role ASKED (once-ever) and open the enrichment cursor. Returns
    True when a new nudge was armed; False if already handled or one is open."""
    if _already_handled(session, tenant_id=tenant_id, fact_id=role_fact_id):
        return False
    enr = journey_context.get("enrichment") or {}
    if enr.get("open"):
        return False  # one session at a time — anti-nag
    session.add(RoleEnrichment(
        id=uuid.uuid4(), tenant_id=tenant_id, fact_id=role_fact_id,
        status="ASKED", trigger=trigger, asked_at=now,
    ))
    # one role per session by design (anti-nag) — no queue scaffolding
    journey_context["enrichment"] = {
        "open": True, "current": str(role_fact_id),
        "opened_at": now.isoformat(), "attempts": 0,
    }
    session.flush()
    return True


def opening_message(session: Session, *, role_fact_id: uuid.UUID) -> str:
    role = session.get(ProfileFact, role_fact_id)
    title = str((role.payload or {}).get("title") or "خبرتك") if role else "خبرتك"
    return _ask(title)


SKIP_LABELS = frozenset({_SKIP_ROLE, _NOTHING, _STOP_ALL, BTN_SKIP,
                         BTN_NONE, "نعدّي", "نعدي", "نكمّل", "نكمل",
                         "نعدي هالدور", "عدها", "تخطى", "تخطي"})
OK_LABELS = frozenset({_CONFIRM_OK, BTN_OK, "مضبوط"})
EDIT_LABELS = frozenset({_CONFIRM_EDIT, BTN_EDIT})
DEL_LABELS = frozenset({_CONFIRM_DEL, BTN_DEL})
AGAIN_LABELS = frozenset({_AGAIN, BTN_AGAIN, "صيغة ثانية", "صيغه ثانيه",
                          "ثانية", "غيرها"})

#: Normalised once at import — «مضبوط» typed must equal «مضبوط ✅» tapped.
_NORMALISED: dict[str, frozenset[str]] = {}


def matches(body: str, labels: frozenset[str]) -> bool:
    """Button ids and typed Arabic labels both resolve, diacritic- and
    emoji-insensitively (§14-ج)."""
    key = id(labels)
    cache = _NORMALISED.get(str(key))
    if cache is None:
        cache = frozenset(normalize_ar(x) for x in labels)
        _NORMALISED[str(key)] = cache
    return normalize_ar(body) in cache


# ── which of three things a free-text reply is, while a draft is on screen ──
# Deterministic by design (no fifth network call): the classifier's failure
# mode is self-healing — an instruction misread as an answer comes back as
# «not_achievement» and the ladder in handle_answer retries it as an
# instruction. Positive-evidence-only, so the DANGEROUS direction (a real
# achievement swallowed as an editing note) cannot happen.

INTENT_AFFIRM = "affirm"
INTENT_INSTRUCTION = "instruction"
INTENT_NEW_ANSWER = "new_answer"

_INSTRUCTION_TOKENS = frozenset({
    "ابدع", "بدع", "اختصر", "قصر", "اقصر", "طول", "قوي", "قو", "حسن", "عدل",
    "غير", "صيغ", "صغ", "اعد", "نقح", "بسط", "وضح", "فصل", "كبر", "صغر", "زد",
    "زيد", "ضيف", "اضف", "انقص", "شل", "احذف", "رتب", "خفف", "نمق", "خلها",
    "احترافي", "احترافيه", "اقوي", "احلي", "افضل", "اجمل", "اطول", "اوضح",
    "مختصر", "مختصره", "طويله", "قصيره", "ضعيفه", "باهته", "جافه",
    "عجبتني", "عجبني",
})
_META_TOKENS = frozenset({
    "كلامي", "كلمي", "كلامك", "الصياغه", "صياغه", "صياغتها", "السطر", "سطر",
    "الجمله", "جمله", "النص", "العباره", "الترجمه", "ترجمه", "ترجمت",
    "ترجمتها", "المسوده", "بالانجليزي", "الانجليزي",
})
_WORK_TOKENS = frozenset({
    "كنت", "مسوول", "مسيول", "اشرفت", "ادرت", "دربت", "طورت", "حللت", "نظمت",
    "انشات", "بنيت", "سويت", "عملت", "شغلت", "راقبت", "قللت", "زدت", "رفعت",
    "حسنت", "قدت", "تابعت", "جهزت", "صممت", "كتبت", "درست", "خدمت", "ساعدت",
    "دعمت", "انجزت", "حققت", "اطلقت", "نفذت", "قدمت", "استلمت", "سلمت",
    "تعاملت", "تواصلت", "فريق", "مشروع", "مشاريع", "عميل", "عملاء", "قسم",
    "تقارير",
})
_AFFIRM_TOKENS = frozenset({
    "ايه", "ايوه", "اي", "نعم", "اوك", "اوكي", "تمام", "ماشي", "زين", "زينه",
    "اكيد", "يب", "تم", "yes", "ok",
})


def _hits(tokens: set[str], lexicon: frozenset[str]) -> set[str]:
    """Lexicon hits, tolerating one trailing enclitic pronoun («اختصرها»)."""
    out: set[str] = set()
    for t in tokens:
        forms = {t} | {
            t[: -len(sfx)] for sfx in ("ها", "هم", "ه")
            if t.endswith(sfx) and len(t) - len(sfx) >= 3
        }
        if forms & lexicon:
            out.add(t)
    return out


def classify_reply(text: str, *, has_previous_answer: bool) -> str:
    """AFFIRM / INSTRUCTION / NEW_ANSWER. Default is NEW_ANSWER so a genuine
    achievement is never silently swallowed as an editing note."""
    if not has_previous_answer:
        return INTENT_NEW_ANSWER      # nothing to regenerate from
    tokens = set(normalize_ar(text).split())
    if not tokens:
        return INTENT_NEW_ANSWER
    if tokens <= _AFFIRM_TOKENS:
        return INTENT_AFFIRM
    work = _hits(tokens, _WORK_TOKENS) | {
        t for t in tokens
        if t.endswith("ت") and len(t) >= 4
        and t not in _META_TOKENS and t not in _INSTRUCTION_TOKENS
    }
    if _hits(tokens, _META_TOKENS) or _hits(tokens, _INSTRUCTION_TOKENS):
        # instruction words present — unless the reply also carries real work
        # content, in which case it is a fresh answer that happens to comment
        return INTENT_NEW_ANSWER if len(work) >= 2 else INTENT_INSTRUCTION
    if not work and len(tokens) <= 5:
        return INTENT_INSTRUCTION     # a short reaction, not an achievement
    return INTENT_NEW_ANSWER


# ── answer handling (the render → pending → confirm → bank chain) ────────────


def _bank_vocabulary(session: Session, *, tenant_id: uuid.UUID) -> set[str]:
    from career.cv.generate import bank_vocabulary

    bank: dict[str, list[dict[str, Any]]] = {}
    for row in session.execute(
        select(ProfileFact).where(
            ProfileFact.tenant_id == tenant_id,
            ProfileFact.status.in_(sorted(BANK_STATUSES)),
        )
    ).scalars():
        bank.setdefault(row.category, []).append(dict(row.payload or {}))
    return bank_vocabulary(bank)


def draft_of(
    session: Session, *, tenant_id: uuid.UUID, fact_id: uuid.UUID
) -> tuple[str, str] | None:
    """(english, arabic_gloss) of a still-pending draft, or None."""
    fact = session.get(ProfileFact, fact_id)
    if (fact is None or fact.tenant_id != tenant_id
            or fact.status != "EXTRACTED"):
        return None
    payload = fact.payload or {}
    return str(payload.get("text") or ""), str(payload.get("arabic_gloss") or "")


def _upsert_draft(
    session: Session, *, tenant_id: uuid.UUID, role_fact_id: uuid.UUID,
    draft_fact_id: uuid.UUID | None, rendered: dict[str, Any],
    arabic_answer: str, edit_intent: str,
) -> ProfileFact:
    """Reuse the pending row across regenerations instead of orphaning one per
    attempt. PAYLOAD IS A CLOSED SET: bank_vocabulary() scans every string
    value of confirmed facts, so no model-authored English (the judge's
    reason, panel bookkeeping) may ever be stored here — it would silently
    widen the §10.2 invention whitelist. Those go to the log."""
    payload = {
        "text": rendered["english_bullet"],
        "experience_fact_id": str(role_fact_id),
        "arabic_source": arabic_answer,           # audit only, never rendered
        "arabic_gloss": rendered["arabic_gloss"],  # confirm UI only
        "lang": "en",
    }
    if edit_intent:
        payload["edit_intent"] = edit_intent
    if draft_fact_id is not None:
        existing = session.get(ProfileFact, draft_fact_id)
        if (existing is not None and existing.tenant_id == tenant_id
                and existing.status == "EXTRACTED"):
            existing.payload = payload
            session.flush()
            return existing
    fact = ProfileFact(
        id=uuid.uuid4(), tenant_id=tenant_id, category="achievement",
        payload=payload, status="EXTRACTED",
        source="conversation_achievement",
    )
    session.add(fact)
    session.flush()
    return fact


def handle_answer(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    role_fact_id: uuid.UUID,
    arabic_answer: str,
    renderer: AchievementRenderer,
    now: datetime,
    known_name: str | None = None,
    judge: Any = None,
    edit_intent: str = "",
    fallback_source: str = "",
    draft_fact_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Run the quality panel over the answer and return a DISCRIMINATED result
    the orchestrator can always answer warmly:

    * ``confirm``   — a grounded winner is on the draft row, show the buttons
    * ``no_bullet`` — nothing survived; ``reason`` says why (they differ in UX)
    * ``soft_fail`` — our boundary failed (model down / PII refusal), not them

    Never returns a bare failure the caller could turn into silence."""
    from career.onboarding.extraction import assert_no_pii, strip_pii

    # AUDIT ك-9 + constant §15.8: strip BEFORE any model call, and the
    # customer's name too. A refusal here is OUR problem, never theirs.
    try:
        stripped = strip_pii(arabic_answer, known_name=known_name)
        assert_no_pii(stripped.text, known_name=known_name)
    except Exception:  # noqa: BLE001 — never echo or log the text itself
        _logger.warning("PII gate refused an enrichment answer")
        return {"status": "soft_fail", "arabic_source": ""}

    source = stripped.text
    # §14: every panel below is billed Claude spend for THIS tenant — one
    # meter, threaded down so the pure panel stays free of the database.
    meter = LlmMeter(session, tenant_id=tenant_id, now=now)
    try:
        vocab = _bank_vocabulary(session, tenant_id=tenant_id)
        result = run_panel(
            renderer, arabic_answer=source, vocabulary=vocab,
            instruction=edit_intent, judge=judge, meter=meter,
        )
    except Exception:  # noqa: BLE001 — a dead boundary is a soft failure
        _logger.warning("enrichment panel failed", exc_info=True)
        return {"status": "soft_fail", "arabic_source": source}

    _logger.info("enrichment panel %s", result.get("panel"))
    regenerated = bool(edit_intent)
    status = result.get("status")

    # LADDER — each branch fires at most one extra panel.
    if status == "not_achievement" and fallback_source and fallback_source != source:
        # the reply was an instruction we misread as an answer: retry against
        # the stored original, which is what the customer actually meant.
        try:
            result = run_panel(
                renderer, arabic_answer=fallback_source, vocabulary=vocab,
                instruction=edit_intent or "rephrase", judge=judge, meter=meter,
            )
            _logger.info("enrichment panel (self-heal) %s", result.get("panel"))
            if result.get("status") == "ok":
                source, regenerated, status = fallback_source, True, "ok"
        except Exception:  # noqa: BLE001
            _logger.warning("self-heal panel failed", exc_info=True)
    elif status == "ungrounded":
        # every draft invented something — one cheap plainer attempt, no judge
        try:
            retry = run_panel(
                renderer, arabic_answer=source, vocabulary=vocab,
                instruction="simpler", judge=None, angles=(PANEL_ANGLES[0],),
                meter=meter,
            )
            _logger.info("enrichment panel (plainer) %s", retry.get("panel"))
            if retry.get("status") == "ok":
                result, status = retry, "ok"
        except Exception:  # noqa: BLE001
            _logger.warning("plainer retry failed", exc_info=True)

    if status != "ok":
        return {
            "status": "no_bullet",
            "reason": str(status or "unknown"),
            "arabic_source": source,
        }

    fact = _upsert_draft(
        session, tenant_id=tenant_id, role_fact_id=role_fact_id,
        draft_fact_id=draft_fact_id, rendered=result,
        arabic_answer=source, edit_intent=edit_intent,
    )
    return {
        "status": "confirm",
        "pending_fact_id": str(fact.id),
        "english_bullet": result["english_bullet"],
        "arabic_gloss": result["arabic_gloss"],
        "arabic_source": source,
        "regenerated": regenerated,
        "prompt": _confirm_prompt(
            result["english_bullet"], result["arabic_gloss"]
        ),
    }


def confirm_answer(
    session: Session, *, tenant_id: uuid.UUID, pending_fact_id: uuid.UUID,
    role_fact_id: uuid.UUID, now: datetime,
) -> None:
    """Promote the pending bullet to the bank and close the role (ENRICHED)."""
    from career.onboarding.confirmation import confirm_fact

    confirm_fact(session, tenant_id=tenant_id, fact_id=pending_fact_id)
    _close_role(session, tenant_id=tenant_id, role_fact_id=role_fact_id,
                status="ENRICHED", now=now)


def reject_answer(
    session: Session, *, tenant_id: uuid.UUID, pending_fact_id: uuid.UUID,
) -> None:
    """Customer said «احذفها» — reject + forbid (§15.5), pending role stays
    open for another attempt or a skip."""
    from career.onboarding.confirmation import reject_fact

    reject_fact(session, tenant_id=tenant_id, fact_id=pending_fact_id)


def skip_role(
    session: Session, *, tenant_id: uuid.UUID, role_fact_id: uuid.UUID,
    now: datetime,
) -> None:
    _close_role(session, tenant_id=tenant_id, role_fact_id=role_fact_id,
                status="SKIPPED", now=now)


def _close_role(
    session: Session, *, tenant_id: uuid.UUID, role_fact_id: uuid.UUID,
    status: str, now: datetime,
) -> None:
    row = session.execute(
        select(RoleEnrichment).where(
            RoleEnrichment.tenant_id == tenant_id,
            RoleEnrichment.fact_id == role_fact_id,
        )
    ).scalars().first()
    if row is not None:
        row.status = status
        row.answered_at = now
    session.flush()


def close_session(journey_context: dict[str, Any]) -> None:
    """Clear the enrichment cursor — the loop is done (or stopped)."""
    journey_context["enrichment"] = {"open": False}


def personalize(text: str, name: str | None) -> str:
    return text.replace("[name]", name or "").replace("  ", " ").strip()


def ack_thanks(name: str | None) -> str:
    if not (name or "").strip():
        return _ACK_THANKS_PLAIN
    return personalize(_ACK_THANKS_NAMED, name)



# ── the icebreaker examples menu (كاسر التجمّد) ──────────────────────────────

_PICK_MAP = {"1": 0, "١": 0, "2": 1, "٢": 1, "3": 2, "٣": 2}


def prepare_examples(
    session: Session, *, role_fact_id: uuid.UUID, writer: Any
) -> list[str]:
    """Three scrubbed colloquial examples for the role, or [] (no examples →
    the opening question stands alone; never blocks the nudge).

    §14: «optional garnish» is still billed Claude spend — the role row names
    the tenant, so the call is metered without threading a meter in."""
    from career.cv.close import LlmMeter, metered
    from career.onboarding.achievement_render import scrub_examples

    role = session.get(ProfileFact, role_fact_id)
    if role is None or writer is None:
        return []
    payload = role.payload or {}
    title = str(payload.get("title") or "")
    description = str(payload.get("description") or "")
    meter = LlmMeter(session, tenant_id=role.tenant_id)
    try:
        with metered(meter, "llm_examples", writer):
            raw = list(writer.write(title, description))
    except Exception:  # noqa: BLE001 — examples are optional garnish
        return []
    # the model is asked for three in the prompt; the schema can't enforce a
    # count (live 400 on minItems/maxItems), so trim after the scrub gate.
    return scrub_examples(
        raw, role_payload_text=f"{title} {description}"
    )[:3]


def pick_example(state: dict[str, Any], body: str) -> str | None:
    """«١»/«2»… → the stored example text, else None."""
    idx = _PICK_MAP.get(body.strip())
    examples = state.get("examples") or []
    if idx is None or idx < 0 or idx >= len(examples):
        return None
    return str(examples[idx])


# ── the hourly sweep: 3-day fallback nudge + 72h auto-close ──────────────────


def run_hourly_sweep(
    session: Session,
    *,
    whatsapp_client: Any,
    examples_writer: Any,
    now: datetime,
) -> dict[str, int]:
    """Two housekeeping duties, called from the worker's hourly block:

    AUTO-CLOSE — an enrichment session open longer than ENRICH_SESSION_TTL
    is closed silently (role → SKIPPED if still ASKED); no dangling «what
    were we talking about» ever greets a customer days later.

    SWEEP — a customer ACTIVE ≥ SWEEP_AFTER_DAYS whose most-recent role is
    thin and never surfaced via the lazy trigger gets ONE gentle nudge —
    only when their 24h window is OPEN. once-ever still holds (enqueue
    refuses a role with any ledger row). Never raises into the loop."""
    from career.db.models import CustomerChannel, OnboardingSession
    from career.whatsapp.delivery import record_out
    from career.whatsapp.window import WindowState, window_state

    counts = {"auto_closed": 0, "swept": 0}
    journeys = session.execute(
        select(OnboardingSession).where(OnboardingSession.state == "ACTIVE")
    ).scalars().all()
    for journey in journeys:
        context = dict(journey.context or {})
        state = context.get("enrichment") or {}

        if state.get("open"):
            opened_raw = str(state.get("opened_at") or "")
            try:
                opened_at = datetime.fromisoformat(opened_raw)
            except ValueError:
                opened_at = None
            if opened_at is not None and opened_at <= now - ENRICH_SESSION_TTL:
                current = state.get("current")
                if current:
                    row = session.execute(
                        select(RoleEnrichment).where(
                            RoleEnrichment.tenant_id == journey.tenant_id,
                            RoleEnrichment.fact_id == uuid.UUID(str(current)),
                        )
                    ).scalars().first()
                    if row is not None and row.status == "ASKED":
                        row.status = "SKIPPED"
                        row.answered_at = now
                close_session(context)
                journey.context = context
                counts["auto_closed"] += 1
            continue

        # sweep candidates: old enough, thin, un-asked, window open
        completed = journey.completed_at
        if completed is None or completed > now - timedelta(days=SWEEP_AFTER_DAYS):
            continue
        thin = thin_roles(session, tenant_id=journey.tenant_id)
        if not thin:
            continue
        role = thin[0]
        if _already_handled(session, tenant_id=journey.tenant_id, fact_id=role.id):
            continue
        channel = (
            session.get(CustomerChannel, journey.channel_id)
            if journey.channel_id else None
        )
        if channel is None or window_state(
            last_inbound_at=channel.last_inbound_at,
            opt_out_at=channel.opt_out_at, now=now,
        ) is not WindowState.OPEN:
            continue
        # AUDIT ك-8: one journey's failure must not poison the others — a
        # savepoint isolates this journey's rows, so a send exception rolls
        # back ONLY its ASKED row (no duplicate nudge next hour for customers
        # who already received theirs) and the sweep continues.
        try:
            with session.begin_nested():
                if not enqueue_enrichment(
                    session, tenant_id=journey.tenant_id, role_fact_id=role.id,
                    journey_context=context,
                    trigger="post_activation_sweep", now=now,
                ):
                    continue
                examples = prepare_examples(
                    session, role_fact_id=role.id, writer=examples_writer
                )
                if examples:
                    context["enrichment"]["examples"] = examples
                journey.context = context
                mid = whatsapp_client.send_interactive(
                    channel.phone_e164,
                    opening_message(session, role_fact_id=role.id),
                    OPENING_BUTTONS,
                )
                record_out(session, tenant_id=journey.tenant_id,
                           channel_id=channel.id, kind="interactive",
                           wa_message_id=mid, now=now)
                if examples:
                    from career.onboarding.achievement_render import (
                        format_examples_message,
                    )

                    mid2 = whatsapp_client.send_text(
                        channel.phone_e164, format_examples_message(examples)
                    )
                    record_out(session, tenant_id=journey.tenant_id,
                               channel_id=channel.id, kind="text",
                               wa_message_id=mid2, now=now)
        except Exception:  # noqa: BLE001 — isolate, log, move on
            _logger.warning("enrichment sweep failed for one journey",
                            exc_info=True)
            continue
        counts["swept"] += 1
    session.flush()
    return counts
