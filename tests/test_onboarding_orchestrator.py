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
    assert "نكمل" in sent[-1].body or "وقفنا" in sent[-1].body
