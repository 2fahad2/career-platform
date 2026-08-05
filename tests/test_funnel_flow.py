"""The funnel journey (§04, C8) — purchase → consents → upload → report.

The §13-C8 exit condition's first half: from a paid cv_analysis order to a
delivered Arabic report within one conversation, all through the SAME worker
entry the production webhook feeds — the funnel reuses the C5 authorities
(consents, §11 upload, extraction) verbatim and never touches the
onboarding FSM.

The second half of this file drives ``funnel/flow.py`` DIRECTLY. The audit of
2026-08 found that this module — named after the funnel flow — imported the
orchestrator and the extractor and never the flow at all, so the 29-riyal
consent gate (the one step every paying analysis customer must pass) had no
test of its own while it was an infinite loop in production.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.funnel import flow as funnel_flow
from career.onboarding import extraction, orchestrator
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.storage import FilesystemStorageAdapter
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.worker import _handle_message
from tests.test_onboarding_upload import CleanScanner, _pdf_with_text

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

FACTS = extraction.ExtractedFacts(
    experiences=[{"title": "Senior Business Analyst", "employer": "Alpha Bank",
                  "start_date": "2021-03", "end_date": "Present",
                  "description": "Requirements and UAT.",
                  "achievements": ["Cut rework by 30%."]}],
    education=[{"degree": "BSc", "field_of_study": "MIS",
                "institution": "KSU", "graduation_year": 2017}],
    certifications=[{"name": "PMI-PBA", "issuer": "PMI", "issue_date": "2022-05"}],
    skills=["SQL", "Power BI", "BPMN", "Requirements", "UAT"],
    languages=[{"language": "English", "proficiency": "Fluent"}],
    achievements=[],
)


class FakeExtractor:
    def extract(self, cv_text: str) -> extraction.ExtractedFacts:
        return FACTS


def _probe_phone() -> str:
    """A real Salla order ALWAYS carries the buyer's mobile — the whole
    zero-touch activation path keys on it. Test orders that omitted it were
    describing a shape the world never sends, and that unrealism is exactly
    how the phone defect survived: the suite was green while no real customer
    could have been activated."""
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _deps(tmp_path) -> orchestrator.Deps:
    client = FakeWhatsAppClient()
    client.media["media-9"] = (
        _pdf_with_text("Senior Business Analyst fahad@example.com"), "cv.pdf"
    )
    return orchestrator.Deps(
        whatsapp_client=client,
        scanner=CleanScanner(),
        storage=FilesystemStorageAdapter(tmp_path),
        extractor=FakeExtractor(),
    )


def _provision_funnel(owner: Session) -> str:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_cv", Decimal("29"), "SAR",
                       customer_phone=_probe_phone())
    })
    result = provision_order(
        owner, order_id, salla_client=client,
        product_catalog={"prod_cv": "cv_analysis"},
        expected_pricing={k: (Decimal("29"), "SAR") for k in {"prod_cv": "cv_analysis"}},
    )
    assert result.activation_token is not None
    return result.activation_token


def _msg_text(phone: str, body: str) -> dict:
    return {"id": f"wamid.{uuid.uuid4().hex}", "from": phone,
            "type": "text", "text": {"body": body}}


def _msg_doc(phone: str, media_id: str) -> dict:
    return {"id": f"wamid.{uuid.uuid4().hex}", "from": phone,
            "type": "document",
            "document": {"id": media_id, "filename": "cv.pdf"}}


def test_purchase_to_report_in_one_conversation(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()

    def say(body: str, minutes: int) -> None:
        _handle_message(
            owner_session, _msg_text(phone, body),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=minutes), onboarding=deps,
        )

    # activation routes to the FUNNEL, not the onboarding
    say(f"تفعيل {token}", 0)
    sent = [m.body or "" for m in deps.whatsapp_client.sent]
    assert any("تحليل السيرة" in b for b in sent)          # funnel welcome
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        # ONE merged consent (CHANGELOG §10)
        say("أوافق", 1)
        assert any("أرسل سيرتك" in (m.body or "")
                   for m in deps.whatsapp_client.sent)

        # the CV document through the §11 pipeline
        _handle_message(
            owner_session, _msg_doc(phone, "media-9"),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=5), onboarding=deps,
        )
        assert any("المسار الوظيفي" in (m.body or "")
                   for m in deps.whatsapp_client.sent)

        # the requested path → the report lands: PDF + Arabic summary
        say("محلل أعمال", 6)
        kinds = [m.kind for m in deps.whatsapp_client.sent]
        assert "document" in kinds
        doc = [m for m in deps.whatsapp_client.sent if m.kind == "document"][-1]
        assert "funnel_reports/" in str(doc.document_ref)
        summary = deps.whatsapp_client.sent[-1].body or ""
        assert "الدرجة العامة" in summary
        assert "البحث اليومي" in summary                    # the upgrade CTA

        # the report persisted on the row; the day is DONE and resumable-safe
        row = owner_session.execute(
            sql_text("SELECT state, report FROM funnel_sessions "
                     "WHERE tenant_id = :t"), {"t": str(tenant_id)},
        ).one()
        assert row.state == "DONE"
        assert row.report["overall"] > 0
        # facts stored EXTRACTED — the upgrade inheritance raw material
        count = owner_session.execute(
            sql_text("SELECT count(*) FROM profile_facts WHERE tenant_id = :t "
                     "AND status = 'EXTRACTED'"), {"t": str(tenant_id)},
        ).scalar_one()
        assert count >= 5
    finally:
        owner_session.rollback()          # never leave a wedged transaction
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()


def test_upgrade_relinks_and_opens_half_ready_onboarding(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """The §13-C8 exit condition's second half: the funnel customer buys a
    subscription with the SAME phone → the new purchase re-links to their
    existing tenant (the facts live there), consents carry over, and the
    onboarding skips the upload leg straight into fact confirmation."""
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()

    def handle(msg: dict, minutes: int) -> None:
        _handle_message(
            owner_session, msg, whatsapp_client=deps.whatsapp_client,
            admin_client=admin, now=NOW + timedelta(minutes=minutes),
            onboarding=deps,
        )

    # ── the full funnel journey first ────────────────────────────────────
    handle(_msg_text(phone, f"تفعيل {token}"), 0)
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        handle(_msg_text(phone, "أوافق"), 1)
        handle(_msg_doc(phone, "media-9"), 5)
        handle(_msg_text(phone, "محلل أعمال"), 6)
        assert owner_session.execute(
            sql_text("SELECT state FROM funnel_sessions WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar_one() == "DONE"

        # ── the upgrade: a basic order, activated from the SAME phone ────
        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({
            order_id: SallaOrder(order_id, "paid", "prod_basic",
                                 Decimal("149"), "SAR",
                       customer_phone=_probe_phone())
        })
        upgrade = provision_order(
            owner_session, order_id, salla_client=client,
            product_catalog={"prod_basic": "basic"},
            expected_pricing={"prod_basic": (Decimal("149"), "SAR")},
        )
        assert upgrade.activation_token is not None
        handle(_msg_text(phone, f"تفعيل {upgrade.activation_token}"), 10)

        # the subscription re-linked to the FUNNEL tenant, not the shell
        plans = owner_session.execute(
            sql_text("SELECT plan_code FROM subscriptions WHERE tenant_id = :t "
                     "ORDER BY plan_code"), {"t": str(tenant_id)},
        ).scalars().all()
        assert plans == ["basic", "cv_analysis"]
        assert any("ترقية" in m for m in admin.messages)     # admin informed

        # every required consent carried over — the journey goes STRAIGHT to
        # the questions (the optional purpose never enters onboarding, §10)
        assert any("الاسم" in (m.body or "")
                   for m in deps.whatsapp_client.sent[-3:])

        # answer the fourteen questions — then NO upload prompt: straight
        # into fact confirmation (half-ready, §04)
        answers = ["Fahad Almulhim", "fahad@example.com", "__skip__",
                   "riyadh", "riyadh_region", "Senior BA", "9", "one_month",
                   "full_time", "yes", "hybrid", "12000", "ar", "محلل أعمال"]
        for i, answer in enumerate(answers):
            handle(_msg_text(phone, answer), 11 + i)

        journey_state = owner_session.execute(
            sql_text("SELECT state FROM onboarding_sessions WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar_one()
        assert journey_state == "PROFILE_CONFIRMATION"
        recent = [m.body or "" for m in deps.whatsapp_client.sent[-3:]]
        assert any("قرأنا سيرتك سابقًا" in b for b in recent)
        assert not any("أرسل سيرتك" in b for b in recent)     # no upload leg
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()


def test_expired_upgrade_token_never_relinks(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """AUDIT FIX A: an invalid/expired token from a funnel phone must reject
    WITHOUT side effects — the subscription must never move before the token
    passes every validity check (the worker commits after activate)."""
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()
    _handle_message(
        owner_session, _msg_text(phone, f"تفعيل {token}"),
        whatsapp_client=deps.whatsapp_client, admin_client=admin,
        now=NOW, onboarding=deps,
    )
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({
            order_id: SallaOrder(order_id, "paid", "prod_basic",
                                 Decimal("149"), "SAR",
                       customer_phone=_probe_phone())
        })
        upgrade = provision_order(
            owner_session, order_id, salla_client=client,
            product_catalog={"prod_basic": "basic"},
            expected_pricing={"prod_basic": (Decimal("149"), "SAR")},
        )
        assert upgrade.activation_token is not None
        # expire the upgrade token BEFORE the customer taps it
        owner_session.execute(
            sql_text("UPDATE activation_tokens SET expires_at = :past "
                     "WHERE tenant_id = :shell"),
            {"past": NOW - timedelta(days=1), "shell": upgrade.tenant_id},
        )
        owner_session.commit()

        _handle_message(
            owner_session, _msg_text(phone, f"تفعيل {upgrade.activation_token}"),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=10), onboarding=deps,
        )
        # the basic subscription must still belong to the SHELL, untouched
        plans_funnel = owner_session.execute(
            sql_text("SELECT plan_code FROM subscriptions WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalars().all()
        assert plans_funnel == ["cv_analysis"]           # nothing moved
        shell_plans = owner_session.execute(
            sql_text("SELECT plan_code FROM subscriptions WHERE tenant_id = :t"),
            {"t": str(upgrade.tenant_id)},
        ).scalars().all()
        assert shell_plans == ["basic"]
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()


def test_second_analysis_purchase_restarts_the_funnel(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """AUDIT FIX B: a paying second cv_analysis purchase must produce a second
    report — the DONE session resets (consents carry over presence-driven,
    so it reopens at the upload step)."""
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()

    def handle(msg: dict, minutes: int) -> None:
        _handle_message(
            owner_session, msg, whatsapp_client=deps.whatsapp_client,
            admin_client=admin, now=NOW + timedelta(minutes=minutes),
            onboarding=deps,
        )

    handle(_msg_text(phone, f"تفعيل {token}"), 0)
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        handle(_msg_text(phone, "أوافق"), 1)
        handle(_msg_doc(phone, "media-9"), 5)
        handle(_msg_text(phone, "محلل أعمال"), 6)

        # the SECOND analysis purchase, same phone
        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({
            order_id: SallaOrder(order_id, "paid", "prod_cv",
                                 Decimal("29"), "SAR",
                       customer_phone=_probe_phone())
        })
        second = provision_order(
            owner_session, order_id, salla_client=client,
            product_catalog={"prod_cv": "cv_analysis"},
            expected_pricing={k: (Decimal("29"), "SAR") for k in {"prod_cv": "cv_analysis"}},
        )
        assert second.activation_token is not None
        handle(_msg_text(phone, f"تفعيل {second.activation_token}"), 10)

        state = owner_session.execute(
            sql_text("SELECT state FROM funnel_sessions WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar_one()
        assert state == "UPLOAD_PENDING"                 # reopened, not DONE
        assert any("أرسل سيرتك" in (m.body or "")
                   for m in deps.whatsapp_client.sent[-2:])

        # and the second journey completes to a fresh report
        handle(_msg_doc(phone, "media-9"), 11)
        handle(_msg_text(phone, "محلل أعمال"), 12)
        docs = [m for m in deps.whatsapp_client.sent if m.kind == "document"]
        assert len(docs) == 2                            # two reports delivered
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()


def test_consent_refusal_explains_and_holds(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()
    _handle_message(
        owner_session, _msg_text(phone, f"تفعيل {token}"),
        whatsapp_client=deps.whatsapp_client, admin_client=admin,
        now=NOW, onboarding=deps,
    )
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        _handle_message(
            owner_session, _msg_text(phone, "لا أوافق"),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=1), onboarding=deps,
        )
        assert any("ضرورية" in (m.body or "")
                   for m in deps.whatsapp_client.sent)
        state = owner_session.execute(
            sql_text("SELECT state FROM funnel_sessions WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar_one()
        assert state == "CONSENT_PENDING"                   # did not advance
    finally:
        owner_session.rollback()          # never leave a wedged transaction
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()


def test_rejected_file_returns_honestly(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    deps.whatsapp_client.media["media-bad"] = (b"MZ\x90\x00 not a pdf", "cv.pdf")
    admin = FakeTelegramAdminClient()
    _handle_message(
        owner_session, _msg_text(phone, f"تفعيل {token}"),
        whatsapp_client=deps.whatsapp_client, admin_client=admin,
        now=NOW, onboarding=deps,
    )
    tenant_id = owner_session.execute(
        sql_text("SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    try:
        _handle_message(
            owner_session, _msg_text(phone, "أوافق"),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=1), onboarding=deps,
        )
        _handle_message(
            owner_session, _msg_doc(phone, "media-bad"),
            whatsapp_client=deps.whatsapp_client, admin_client=admin,
            now=NOW + timedelta(minutes=5), onboarding=deps,
        )
        assert any("ما قدرنا نقبل الملف" in (m.body or "")
                   for m in deps.whatsapp_client.sent)
        state = owner_session.execute(
            sql_text("SELECT state FROM funnel_sessions WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar_one()
        assert state == "UPLOAD_PENDING"                    # still waiting
    finally:
        owner_session.rollback()          # never leave a wedged transaction
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": str(tenant_id)}
        )
        owner_session.commit()


def test_privacy_commands_work_for_funnel_customers(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """AUDIT ك-7: «حالة اشتراكي» (and every standing command) must be handled
    for a funnel customer BEFORE reaching the funnel branch — previously the
    command was consumed as a career-path answer or refused for lack of an
    onboarding journey."""
    from career.whatsapp.worker import _handle_message

    token = _provision_funnel(owner_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()
    # activate → the funnel conversation opens
    _handle_message(owner_session, _msg_text(phone, f"تفعيل {token}"),
                    whatsapp_client=deps.whatsapp_client, admin_client=admin,
                    now=NOW, onboarding=deps)
    owner_session.commit()
    before = len(deps.whatsapp_client.sent)
    # mid-funnel privacy command must NOT be consumed as a funnel answer
    _handle_message(owner_session, _msg_text(phone, "حالة اشتراكي"),
                    whatsapp_client=deps.whatsapp_client, admin_client=admin,
                    now=NOW, onboarding=deps)
    owner_session.commit()
    sent = deps.whatsapp_client.sent
    assert len(sent) > before
    assert "اشتراك" in (sent[-1].body or "")


def test_cv_header_name_is_stripped_before_the_model(tmp_path) -> None:
    """The funnel never asks for a name, so the CV header was reaching the
    model verbatim while the privacy page promised otherwise (closure ح-3)."""
    from career.onboarding.extraction import infer_header_name, strip_pii

    cv = (
        "Faisal Al-Otaibi\n"
        "faisal@example.com | +966 50 000 0000\n\n"
        "SUMMARY\nBusiness analyst with six years of experience.\n"
    )
    name = infer_header_name(cv)
    assert name == "Faisal Al-Otaibi"
    safe = strip_pii(cv, known_name=name).text
    assert "Faisal" not in safe and "Al-Otaibi" not in safe
    assert "[NAME]" in safe


def test_header_inference_skips_section_headings() -> None:
    from career.onboarding.extraction import infer_header_name

    assert infer_header_name("CURRICULUM VITAE\nSUMMARY\n") is None
    assert infer_header_name("السيرة الذاتية\nنبذة عني\n") is None
    assert infer_header_name("Senior Analyst, Riyadh\n") is None   # punctuation
    assert infer_header_name("Team of 5 engineers\n") is None       # digits


# ═══════════ THE CONSENT GATE — the 29-riyal customer's first step ═══════════
#
# AUDIT 2026-08. The gate compared RAW bytes against «أوافق» and re-sent the
# identical wall on every miss, forever: «اوافق» (the hamza-less spelling the
# default Saudi Android keyboard produces), «موافق», «نعم», «تمام» and «اوك»
# all landed in the else. The customer had already PAID. These tests pin both
# halves of the fix: the reply is understood, and when it truly is not, the
# conversation still has a way out.


def _activate_funnel(
    owner: Session, deps: orchestrator.Deps, admin: FakeTelegramAdminClient,
    phone: str,
) -> tuple[str, uuid.UUID]:
    """Take a paid analysis order all the way to the open consent gate.
    Returns (tenant_id, channel_id) so the tests can drive ``flow`` itself."""
    token = _provision_funnel(owner)
    _handle_message(
        owner, _msg_text(phone, f"تفعيل {token}"),
        whatsapp_client=deps.whatsapp_client, admin_client=admin,
        now=NOW, onboarding=deps,
    )
    row = owner.execute(
        sql_text("SELECT id, tenant_id FROM customer_channels "
                 "WHERE phone_e164 = :p"), {"p": phone},
    ).one()
    return str(row.tenant_id), row.id


def _say_to_funnel(
    owner: Session, channel_id: uuid.UUID, body: str,
    deps: orchestrator.Deps, minutes: int,
) -> None:
    funnel_flow.handle_funnel_text(
        owner, channel_id=channel_id, text=body, deps=deps,
        now=NOW + timedelta(minutes=minutes),
    )


def _state(owner: Session, tenant_id: str) -> str:
    return owner.execute(
        sql_text("SELECT state FROM funnel_sessions WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).scalar_one()


def _grants(owner: Session, tenant_id: str) -> int:
    return owner.execute(
        sql_text("SELECT count(*) FROM consent_events WHERE tenant_id = :t "
                 "AND action = 'granted'"), {"t": tenant_id},
    ).scalar_one()


def test_consent_replies_are_read_without_ever_inventing_an_agreement() -> None:
    """The classifier the gate runs on. Two rules decide every line here:
    the obvious spellings of «أوافق» must pass, and NOTHING ambiguous may be
    recorded as consent — a granted consent event is a legal artifact (PDPL),
    so «تمام» gets a clarifying ask, never a grant."""
    from career.onboarding.orchestrator import (
        CONSENT_ACK,
        CONSENT_AGREE,
        CONSENT_DECLINE,
        CONSENT_UNCLEAR,
        classify_consent_reply,
    )

    for yes in ("أوافق", "اوافق", "أوافق ✅", "موافق", "موافقة", "نعم",
                "اوافق يا اخوي", "نعم أوافق", "consent_agree"):
        assert classify_consent_reply(yes) == CONSENT_AGREE, yes

    # negation FIRST: substring matching on «اوافق» would read every one of
    # these as a grant, which is worse than the loop it replaces.
    for no in ("لا أوافق", "لا اوافق", "ما أوافق", "ما اوافق", "مو موافق",
               "لا", "لا شكرا", "أرفض", "ارفض", "consent_decline"):
        assert classify_consent_reply(no) == CONSENT_DECLINE, no

    # acknowledgement ≠ agreement — answered, but never recorded as consent
    for maybe in ("تمام", "اوك", "أوكي", "ماشي", "طيب", "خلاص", "أكيد"):
        assert classify_consent_reply(maybe) == CONSENT_ACK, maybe

    for unclear in ("", "   ", "ما فهمت", "وش هذا", "كيف أرفع سيرتي؟",
                    "أوافق بس عندي سؤال أول"):
        assert classify_consent_reply(unclear) == CONSENT_UNCLEAR, unclear


def test_a_negation_in_another_clause_is_not_a_refusal() -> None:
    """AUDIT 2026-08-05. «لا مشكلة» and «ما عندي مانع» are among the most
    natural ways a Saudi customer says YES, and the veto read the «لا» and
    the «ما» as scoping the agreement standing right next to them. A paying
    customer agreeing in plain Arabic was told their refusal was recorded.

    The counterweight is the whole point and is asserted in the same test: no
    line here may be reachable by «affirmation beats negation», because «لا
    أوافق» contains «أوافق» and inventing a consent is the one failure worse
    than the loop."""
    from career.onboarding.orchestrator import (
        CONSENT_ACK,
        CONSENT_AGREE,
        CONSENT_DECLINE,
        classify_consent_reply,
    )

    for yes in ("لا مشكلة، موافق", "ما عندي مانع، موافق", "ما شاء الله موافق",
                "لا مشكلة أوافق", "ما فيه مانع موافق", "ما عندي مشكلة موافق"):
        assert classify_consent_reply(yes) == CONSENT_AGREE, yes

    # the phrase ALONE is a yes to a human and not one to a regulator — same
    # call the module already makes on «تمام», so it gets the explicit ask
    for soft in ("لا مشكلة", "ما عندي مانع", "لا مانع"):
        assert classify_consent_reply(soft) == CONSENT_ACK, soft

    # …and the negation that really does scope the agreement still wins
    for no in ("لا أوافق", "ما أوافق", "مو موافق", "ما أبي أوافق",
               "لا مشكلة بس ما أوافق"):
        assert classify_consent_reply(no) == CONSENT_DECLINE, no


def test_the_consent_gate_ships_with_real_buttons(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """The funnel got the copy without the buttons: onboarding sends real
    interactive replies at the same gate, the funnel sent plain text and told
    the customer to TYPE an Arabic word with a hamza on it."""
    deps = _deps(tmp_path)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    tenant_id, _ = _activate_funnel(
        owner_session, deps, FakeTelegramAdminClient(), phone)
    try:
        prompt = deps.whatsapp_client.sent[-1]
        assert prompt.kind == "interactive"
        assert funnel_flow.CONSENT_AGREE_ID in prompt.buttons
        assert funnel_flow.CONSENT_DECLINE_ID in prompt.buttons
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        owner_session.commit()


def test_the_hamza_less_agreement_opens_the_upload_step(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """«اوافق» is what most Saudi Android keyboards produce. It used to fall
    into the else branch of a gate the customer had already paid to pass."""
    deps = _deps(tmp_path)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    tenant_id, channel_id = _activate_funnel(
        owner_session, deps, FakeTelegramAdminClient(), phone)
    try:
        _say_to_funnel(owner_session, channel_id, "اوافق", deps, 1)
        assert _state(owner_session, tenant_id) == funnel_flow.STATE_UPLOAD
        assert _grants(owner_session, tenant_id) == 3     # the three required
        assert "أرسل سيرتك" in (deps.whatsapp_client.sent[-1].body or "")
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        owner_session.commit()


def test_a_negated_reply_never_records_a_grant(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """«ما أوافق» and «لا اوافق» contain the agreement word. Reading them as
    agreement would record a consent the customer explicitly refused — the
    one failure worse than the loop."""
    deps = _deps(tmp_path)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    tenant_id, channel_id = _activate_funnel(
        owner_session, deps, FakeTelegramAdminClient(), phone)
    try:
        for minute, reply in enumerate(("ما أوافق", "لا اوافق", "مو موافق"), 1):
            _say_to_funnel(owner_session, channel_id, reply, deps, minute)
            assert _grants(owner_session, tenant_id) == 0, reply
            assert _state(owner_session, tenant_id) == funnel_flow.STATE_CONSENT
        assert any("ضرورية" in (m.body or "")
                   for m in deps.whatsapp_client.sent[-2:])
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        owner_session.commit()


def test_an_acknowledgement_is_answered_but_never_granted(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """«تمام» means «got it», not «I consent». The customer must get a clear,
    DIFFERENT ask — and the consent ledger must stay empty until they say the
    word."""
    deps = _deps(tmp_path)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    tenant_id, channel_id = _activate_funnel(
        owner_session, deps, FakeTelegramAdminClient(), phone)
    try:
        wall = deps.whatsapp_client.sent[-1].body or ""
        _say_to_funnel(owner_session, channel_id, "تمام", deps, 1)
        reply = deps.whatsapp_client.sent[-1].body or ""
        assert _grants(owner_session, tenant_id) == 0
        assert reply != wall                     # not the identical wall again
        assert deps.whatsapp_client.sent[-1].kind == "interactive"
        # and the word itself still works right after
        _say_to_funnel(owner_session, channel_id, "أوافق", deps, 2)
        assert _state(owner_session, tenant_id) == funnel_flow.STATE_UPLOAD
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        owner_session.commit()


def test_unreadable_replies_escalate_instead_of_looping_forever(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """The heart of the defect: the gate had no attempt counter, no «لم أفهم»
    and no escalation, so a paying customer whose words we cannot read was
    walled in permanently. Every attempt must now say something NEW, and the
    last one must put a human on it."""
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()
    deps.admin_client = admin              # wired the way the runner wires it
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    tenant_id, channel_id = _activate_funnel(owner_session, deps, admin, phone)
    try:
        bodies: list[str] = []
        for minute, junk in enumerate(("وش هذا", "ما فهمت عليك", "كيف يعني"), 1):
            _say_to_funnel(owner_session, channel_id, junk, deps, minute)
            bodies.append(deps.whatsapp_client.sent[-1].body or "")
        assert len(set(bodies)) == 3            # never the same wall twice

        escalated = bodies[-1]
        assert "دعم" in escalated                # the escape hatch, spelled out
        opened = owner_session.execute(
            sql_text("SELECT count(*) FROM support_events WHERE tenant_id = :t "
                     "AND kind = :k AND status = 'open'"),
            {"t": tenant_id, "k": funnel_flow.CONSENT_STUCK_KIND},
        ).scalar_one()
        assert opened == 1                       # the operator has the ticket
        code = owner_session.execute(
            sql_text("SELECT code FROM tenants WHERE id = :t"), {"t": tenant_id},
        ).scalar_one()
        paged = [m for m in admin.messages if "consent stuck" in m]
        assert len(paged) == 1                   # …and was paged, once
        assert code in paged[0]                  # §13: the TEN code, never PII

        # a fourth unreadable reply is still ANSWERED (never silence) and does
        # not open a second ticket for the same stall
        _say_to_funnel(owner_session, channel_id, "؟؟", deps, 4)
        assert (deps.whatsapp_client.sent[-1].body or "") != ""
        again = owner_session.execute(
            sql_text("SELECT count(*) FROM support_events WHERE tenant_id = :t "
                     "AND kind = :k"),
            {"t": tenant_id, "k": funnel_flow.CONSENT_STUCK_KIND},
        ).scalar_one()
        assert again == 1
        # and the gate still opens the moment they type the word
        _say_to_funnel(owner_session, channel_id, "موافق", deps, 5)
        assert _state(owner_session, tenant_id) == funnel_flow.STATE_UPLOAD
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        owner_session.commit()


def test_a_repeated_refusal_reaches_a_human_instead_of_looping(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """AUDIT 2026-08-05, the other half of the ladder. DECLINE never touched
    the attempt counter: it explained and re-sent the wall, on the first
    refusal and on the hundredth, so the escalation could not be reached from
    that branch at all. A customer who reads the consent wall as something to
    refuse is precisely the customer who needs a human — and they have paid
    29 riyals to be standing at this gate."""
    deps = _deps(tmp_path)
    admin = FakeTelegramAdminClient()
    deps.admin_client = admin
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    tenant_id, channel_id = _activate_funnel(owner_session, deps, admin, phone)
    try:
        for minute, no in enumerate(("لا أوافق", "لا اوافق", "مو موافق"), 1):
            _say_to_funnel(owner_session, channel_id, no, deps, minute)
            assert _grants(owner_session, tenant_id) == 0, no

        opened = owner_session.execute(
            sql_text("SELECT count(*) FROM support_events WHERE tenant_id = :t "
                     "AND kind = :k AND status = 'open'"),
            {"t": tenant_id, "k": funnel_flow.CONSENT_STUCK_KIND},
        ).scalar_one()
        assert opened == 1, "three refusals and no ticket is the loop itself"
        paged = [m for m in admin.messages if "consent stuck" in m]
        assert len(paged) == 1
        assert phone not in paged[0]            # §13: the TEN code, never PII

        # the last word is still theirs: the gate opens the moment they agree
        _say_to_funnel(owner_session, channel_id, "أوافق", deps, 4)
        assert _state(owner_session, tenant_id) == funnel_flow.STATE_UPLOAD
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        owner_session.commit()


def test_the_path_step_holds_its_state_on_an_empty_target(
    owner_session: Session, clean_billing: None, tmp_path
) -> None:
    """The other half of the flow's own state machine, driven directly: a
    one-character path is re-asked and PATH_PENDING survives."""
    deps = _deps(tmp_path)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    admin = FakeTelegramAdminClient()
    tenant_id, channel_id = _activate_funnel(owner_session, deps, admin, phone)
    try:
        _say_to_funnel(owner_session, channel_id, "أوافق", deps, 1)
        funnel_flow.handle_funnel_document(
            owner_session, channel_id=channel_id, media_id="media-9",
            filename="cv.pdf", deps=deps, now=NOW + timedelta(minutes=5),
        )
        assert _state(owner_session, tenant_id) == funnel_flow.STATE_PATH
        _say_to_funnel(owner_session, channel_id, "ا", deps, 6)
        assert _state(owner_session, tenant_id) == funnel_flow.STATE_PATH
        assert "المسار الوظيفي" in (deps.whatsapp_client.sent[-1].body or "")
    finally:
        owner_session.rollback()
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        owner_session.commit()


def test_the_consent_copy_is_direction_pure() -> None:
    """§16 / Fahad's client: a line mixing Arabic with Latin letters or digits
    is scrambled on delivery. Every line the consent gate can send is checked
    — the gate is the one screen a paying customer cannot skip."""
    import re

    arabic = re.compile(r"[؀-ۿ]")
    latin = re.compile(r"[A-Za-z0-9]")

    bodies = [(name, value) for name, value in vars(funnel_flow).items()
              if "CONSENT" in name and isinstance(value, str)]
    bodies.append(("_consent_wall", funnel_flow._consent_wall()))
    assert len(bodies) >= 5           # the whole gate, not one lucky constant
    for name, body in bodies:
        for line in body.splitlines():
            if arabic.search(line) and latin.search(line):
                raise AssertionError(
                    f"{name}: mixed-direction line would scramble: {line!r}"
                )
