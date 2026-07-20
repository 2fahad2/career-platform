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

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import (
    ProfileFact,
    RoleEnrichment,
)
from career.onboarding.achievement_render import (
    AchievementRenderer,
    render_achievement,
)
from career.onboarding.confirmation import BANK_STATUSES

THIN_ACHIEVEMENT_THRESHOLD = 2
MAX_ENRICH_ROLES_PER_SESSION = 2
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

#: Interactive button ids (≤256 chars) — the tap arrives as the id via
#: _button_id_of; typed labels keep working as a fallback for old clients.
BTN_SKIP = "enr_skip"
BTN_NONE = "enr_none"
BTN_OK = "enr_ok"
BTN_EDIT = "enr_edit"
BTN_DEL = "enr_del"

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

_EDIT_PROMPT = "تمام، اكتب لي إياها بكلماتك وأنا أعيد صياغتها 👌"

_ACK_THANKS = "تسلم يا [name] 🙏 هالمعلومة فرقت مرة وبتقوّي سيرتك فعلاً."
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


def _role_end_key(payload: dict[str, Any]) -> tuple[int, str]:
    """Recency sort: current roles (no end date) first, then latest end."""
    end = str(payload.get("end_date") or "").strip()
    current = not end or end.lower() in ("present", "current", "الحالي", "حاليا")
    return (0 if current else 1, "" if current else end)


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

    recent = sorted(exp, key=lambda f: _role_end_key(f.payload or {}))[:2]
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
    journey_context["enrichment"] = {
        "open": True, "current": str(role_fact_id), "queue": [],
        "opened_at": now.isoformat(),
    }
    session.flush()
    return True


def opening_message(session: Session, *, role_fact_id: uuid.UUID) -> str:
    role = session.get(ProfileFact, role_fact_id)
    title = str((role.payload or {}).get("title") or "خبرتك") if role else "خبرتك"
    return _ask(title)


def enrichment_buttons(has_more: bool) -> tuple[str, ...]:
    base = (_SKIP_ROLE, _NOTHING)
    return (*base, _STOP_ALL) if has_more else base


SKIP_LABELS = frozenset({_SKIP_ROLE, _NOTHING, _STOP_ALL, BTN_SKIP, BTN_NONE})
OK_LABELS = frozenset({_CONFIRM_OK, BTN_OK})
EDIT_LABELS = frozenset({_CONFIRM_EDIT, BTN_EDIT})
DEL_LABELS = frozenset({_CONFIRM_DEL, BTN_DEL})


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


def handle_answer(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    role_fact_id: uuid.UUID,
    arabic_answer: str,
    renderer: AchievementRenderer,
    now: datetime,
) -> dict[str, Any]:
    """Render + ground the colloquial answer. On success store a PENDING
    (EXTRACTED) achievement fact and return a confirm prompt; on failure
    return a re-ask. Never stores an ungrounded bullet."""
    # the answer is a customer-stated fact — strip PII before the model call.
    from career.onboarding.extraction import assert_no_pii, strip_pii

    stripped = strip_pii(arabic_answer, known_name=None)
    assert_no_pii(stripped.text, known_name=None)

    vocab = _bank_vocabulary(session, tenant_id=tenant_id)
    rendered = render_achievement(
        renderer, arabic_answer=stripped.text, vocabulary=vocab
    )
    if rendered is None:
        return {"status": "reask"}

    fact = ProfileFact(
        id=uuid.uuid4(), tenant_id=tenant_id, category="achievement",
        payload={
            "text": rendered["english_bullet"],
            "experience_fact_id": str(role_fact_id),
            "arabic_source": arabic_answer,          # audit only, never rendered
            "arabic_gloss": rendered["arabic_gloss"],  # confirm UI only
            "lang": "en",
        },
        status="EXTRACTED", source="conversation_achievement",
    )
    session.add(fact)
    session.flush()
    return {
        "status": "confirm",
        "pending_fact_id": str(fact.id),
        "prompt": _confirm_prompt(
            rendered["english_bullet"], rendered["arabic_gloss"]
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
    return personalize(_ACK_THANKS, name)



# ── the icebreaker examples menu (كاسر التجمّد) ──────────────────────────────

_PICK_MAP = {"1": 0, "١": 0, "2": 1, "٢": 1, "3": 2, "٣": 2}


def prepare_examples(
    session: Session, *, role_fact_id: uuid.UUID, writer: Any
) -> list[str]:
    """Three scrubbed colloquial examples for the role, or [] (no examples →
    the opening question stands alone; never blocks the nudge)."""
    from career.onboarding.achievement_render import scrub_examples

    role = session.get(ProfileFact, role_fact_id)
    if role is None or writer is None:
        return []
    payload = role.payload or {}
    title = str(payload.get("title") or "")
    description = str(payload.get("description") or "")
    try:
        raw = list(writer.write(title, description))
    except Exception:  # noqa: BLE001 — examples are optional garnish
        return []
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
        if not enqueue_enrichment(
            session, tenant_id=journey.tenant_id, role_fact_id=role.id,
            journey_context=context, trigger="post_activation_sweep", now=now,
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
        counts["swept"] += 1
    session.flush()
    return counts
