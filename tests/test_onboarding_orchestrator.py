"""Orchestrator acceptance tests (whitepaper §05) — the whole journey, wired.

The centerpiece is one END-TO-END conversation: activation → consents → the
eleven questions → CV upload (a real PDF through the §11 pipeline) → identity-
stripped extraction (fake boundary) → fact confirmation → gap question →
path review → policy card → «تأكيد وبدء البحث» → ACTIVE, with the 30-day
anchor stamped — resumable at every step, «دعم» untouched (C4 handles it
before us), privacy commands honored mid-journey, and honest failure paths
(rejected file regresses to upload with reasons).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text as sql_text

from career.db.session import tenant_session
from career.onboarding import extraction, orchestrator
from career.storage import FilesystemStorageAdapter
from career.whatsapp.client import FakeWhatsAppClient
from tests.test_onboarding_upload import CleanScanner, _pdf_with_text

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PHONE = "+966500000001"

FACTS = extraction.ExtractedFacts(
    experiences=[{"title": "Senior Business Analyst", "employer": "Acme",
                  "start_date": "2019", "end_date": None, "description": None,
                  "achievements": []}],
    education=[],
    certifications=[],
    skills=["SQL"],
    languages=[],
    achievements=[],
)


class FakeExtractor:
    def __init__(self, result: extraction.ExtractedFacts = FACTS) -> None:
        self.result = result

    def extract(self, cv_text: str) -> extraction.ExtractedFacts:
        return self.result


def _deps(tmp_path) -> orchestrator.Deps:
    client = FakeWhatsAppClient()
    # the fake media store: media-1 resolves to a real, clean PDF by default
    client.media["media-1"] = (_pdf_with_text("Senior Business Analyst at Acme"), "cv.pdf")
    return orchestrator.Deps(
        whatsapp_client=client,
        scanner=CleanScanner(),
        storage=FilesystemStorageAdapter(tmp_path),
        extractor=FakeExtractor(),
    )


def _seed_activated_tenant(session, tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Subscription in ONBOARDING (what activation leaves behind) + channel."""
    sub_id = uuid.uuid4()
    session.execute(
        sql_text(
            "INSERT INTO subscriptions "
            "(id, tenant_id, plan_code, status, salla_order_id, amount_sar, currency) "
            "VALUES (:id, :tid, 'basic', 'ONBOARDING', :oid, 149, 'SAR')"
        ),
        {"id": str(sub_id), "tid": str(tenant_id), "oid": f"O-{uuid.uuid4()}"},
    )
    channel_id = uuid.uuid4()
    session.execute(
        sql_text(
            "INSERT INTO customer_channels "
            "(id, tenant_id, subscription_id, provider, phone_e164, verified_at, "
            " opt_in_at, last_inbound_at) "
            "VALUES (:id, :tid, :sid, 'whatsapp', :phone, :now, :now, :now)"
        ),
        {"id": str(channel_id), "tid": str(tenant_id), "sid": str(sub_id),
         "phone": PHONE, "now": NOW},
    )
    return sub_id, channel_id


def _say(session, deps, channel_id: uuid.UUID, text: str, minutes: int) -> int:
    """Send one customer message; returns the next minute for readable flows."""
    orchestrator.handle_text(
        session, channel_id=channel_id, text=text, deps=deps,
        now=NOW + timedelta(minutes=minutes),
    )
    return minutes + 1


def _last_sent(deps: orchestrator.Deps) -> str:
    sent = deps.whatsapp_client.sent  # type: ignore[attr-defined]
    return sent[-1].body or ""


# ═════════════════════ THE END-TO-END JOURNEY ════════════════════════════════


def test_full_journey_from_activation_to_active(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)

    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)

        # ── activation hands over to the journey ─────────────────────────────
        journey = orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        assert journey.state == "CONSENT_PENDING"  # phone was just verified
        assert "التشغيل الأساسي" in _last_sent(deps)  # first consent prompt
        assert "سحب" in _last_sent(deps)  # rights text shown (§12)

        # ── consents: ONE merged message, one tap (CHANGELOG §10) ────────────
        assert "موافقة واحدة" in _last_sent(deps)
        m = 1
        m = _say(s, deps, channel_id, "أوافق", m)

        # ── the fourteen questions (CHANGELOG v1.1 §9 expansion) ─────────────
        assert "الاسم" in _last_sent(deps)
        answers = [
            "Fahad Almulhim", "fahad@example.com", "linkedin.com/in/fahad-x",
            "riyadh", "riyadh_region", "Senior Business Analyst", "9",
            "one_month", "full_time", "yes", "hybrid", "12000", "ar",
            "محلل أعمال",
        ]
        for answer in answers:
            m = _say(s, deps, channel_id, answer, m)

        # ── CV upload: a real PDF through the §11 pipeline ───────────────────
        assert "سيرتك" in _last_sent(deps)  # asked to upload
        journey = orchestrator.get_journey(s, tenant_id=tid)
        assert journey.state == "CV_UPLOAD_PENDING"
        orchestrator.handle_document(
            s, channel_id=channel_id, media_id="media-1",
            filename="cv.pdf", deps=deps, now=NOW + timedelta(minutes=m),
        )
        m += 1

        # extraction ran inline → the BATCH summary (CHANGELOG §10)
        journey = orchestrator.get_journey(s, tenant_id=tid)
        assert journey.state == "PROFILE_CONFIRMATION"
        summary = _last_sent(deps)
        assert "Senior Business Analyst" in summary          # numbered summary
        assert "تأكيد الكل" in summary

        # ── one tap confirms everything ──────────────────────────────────────
        m = _say(s, deps, channel_id, "تأكيد الكل", m)

        # no gaps (experience+skill exist) → straight to path review
        journey = orchestrator.get_journey(s, tenant_id=tid)
        assert journey.state == "CAREER_PATH_REVIEW"
        assert "محلل أعمال" in _last_sent(deps)  # the assessment presented

        m = _say(s, deps, channel_id, "approve", m)

        # ── policy card → confirm → ACTIVE ───────────────────────────────────
        journey = orchestrator.get_journey(s, tenant_id=tid)
        assert journey.state == "SEARCH_POLICY_REVIEW"
        card = _last_sent(deps)
        assert "الرياض" in card and "12000" in card

        m = _say(s, deps, channel_id, "confirm_policy", m)

    with tenant_session(a) as s:
        journey_row = s.execute(
            sql_text("SELECT state, completed_at FROM onboarding_sessions")
        ).one()
        sub_row = s.execute(
            sql_text("SELECT status, current_period_end FROM subscriptions")
        ).one()
        bank_count = s.execute(
            sql_text(
                "SELECT count(*) FROM profile_facts WHERE status IN "
                "('CUSTOMER_CONFIRMED','CUSTOMER_CORRECTED','OPERATOR_VERIFIED')"
            )
        ).scalar_one()
    assert journey_row.state == "ACTIVE"
    assert journey_row.completed_at is not None          # the 30-day anchor
    assert sub_row.status == "ACTIVE"
    assert sub_row.current_period_end is not None
    assert bank_count == 2                                # both confirmed facts
    assert "انطلقنا" in _last_sent(deps) or "نبدأ" in _last_sent(deps)


# ═════════════════════ resumability and edge paths ═══════════════════════════


def test_journey_is_resumable_mid_questions(two_tenants: tuple[str, str], tmp_path) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "أوافق", 1)
        _say(s, deps, channel_id, "Fahad Almulhim", 2)  # answered Q1 only

    # a fresh session (process restart) — the next nudge re-asks Q2, not Q1
    deps2 = _deps(tmp_path)
    with tenant_session(a) as s:
        _say(s, deps2, channel_id, "مرحبا؟", 60)
    assert "بريد" in _last_sent(deps2)  # email question — exactly where we left


def test_invalid_answer_reprompts_in_arabic_without_advancing(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "أوافق", 1)
        _say(s, deps, channel_id, "فهد الملحم", 2)  # Arabic name → invalid
        assert "بالإنجليزية" in _last_sent(deps)
        _say(s, deps, channel_id, "Fahad Almulhim", 3)  # now accepted
        assert "بريد" in _last_sent(deps)


def test_required_consent_refusal_explains_and_does_not_advance(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "لا أوافق", 1)
        # the refusal produces TWO messages: the explanation, then the re-ask
        last_two = [m.body or "" for m in deps.whatsapp_client.sent[-2:]]  # type: ignore[attr-defined]
        assert any("ضرورية" in body for body in last_two)
        assert "التشغيل الأساسي" in last_two[-1]      # re-asked, same purpose
        journey = orchestrator.get_journey(s, tenant_id=tid)
        assert journey.state == "CONSENT_PENDING"     # did not advance


def test_rejected_file_regresses_to_upload_with_reasons(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    # media returns an EXE masquerading as a PDF
    deps.whatsapp_client.media = {"media-1": (b"MZ\x90\x00evil", "cv.pdf")}  # type: ignore[attr-defined]
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        s.execute(
            sql_text(
                "UPDATE onboarding_sessions SET state = 'CV_UPLOAD_PENDING', "
                "context = '{}'::jsonb"
            )
        )
        for p in ("basic_processing", "external_providers", "daily_messages"):
            s.execute(
                sql_text(
                    "INSERT INTO consent_events (id, tenant_id, purpose, action) "
                    "VALUES (:id, :tid, :p, 'granted')"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "p": p},
            )
        orchestrator.handle_document(
            s, channel_id=channel_id, media_id="media-1", filename="cv.pdf",
            deps=deps, now=NOW,
        )
        journey = orchestrator.get_journey(s, tenant_id=tid)
        assert journey.state == "CV_UPLOAD_PENDING"  # regressed, not stuck
        assert "ما قدرنا" in _last_sent(deps) or "أعد" in _last_sent(deps)


def test_truncated_button_labels_resolve_to_values() -> None:
    """AUDIT (live TEN-0002 data): WhatsApp caps reply ids/titles at 20
    chars — a tap arriving as label_ar[:20] must resolve to the option id,
    never fall through to free text. All shipped labels are now ≤20 by
    design; this unit test guards the defense-in-depth for any future
    long label."""
    from career.onboarding.collection import Option, Question

    q = Question(
        key="city", prompt_ar="؟", kind="list",
        options=(Option("dammam", "الدمام / الخبر / الظهران", "الدمام"),),
    )
    truncated = "الدمام / الخبر / الظهران"[:20]
    assert orchestrator._resolve_question_input(q, truncated) == "dammam"
    assert orchestrator._resolve_question_input(q, "dammam") == "dammam"
    # every SHIPPED label fits the cap by design (the class is dead)
    from career.onboarding import collection as c
    for question in c.QUESTIONS:
        for option in question.options:
            assert len(option.label_ar) <= 20, option.label_ar


def test_correction_subflow_stores_customer_text(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "أوافق", 1)
        answers = ["Fahad Almulhim", "fahad@example.com", "__skip__",
                   "riyadh", "eastern", "Senior BA", "9", "one_month",
                   "full_time", "yes", "hybrid", "12000", "ar", "محلل أعمال"]
        for i, answer in enumerate(answers):
            _say(s, deps, channel_id, answer, 2 + i)
        orchestrator.handle_document(
            s, channel_id=channel_id, media_id="media-1", filename="cv.pdf",
            deps=deps, now=NOW + timedelta(minutes=30),
        )
        _say(s, deps, channel_id, "تعديل بند", 31)
        assert "رقم البند" in _last_sent(deps)   # asked for number + fix
        _say(s, deps, channel_id, "1 Lead Business Analyst — Acme", 32)
    with tenant_session(a) as s:
        row = s.execute(
            sql_text(
                "SELECT status, payload->>'customer_correction' AS fix, "
                "original_payload->>'title' AS original FROM profile_facts "
                "WHERE category = 'experience'"
            )
        ).one()
    assert row.status == "CUSTOMER_CORRECTED"
    assert row.fix == "Lead Business Analyst — Acme"
    assert row.original == "Senior Business Analyst"


def test_privacy_command_works_mid_journey(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "حالة اشتراكي", 1)
        assert "اشتراكك" in _last_sent(deps)
        # and the journey did not advance or break
        journey = orchestrator.get_journey(s, tenant_id=tid)
        assert journey.state == "CONSENT_PENDING"


def test_privacy_commands_survive_the_spellings_a_real_keyboard_produces() -> None:
    """AUDIT 2026-08. The standing commands were matched by RAW byte equality
    (``_PRIVACY_COMMANDS.get(text.strip())``, and inbound's ``normalize`` only
    collapses whitespace), so the deletion the customer is told to send
    VERBATIM did not execute when they typed the hamza-less «اؤكد» their
    keyboard offers — a statutory PDPL request failing in total silence, the
    worst available failure mode. These are all the same command."""
    from career.onboarding.orchestrator import _privacy_command

    assert _privacy_command("أؤكد حذف بياناتي") == "delete_execute"
    assert _privacy_command("اؤكد حذف بياناتي") == "delete_execute"
    assert _privacy_command("اؤكد حذف بياناتى") == "delete_execute"
    assert _privacy_command("حالة اشتراكي") == "status"
    assert _privacy_command("حالة اشتراكى") == "status"
    assert _privacy_command("حاله اشتراكي؟") == "status"
    assert _privacy_command("تجديد الإشتراك") == "status"
    assert _privacy_command("  وقف   مؤقت  ") == "pause"
    assert _privacy_command("تصدير بياناتى") == "export"
    assert _privacy_command("حذف بياناتى") == "delete_warn"
    assert _privacy_command("سؤال عن حالة اشتراكي") is None   # not a command


def test_normalisation_does_not_widen_the_two_step_deletion_gate() -> None:
    """The second step is a deliberate safety gate. Folding hamza forms adds
    ONLY spellings of the very same three words: no synonym, no bare «نعم»,
    and no substring — «لا أؤكد حذف بياناتي» carries the phrase and must
    still not delete anything."""
    from career.onboarding.orchestrator import _privacy_command

    for text in ("نعم", "تم", "أوافق", "اوك", "أؤكد", "أؤكد الحذف", "احذف",
                 "لا أؤكد حذف بياناتي", "أؤكد حذف بياناتي بعدين",
                 "قال لي أرسل أؤكد حذف بياناتي"):
        assert _privacy_command(text) != "delete_execute", text
    # the FIRST step still only warns — it never executes
    assert _privacy_command("حذف بياناتي") == "delete_warn"


def test_the_hamza_less_confirmation_really_executes_the_deletion(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    """Same two-step journey as below, typed the way a Saudi phone types it."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "حذف بياناتي", 1)
        # an ordinary «نعم» must NOT be read as the confirmation
        _say(s, deps, channel_id, "نعم", 2)
        assert s.execute(
            sql_text("SELECT count(*) FROM privacy_requests")
        ).scalar_one() == 0
        _say(s, deps, channel_id, "اؤكد حذف بياناتي", 3)
    with tenant_session(a) as s:
        req = s.execute(sql_text("SELECT kind, status FROM privacy_requests")).one()
        channels = s.execute(
            sql_text("SELECT count(*) FROM customer_channels")
        ).scalar_one()
    assert (req.kind, req.status) == ("delete", "fulfilled")
    assert channels == 0


def test_typed_consent_without_the_hamza_is_accepted(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    """The gate sends buttons here, but the copy also invites the typed word —
    and the typed word was compared byte-for-byte against «أوافق»."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "اوافق", 1)
        assert "الاسم" in _last_sent(deps)          # the questions began
        granted = s.execute(
            sql_text("SELECT count(*) FROM consent_events "
                     "WHERE action = 'granted'")
        ).scalar_one()
        assert granted == 3


def test_a_negated_consent_is_never_read_as_agreement(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    """«ما أوافق» and «لا اوافق» both CONTAIN the agreement word. Recording a
    grant from either would be consent by accident — worse than any loop."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        for minute, reply in enumerate(("ما أوافق", "لا اوافق", "مو موافق"), 1):
            _say(s, deps, channel_id, reply, minute)
            granted = s.execute(
                sql_text("SELECT count(*) FROM consent_events "
                         "WHERE action = 'granted'")
            ).scalar_one()
            assert granted == 0, reply
            assert orchestrator.get_journey(s, tenant_id=tid).state \
                == "CONSENT_PENDING"


def test_an_acknowledgement_never_grants_the_consent(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    """«تمام» is an acknowledgement, not a legal consent. It is answered with
    a clear ask; the ledger stays empty until the customer says the word."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "تمام", 1)
        granted = s.execute(
            sql_text("SELECT count(*) FROM consent_events "
                     "WHERE action = 'granted'")
        ).scalar_one()
        assert granted == 0
        assert "موافقة واحدة" in _last_sent(deps)      # re-asked, not advanced
        _say(s, deps, channel_id, "موافق", 2)
        assert "الاسم" in _last_sent(deps)             # and then it opens


def test_deletion_needs_the_explicit_second_step(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "حذف بياناتي", 1)
        assert "أؤكد حذف بياناتي" in _last_sent(deps)  # warned + asked to confirm
        count = s.execute(sql_text("SELECT count(*) FROM privacy_requests")).scalar_one()
        assert count == 0  # nothing destructive yet
        _say(s, deps, channel_id, "أؤكد حذف بياناتي", 2)
    with tenant_session(a) as s:
        req = s.execute(
            sql_text("SELECT kind, status FROM privacy_requests")
        ).one()
        channels = s.execute(
            sql_text("SELECT count(*) FROM customer_channels")
        ).scalar_one()
    assert (req.kind, req.status) == ("delete", "fulfilled")
    assert channels == 0  # الرقم أُزيل


# ═════════════════════ the >24h reminder runner ══════════════════════════════


def test_reminder_runner_nudges_stalled_journeys_once(
    two_tenants: tuple[str, str], tmp_path, owner_engine
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    deps = _deps(tmp_path)
    with tenant_session(a) as s:
        sub_id, channel_id = _seed_activated_tenant(s, tid)
        orchestrator.start_journey(
            s, tenant_id=tid, subscription_id=sub_id, channel_id=channel_id,
            deps=deps, now=NOW,
        )
        _say(s, deps, channel_id, "أوافق", 1)  # interacted once, then silence

    from sqlalchemy.orm import Session

    with Session(owner_engine) as s:
        sent_count_before = len(deps.whatsapp_client.sent)  # type: ignore[attr-defined]
        first = orchestrator.send_due_reminders(
            s, deps=deps, now=NOW + timedelta(hours=25)
        )
        s.commit()
        second = orchestrator.send_due_reminders(
            s, deps=deps, now=NOW + timedelta(hours=26)
        )
    assert first == 1                                     # one stalled journey
    assert second == 0                                    # once per stall
    sent = deps.whatsapp_client.sent  # type: ignore[attr-defined]
    assert len(sent) == sent_count_before + 1
    # a >24h stall = closed window → the APPROVED template, never free text
    assert sent[-1].kind == "template"
    assert sent[-1].template_name == "onboarding_reminder"
