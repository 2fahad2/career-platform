"""WhatsApp inbound worker (DB) — end-to-end routing from a webhook payload.

Activation, STOP (immediate opt-out), support escalation, OTHER→descend,
delivery status callbacks, per-message idempotency, and no PII in the admin
channel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, WebhookEvent
from career.salla import subscriptions as st
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.delivery import DELIVERY_COMPLETED, deliver_adaptive
from career.whatsapp.templates import DAILY_UTILITY
from career.whatsapp.worker import process_pending_whatsapp

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
CATALOG = {"prod_pro": "professional"}


def _probe_phone() -> str:
    """A real Salla order ALWAYS carries the buyer's mobile — the whole
    zero-touch activation path keys on it. A test order without one describes
    a shape the world never sends, and that unrealism is how the phone defect
    survived: the suite was green while no real customer could be activated."""
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _payload(messages: list[dict[str, Any]] | None = None,
             statuses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "messages": messages or [], "statuses": statuses or [],
    }}]}]}


def _text_msg(wamid: str, phone: str, body: str) -> dict[str, Any]:
    return {"id": wamid, "from": phone, "type": "text", "text": {"body": body}}


def _insert_event(owner_session: Session, payload: dict[str, Any]) -> None:
    owner_session.add(WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="messages",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload=payload, processing_status="received",
    ))
    owner_session.commit()


def _provision_token(owner_session: Session) -> str:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"),
                             "SAR", customer_phone=_probe_phone())
    })
    pricing = {k: (Decimal("279.00"), "SAR") for k in CATALOG}
    tok = provision_order(owner_session, order_id, salla_client=client,
                          product_catalog=CATALOG,
                          expected_pricing=pricing).activation_token
    assert tok is not None
    return tok


def _phone() -> str:
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _run(owner_session: Session, wa: FakeWhatsAppClient, admin: FakeTelegramAdminClient,
         *, now: datetime = NOW) -> dict[str, int]:
    return process_pending_whatsapp(owner_session, whatsapp_client=wa, admin_client=admin, now=now)


def test_activation_via_worker_and_idempotency(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    token = _provision_token(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()

    _insert_event(owner_session, _payload([_text_msg("wamid-1", phone, f"تفعيل {token}")]))
    counts = _run(owner_session, wa, admin)
    assert counts["messages"] == 1

    with Session(owner_engine) as s:
        status = s.execute(
            text("SELECT sub.status FROM subscriptions sub JOIN customer_channels c"
                 " ON c.subscription_id = sub.id WHERE c.phone_e164 = :p"), {"p": phone},
        ).scalar_one()
        assert status == st.ONBOARDING

    # A redelivered message with the same wamid (different POST) must not re-activate.
    _insert_event(owner_session, _payload([_text_msg("wamid-1", phone, f"تفعيل {token}")]))
    _run(owner_session, wa, admin)
    with Session(owner_engine) as s:
        channels = s.execute(
            text("SELECT count(*) FROM customer_channels WHERE phone_e164 = :p"), {"p": phone},
        ).scalar_one()
        inbound = s.execute(
            text("SELECT count(*) FROM inbound_messages WHERE wa_message_id = 'wamid-1'")
        ).scalar_one()
    assert channels == 1
    assert inbound == 1


def _activate_phone(owner_session: Session, wa: FakeWhatsAppClient,
                    admin: FakeTelegramAdminClient) -> str:
    token = _provision_token(owner_session)
    phone = _phone()
    activate(owner_session, token=token, from_phone=phone, display_name=None, now=NOW,
             whatsapp_client=wa, admin_client=admin)
    return phone


def test_stop_opts_out_immediately(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    _insert_event(owner_session, _payload([_text_msg("wamid-stop", phone, "إيقاف الرسائل")]))
    _run(owner_session, wa, admin)
    with Session(owner_engine) as s:
        opt_out = s.execute(
            text("SELECT opt_out_at FROM customer_channels WHERE phone_e164 = :p"), {"p": phone},
        ).scalar_one()
    assert opt_out is not None  # opted out immediately


def test_support_escalates_without_pii(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    admin.messages.clear()  # ignore any activation-time admin noise
    _insert_event(owner_session, _payload([_text_msg("wamid-sup", phone, "دعم")]))
    _run(owner_session, wa, admin)
    with Session(owner_engine) as s:
        open_support = s.execute(
            text("SELECT count(*) FROM support_events WHERE status = 'open'")
        ).scalar_one()
    assert open_support == 1
    assert len(admin.messages) == 1
    # No PII: the phone number never appears in the admin channel (§15.13).
    digits = phone.lstrip("+")
    assert all(digits not in m for m in admin.messages)
    assert any("TEN-" in m for m in admin.messages)


def test_other_message_descends_pending_delivery(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    ch = owner_session.execute(
        select(CustomerChannel).where(CustomerChannel.phone_e164 == phone)
    ).scalar_one()
    # Make the window closed and queue a pending delivery.
    ch.last_inbound_at = NOW - timedelta(hours=25)
    owner_session.commit()
    bundle = {"parts": [{"kind": "text", "body": "#1 job"}]}
    deliver_adaptive(owner_session, ch, bundle, run_date=NOW.date(),
                     whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    # Customer taps (OTHER) → descend.
    _insert_event(owner_session, _payload([_text_msg("wamid-tap", phone, "عرض")]))
    _run(owner_session, wa, admin, now=NOW + timedelta(minutes=1))
    with Session(owner_engine) as s:
        status = s.execute(text("SELECT status FROM deliveries")).scalar_one()
    assert status == DELIVERY_COMPLETED


def test_delivery_status_callback_updates_message(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate_phone(owner_session, wa, admin)
    # The activation welcome recorded a delivery_message with the fake wamid.
    welcome_wamid = wa.sent[-1].message_id
    _insert_event(owner_session, _payload(statuses=[{"id": welcome_wamid, "status": "delivered"}]))
    _run(owner_session, wa, admin)
    with Session(owner_engine) as s:
        status = s.execute(
            text("SELECT status FROM delivery_messages WHERE wa_message_id = :m"),
            {"m": welcome_wamid},
        ).scalar_one()
    assert status == "delivered"


# ── closure audit: a paying customer must never meet silence ────────────────


def _activate(owner_session: Session, wa: Any, admin: Any, phone: str) -> None:
    tok = _provision_token(owner_session)
    activate(owner_session, token=tok, from_phone=phone, display_name=None,
             now=NOW, whatsapp_client=wa, admin_client=admin)
    owner_session.commit()


def test_support_answers_the_customer_not_only_the_operator(
    owner_session: Session, clean_billing: None
) -> None:
    """«دعم» is printed in every error message and on the store page — it used
    to page the operator and answer the CUSTOMER with nothing at all."""
    from career.whatsapp.client import FakeWhatsAppClient
    from career.whatsapp.worker import _handle_message

    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, wa, admin, phone)
    before = len(wa.sent)
    _handle_message(owner_session,
                    _text_msg(f"wamid.{uuid.uuid4().hex}", phone, "دعم"),
                    whatsapp_client=wa, admin_client=admin, now=NOW)
    owner_session.commit()
    assert len(wa.sent) > before, "the customer got silence"
    assert "الفريق" in (wa.sent[-1].body or "")


def test_data_rights_survive_an_opt_out(
    owner_session: Session, clean_billing: None
) -> None:
    """§12 + PDPL: an opt-out stops MARKETING, never the customer's own
    export / delete / status requests — those died with it before."""
    from career.whatsapp.client import FakeWhatsAppClient
    from career.whatsapp.worker import _handle_message

    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, wa, admin, phone)
    owner_session.execute(text(
        "UPDATE customer_channels SET opt_out_at = :n WHERE phone_e164 = :p"),
        {"n": NOW, "p": phone.lstrip("+")})
    owner_session.execute(text(
        "UPDATE customer_channels SET opt_out_at = :n WHERE phone_e164 = :p"),
        {"n": NOW, "p": phone})
    owner_session.commit()
    before = len(wa.sent)
    _handle_message(owner_session,
                    _text_msg(f"wamid.{uuid.uuid4().hex}", phone,
                              "حالة اشتراكي"),
                    whatsapp_client=wa, admin_client=admin, now=NOW,
                    onboarding=_worker_deps(wa))
    owner_session.commit()
    assert len(wa.sent) > before, "an opted-out customer lost their data rights"


def _worker_deps(wa: Any) -> Any:
    """Minimal Deps: the privacy commands need no model boundaries."""
    import tempfile

    from career.onboarding import orchestrator
    from career.storage import FilesystemStorageAdapter

    class _Scanner:
        def scan(self, data: bytes) -> str | None:
            return None

    class _Extractor:
        def extract(self, cv_text: str) -> Any:
            raise AssertionError("not used by privacy commands")

    return orchestrator.Deps(
        whatsapp_client=wa, scanner=_Scanner(),
        storage=FilesystemStorageAdapter(tempfile.mkdtemp()),
        extractor=_Extractor(),
    )


# ── the way back from «إيقاف» (closure audit, 31 July) ──────────────────────


def test_stopping_messages_is_not_a_life_sentence(owner_session, clean_billing):
    from career.whatsapp.worker import _handle_message

    """Nothing in the codebase ever cleared opt_out_at. A customer who typed
    «إلغاء الاشتراك» was silenced permanently while their subscription — and
    their billing — carried on, and the confirmation told them to send «دعم»,
    which does not clear it either."""
    from sqlalchemy import text as _sql

    wa = FakeWhatsAppClient()
    token = _provision_token(owner_session)
    phone = _phone()
    deps = _worker_deps(wa)
    admin = FakeTelegramAdminClient()

    def handle(body: str) -> None:
        _handle_message(owner_session,
                        _text_msg(f"wamid-{uuid.uuid4()}", phone, body),
                        whatsapp_client=wa, admin_client=admin, now=NOW,
                        onboarding=deps)

    handle(f"تفعيل {token}")
    handle("إلغاء الاشتراك")
    owner_session.commit()
    assert owner_session.execute(_sql(
        "SELECT opt_out_at IS NOT NULL FROM customer_channels WHERE"
        " phone_e164 = :p"), {"p": phone}).scalar_one() is True
    # the confirmation must name a word that actually works
    stop_reply = [m.body for m in wa.sent if m.kind == "text"][-1]
    assert "تشغيل الرسائل" in (stop_reply or "")
    # and the operator hears about it — this may be a billing cancellation
    assert any("أوقف الرسائل" in m for m in admin.messages)

    before = len(wa.sent)
    handle("تشغيل الرسائل")
    owner_session.commit()

    assert owner_session.execute(_sql(
        "SELECT opt_out_at IS NULL FROM customer_channels WHERE"
        " phone_e164 = :p"), {"p": phone}).scalar_one() is True
    assert len(wa.sent) > before          # they are answered, not ignored


def test_resume_does_not_swallow_the_privacy_command(owner_session, clean_billing):
    from career.whatsapp.worker import _handle_message

    """«استئناف» is also the standing command that un-pauses a SUBSCRIPTION.
    A customer who is not silenced must still reach it."""
    from career.whatsapp.inbound import InboundKind, classify_inbound

    kind, _ = classify_inbound("استئناف")
    assert kind is InboundKind.RESUME     # classified…

    wa = FakeWhatsAppClient()
    token = _provision_token(owner_session)
    phone = _phone()
    deps = _worker_deps(wa)
    _handle_message(owner_session,
                    _text_msg(f"wamid-{uuid.uuid4()}", phone, f"تفعيل {token}"),
                    whatsapp_client=wa, admin_client=FakeTelegramAdminClient(),
                    now=NOW, onboarding=deps)
    before = len(wa.sent)
    _handle_message(owner_session,
                    _text_msg(f"wamid-{uuid.uuid4()}", phone, "استئناف"),
                    whatsapp_client=wa, admin_client=FakeTelegramAdminClient(),
                    now=NOW, onboarding=deps)
    owner_session.commit()

    # …but NOT consumed by the resume branch when nothing was silenced
    assert len(wa.sent) > before


# ── a non-text message is never silence, and never a fact ───────────────────
# Theme: a paying customer must never be met with silence or with a lie.
# A voice note is the most natural reply in Saudi WhatsApp; it used to be
# routed into the conversation as "" — discarding the answer, committing a
# BLANK «customer-confirmed» row into the achievement bank (§15.5) — or, once
# ACTIVE, to produce no outbound at all.


def _media_msg(phone: str, mtype: str, body: Any = None) -> dict[str, Any]:
    """A realistic Meta media payload: the type carries no readable text."""
    return {
        "id": f"wamid.{uuid.uuid4().hex}", "from": phone, "type": mtype,
        mtype: body if body is not None else {"id": f"media-{uuid.uuid4().hex}"},
    }


def _start_journey(owner_session: Session, wa: Any, deps: Any) -> str:
    """Activate a real pass through the worker so the journey row exists."""
    from career.whatsapp.worker import _handle_message

    token = _provision_token(owner_session)
    phone = _phone()
    _handle_message(owner_session, _text_msg(f"wamid-{uuid.uuid4()}", phone,
                                             f"تفعيل {token}"),
                    whatsapp_client=wa, admin_client=FakeTelegramAdminClient(),
                    now=NOW, onboarding=deps)
    owner_session.commit()
    return phone


def _set_journey(owner_session: Session, phone: str, state: str,
                 context: str = "{}") -> None:
    owner_session.execute(text(
        "UPDATE onboarding_sessions o SET state = :s, context = CAST(:c AS jsonb)"
        " FROM customer_channels c WHERE c.tenant_id = o.tenant_id"
        " AND c.phone_e164 = :p"),
        {"s": state, "c": context, "p": phone})
    owner_session.commit()


def _set_subscription(owner_session: Session, phone: str, *,
                      plan_code: str | None = None,
                      status: str | None = None) -> None:
    if plan_code is not None:
        owner_session.execute(text(
            "UPDATE subscriptions s SET plan_code = :v FROM customer_channels c"
            " WHERE c.tenant_id = s.tenant_id AND c.phone_e164 = :p"),
            {"v": plan_code, "p": phone})
    if status is not None:
        owner_session.execute(text(
            "UPDATE subscriptions s SET status = :v FROM customer_channels c"
            " WHERE c.tenant_id = s.tenant_id AND c.phone_e164 = :p"),
            {"v": status, "p": phone})
    owner_session.commit()


def _fact_rows(owner_session: Session, phone: str) -> list[Any]:
    return list(owner_session.execute(text(
        "SELECT f.category, f.payload::text, f.status, f.source"
        " FROM profile_facts f JOIN customer_channels c"
        " ON c.tenant_id = f.tenant_id WHERE c.phone_e164 = :p"), {"p": phone}))


def _deliver(owner_session: Session, phone: str, msg: dict[str, Any],
             wa: Any, admin: Any, deps: Any = None) -> None:
    from career.whatsapp.worker import _handle_message

    _handle_message(owner_session, msg, whatsapp_client=wa,
                    admin_client=admin, now=NOW, onboarding=deps)
    owner_session.commit()


def test_a_voice_note_never_becomes_a_confirmed_fact(
    owner_session: Session, clean_billing: None
) -> None:
    """§15.5: nothing enters the achievement bank without an explicit human
    confirmation. Mid-confirmation the gap answer was taken from the message
    BODY — and a voice note / sticker has none, so an empty
    CUSTOMER_CONFIRMED experience was committed, the customer's real answer
    was discarded, and the mandatory question was never asked again."""
    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    admin = FakeTelegramAdminClient()
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "PROFILE_CONFIRMATION",
                 '{"awaiting_gap": "experience"}')

    before = len(wa.sent)
    for mtype in ("audio", "sticker", "image"):
        _deliver(owner_session, phone, _media_msg(phone, mtype), wa, admin, deps)

    assert _fact_rows(owner_session, phone) == [], \
        "a message with no words became a customer-confirmed fact"
    still = owner_session.execute(text(
        "SELECT o.context ->> 'awaiting_gap' FROM onboarding_sessions o"
        " JOIN customer_channels c ON c.tenant_id = o.tenant_id"
        " WHERE c.phone_e164 = :p"), {"p": phone}).scalar_one()
    assert still == "experience", "the question was marked answered by silence"
    replies = [m.body or "" for m in wa.sent[before:]]
    assert len(replies) == 3, "the customer was met with silence"
    assert all("ما أقدر أقرأ إلا النص المكتوب" in r for r in replies)
    assert "وصلتني رسالتك الصوتية" in replies[0]      # grammatical per type
    assert "وصلني الملصق" in replies[1]
    assert "وصلتني الصورة" in replies[2]


def test_a_photo_of_a_cv_is_answered_with_the_real_next_step(
    owner_session: Session, clean_billing: None
) -> None:
    """Photographing the CV is what people actually do. We cannot read it —
    say so, and name the two formats we can."""
    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "CV_UPLOAD_PENDING")

    before = len(wa.sent)
    _deliver(owner_session, phone, _media_msg(phone, "image"), wa,
             FakeTelegramAdminClient(), deps)
    reply = (wa.sent[-1].body or "")
    assert len(wa.sent) > before
    assert "أحتاجها ملفًا مرفقًا" in reply
    assert "PDF" in reply and "DOCX" in reply


def test_every_non_text_shape_from_an_active_customer_is_answered(
    owner_session: Session, clean_billing: None
) -> None:
    """The closure audit's fix covered TEXT only: image, voice, sticker,
    location, video, contacts, reaction and Meta's `unsupported` all produced
    zero outbound for a paying ACTIVE customer, indefinitely."""
    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE")
    _set_subscription(owner_session, phone, status="ACTIVE")

    shapes = ["image", "audio", "sticker", "location", "video", "contacts",
              "reaction", "unsupported"]
    for mtype in shapes:
        before = len(wa.sent)
        _deliver(owner_session, phone, _media_msg(phone, mtype), wa,
                 FakeTelegramAdminClient(), deps)
        assert len(wa.sent) > before, f"{mtype} from a paying customer: silence"
        body = wa.sent[-1].body or ""
        assert "وصل" in body and "ما أقدر أقرأ إلا النص المكتوب" in body
        assert "دعم" in body                     # the way to a human, always


def test_a_document_from_an_active_customer_is_not_swallowed(
    owner_session: Session, clean_billing: None
) -> None:
    """An ACTIVE customer sending a file is sending an updated CV. It was
    recorded as 'other' and dropped: every later CV kept being built from the
    stale profile, and nobody — customer or operator — was told."""
    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    admin = FakeTelegramAdminClient()
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE")
    _set_subscription(owner_session, phone, status="ACTIVE")
    admin.messages.clear()

    before = len(wa.sent)
    _deliver(owner_session, phone,
             _media_msg(phone, "document",
                        {"id": "media-1", "filename": "cv.pdf"}),
             wa, admin, deps)
    reply = wa.sent[-1].body or ""
    assert len(wa.sent) > before
    assert "ما دخل شي على بياناتك" in reply       # no invented capability
    assert "دعم" in reply
    assert len(admin.messages) == 1               # the operator hears the intent
    assert "TEN-" in admin.messages[0]
    assert phone.lstrip("+") not in admin.messages[0]   # §15.13 no PII


# ── the fallback must tell each customer the truth ──────────────────────────

_DAILY_PROMISE = "أنا معك يوميًا"


def _fallback_for(owner_session: Session, phone: str, wa: Any, deps: Any) -> str:
    before = len(wa.sent)
    _deliver(owner_session, phone,
             _text_msg(f"wamid-{uuid.uuid4()}", phone, "شكرًا، وش الخطوة الجاية؟"),
             wa, FakeTelegramAdminClient(), deps)
    assert len(wa.sent) > before, "no reply at all"
    return wa.sent[-1].body or ""


def test_the_daily_promise_survives_for_a_live_pass(
    owner_session: Session, clean_billing: None
) -> None:
    """Guard against over-correcting: a real ACTIVE pass IS on the daily
    service (engine/families.py) and must keep hearing so."""
    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE")
    _set_subscription(owner_session, phone, status="ACTIVE")
    assert _DAILY_PROMISE in _fallback_for(owner_session, phone, wa, deps)


def test_the_fallback_never_promises_a_daily_service_to_a_funnel_customer(
    owner_session: Session, clean_billing: None
) -> None:
    """cv_analysis is a one-shot 29-riyal report — daily_job_limit = 0 by
    design, excluded from tonight's families. «أنا معك يوميًا» right after
    their report reads as an enrolment they never bought, and they wait every
    morning for a delivery that can never come."""
    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE")
    _set_subscription(owner_session, phone, plan_code="cv_analysis",
                      status="ONBOARDING")
    reply = _fallback_for(owner_session, phone, wa, deps)
    assert _DAILY_PROMISE not in reply
    assert "ما راح توصلك فرص كل صباح" in reply
    assert "تحليل السيرة الذاتية" in reply
    assert "دعم" in reply


def test_the_fallback_tells_a_paused_customer_they_are_paused(
    owner_session: Session, clean_billing: None
) -> None:
    """A paused customer was promised the morning delivery their own pause
    stopped — and offered «وقف مؤقت» a second time, with no way back named."""
    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE")
    _set_subscription(owner_session, phone, status="PAUSED")
    reply = _fallback_for(owner_session, phone, wa, deps)
    assert _DAILY_PROMISE not in reply
    assert "موقوف مؤقتًا" in reply
    assert "استئناف" in reply                     # the way back, named
    assert "وقف مؤقت" not in reply                # never offered twice


def test_the_fallback_tells_an_expired_customer_to_renew(
    owner_session: Session, clean_billing: None
) -> None:
    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE")
    _set_subscription(owner_session, phone, status="EXPIRED")
    reply = _fallback_for(owner_session, phone, wa, deps)
    assert _DAILY_PROMISE not in reply
    assert "غير فعّال حاليًا" in reply
    assert "حالة اشتراكي" in reply                # carries the renewal link


def test_every_customer_reply_is_direction_pure() -> None:
    """§16 / Fahad's client: a line mixing Arabic with Latin letters, digits
    or a URL is scrambled on delivery. Every reply the worker can send — and
    every media acknowledgement — is checked line by line."""
    import re

    from career.whatsapp import inbound as wa_inbound
    from career.whatsapp import worker as wa_worker

    arabic = re.compile(r"[؀-ۿ]")
    latin = re.compile(r"[A-Za-z0-9]")

    bodies: list[tuple[str, str]] = []
    for module in (wa_worker, wa_inbound):
        for name, value in vars(module).items():
            if name.startswith("__") or not name.isupper() and not name.startswith("_"):
                continue
            if isinstance(value, str):
                bodies.append((name, value))
            elif isinstance(value, dict):
                bodies.extend(
                    (name, v) for v in value.values() if isinstance(v, str)
                )
    assert bodies
    for name, body in bodies:
        for line in body.splitlines():
            if arabic.search(line) and latin.search(line):
                raise AssertionError(
                    f"{name}: mixed-direction line would scramble: {line!r}"
                )


# ── a lost opt-out is worse than a duplicate confirmation (AUDIT, 5 Aug) ─────
# Theme: every branch below writes a FACT and then talks about it. The talking
# used to come first, unwrapped, inside the same transaction — so a 429 or a
# 5xx from Meta took the fact down with it and left the customer believing
# something that was no longer true.


class _MetaRefusing(FakeWhatsAppClient):
    """Meta answering 429/5xx on the send. Everything else stays real."""

    def send_text(self, to_phone: str, body: str) -> str:
        from career.whatsapp.client import WhatsAppSendError

        raise WhatsAppSendError("429")


class _TelegramRefusing(FakeTelegramAdminClient):
    """The operator channel down — never the customer's problem."""

    def send_admin(self, body: str) -> None:
        from career.telegram.admin import TelegramSendError

        raise TelegramSendError("500")


def test_an_opt_out_is_never_lost_to_a_failed_confirmation(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """Opt-out is a compliance surface. The STOP branch set opt_out_at, then
    sent «تم إيقاف الرسائل» UNWRAPPED before committing: one 429 from Meta
    unwound the whole turn, opt_out_at went back to NULL, and the customer who
    had just been told we would never write again received the next morning's
    delivery. Losing one opt-out is worse than sending two confirmations."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    _insert_event(owner_session, _payload(
        [_text_msg(f"wamid-{uuid.uuid4()}", phone, "إيقاف الرسائل")]))

    counts = _run(owner_session, _MetaRefusing(), admin)

    assert counts["failed"] == 0          # a failed courtesy is not a failure
    with Session(owner_engine) as s:
        opt_out = s.execute(text(
            "SELECT opt_out_at FROM customer_channels WHERE phone_e164 = :p"),
            {"p": phone}).scalar_one()
    assert opt_out is not None, "the opt-out was rolled back by a failed send"


def test_a_support_ticket_survives_a_deaf_operator_channel(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """«دعم» is the escape hatch printed in every error message. The Telegram
    page ran first and unwrapped, so a Telegram blip discarded the
    support_events row along with the inbound: the customer who asked for a
    human got no ticket, no ack, and no operator."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    _insert_event(owner_session, _payload(
        [_text_msg(f"wamid-{uuid.uuid4()}", phone, "دعم")]))

    counts = _run(owner_session, wa, _TelegramRefusing())

    assert counts["failed"] == 0
    with Session(owner_engine) as s:
        tickets = s.execute(text(
            "SELECT count(*) FROM support_events se JOIN customer_channels c"
            " ON c.id = se.channel_id WHERE c.phone_e164 = :p"),
            {"p": phone}).scalar_one()
    assert tickets == 1
    assert "الفريق" in (wa.sent[-1].body or "")   # and they were answered


def test_an_outcome_tap_is_recorded_before_it_is_thanked(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """§20's answer is a datum that cannot be collected twice — the card is
    answered, so the question never comes back. The thank-you used to be sent
    before the commit, so a Meta hiccup rolled the recorded tap back and the
    inbound row with it, spending the customer's tap on nothing."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _insert_event(owner_session, _payload(
        [_text_msg(wamid, phone, "oc_interview")]))

    counts = _run(owner_session, _MetaRefusing(), admin)

    assert counts["failed"] == 0
    with Session(owner_engine) as s:
        recorded = s.execute(text(
            "SELECT count(*) FROM inbound_messages WHERE wa_message_id = :w"),
            {"w": wamid}).scalar_one()
    assert recorded == 1, "the tap was rolled back by a failed thank-you"


# ── delivery receipts only ever move forwards (AUDIT, 5 Aug) ────────────────


LATER = NOW + timedelta(hours=2)


def _seed_outbound(owner_session: Session, phone: str, wamid: str, *,
                   status: str = "sent", template: str | None = None,
                   kind: str = "template") -> uuid.UUID:
    from career.whatsapp.delivery import record_out

    channel = owner_session.execute(select(CustomerChannel).where(
        CustomerChannel.phone_e164 == phone)).scalar_one()
    record_out(owner_session, tenant_id=channel.tenant_id,
               channel_id=channel.id, kind=kind, wa_message_id=wamid,
               template_name=template, now=NOW, status=status)
    owner_session.commit()
    return channel.tenant_id


def _receipt(owner_session: Session, wamid: str, status: str, *,
             now: datetime = NOW) -> None:
    _insert_event(owner_session, _payload(statuses=[
        {"id": wamid, "status": status, "recipient_id": "966500000000"},
    ]))
    _run(owner_session, FakeWhatsAppClient(), FakeTelegramAdminClient(), now=now)


def _receipt_row(owner_session: Session, wamid: str) -> Any:
    return owner_session.execute(text(
        "SELECT status, status_updated_at FROM delivery_messages"
        " WHERE wa_message_id = :w"), {"w": wamid}).one()


def test_a_late_sent_never_unreads_a_read(
    owner_session: Session, clean_billing: None
) -> None:
    """Meta does not guarantee receipt ORDER — a retried «sent» after a «read»
    is routine — and the handler wrote whatever arrived last, walking the
    delivery backwards and dating it now."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid)

    _receipt(owner_session, wamid, "read", now=NOW)
    _receipt(owner_session, wamid, "sent", now=LATER)

    row = _receipt_row(owner_session, wamid)
    assert row.status == "read"
    assert row.status_updated_at == NOW      # the receipt that WON dates it


def test_a_duplicate_receipt_changes_nothing(
    owner_session: Session, clean_billing: None
) -> None:
    """The same receipt twice (Meta redelivers freely) must not make a row
    look freshly updated — the stamp is evidence the operator reads."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid)

    _receipt(owner_session, wamid, "delivered", now=NOW)
    _receipt(owner_session, wamid, "delivered", now=LATER)

    row = _receipt_row(owner_session, wamid)
    assert row.status == "delivered"
    assert row.status_updated_at == NOW


def test_a_failed_message_never_becomes_billable_again(
    owner_session: Session, clean_billing: None
) -> None:
    """The rung with money on it. `cv/close.whatsapp_spend` bills every
    template whose status is not «failed», so a stale «sent» arriving after
    the failure flipped an undelivered message back into a billed one — and
    told the operator it had been sent. A failure outranks a `sent`; nothing
    weaker than it may overwrite it."""
    from career.cv import close as cv_close

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    tenant_id = _seed_outbound(owner_session, phone, wamid,
                               template=DAILY_UTILITY.name)

    _receipt(owner_session, wamid, "failed", now=NOW)
    assert _receipt_row(owner_session, wamid).status == "failed"
    assert cv_close.whatsapp_spend(owner_session, tenant_id=tenant_id) == {}

    _receipt(owner_session, wamid, "sent", now=LATER)      # the stale racer

    row = _receipt_row(owner_session, wamid)
    assert row.status == "failed"
    assert row.status_updated_at == NOW
    assert cv_close.whatsapp_spend(owner_session, tenant_id=tenant_id) == {}


def test_proof_of_receipt_outranks_a_failure_that_cannot_have_happened(
    owner_session: Session, clean_billing: None
) -> None:
    """The deliberate half of the ladder. `failed` is not last: a send can
    only fail while it is merely sent, so a `read` for this exact
    wa_message_id is positive proof the failure belonged to another attempt —
    and a message the customer demonstrably opened is not an undelivered
    one."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid)

    _receipt(owner_session, wamid, "failed", now=NOW)
    _receipt(owner_session, wamid, "read", now=LATER)

    row = _receipt_row(owner_session, wamid)
    assert row.status == "read"
    assert row.status_updated_at == LATER


def test_an_unknown_receipt_status_is_refused_loudly_not_quietly(
    owner_session: Session, clean_billing: None, caplog: Any
) -> None:
    """Two decisions pinned at once.

    NOT WRITTEN — and that is deliberate. The column feeds money
    (`cv/close.whatsapp_spend` bills everything whose status is not «failed»)
    and the operator's message-status panel, so a rung nobody has placed would
    be billed and displayed on the strength of a name we have never seen.

    LOUDLY — and that is the fix. It was a `logger.warning`, and warnings do
    not leave this box: the operator's harvester forwards «ERROR:» lines only.
    A status Meta adds that we silently never record is a permanent blind spot
    in the delivery ledger, and a warning nobody reads is how it stays one.
    """
    from career.whatsapp import worker as wa_worker

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid)

    assert wa_worker.receipt_rank("deleted") == 0
    with caplog.at_level("ERROR"):
        _receipt(owner_session, wamid, "deleted", now=LATER)

    assert _receipt_row(owner_session, wamid).status == "sent"
    assert any("deleted" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


# ── one receipt ladder, imported (scripts/replay_lost_events.py) ────────────


def _replay_tool() -> Any:
    """The recovery script, loaded as a module. It must be registered in
    ``sys.modules`` before execution — its ``@dataclass`` resolves annotations
    through ``sys.modules[cls.__module__]`` and raises without it."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "replay_lost_events.py"
    spec = importlib.util.spec_from_file_location("career_replay_lost", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["career_replay_lost"] = module
    spec.loader.exec_module(module)
    return module


def test_the_replay_tool_and_the_worker_can_never_rank_receipts_differently(
) -> None:
    """The guard against a second ladder appearing.

    Both files shipped one, in the same commit, each with a docstring arguing
    its order was deliberate — and they disagreed on the comparison that
    matters: the worker put `failed` between `sent` and `delivered`, the tool
    put it on top. This walks every ordered pair of statuses either file knows
    and asserts the two answer «is this receipt a forward move» identically,
    so re-introducing a local table fails here rather than in production.
    """
    from career.whatsapp import worker as wa_worker

    tool = _replay_tool()
    statuses = ["", "queued", "accepted", "sent", "failed", "delivered",
                "read", "deleted"]
    for current in statuses:
        for incoming in statuses:
            assert (tool._rank(incoming) > tool._rank(current)) == (
                wa_worker.receipt_rank(incoming)
                > wa_worker.receipt_rank(current)
            ), f"{current!r} → {incoming!r}"


def test_the_tool_recovers_the_receipt_class_it_used_to_refuse(
    owner_session: Session, clean_billing: None
) -> None:
    """The harm the second ladder did. With `failed` ranked top, a `read`
    destroyed while the row sat `failed` was «already at or past this receipt»
    — so the tool skipped the one recovery worth making, the row stayed
    failed, `whatsapp_spend` went on excluding a message the customer had
    read, and the console showed the operator a failure for a message that had
    arrived."""
    from career.db.models import WebhookEvent as _WebhookEvent

    tool = _replay_tool()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid)
    _receipt(owner_session, wamid, "failed", now=NOW)
    assert _receipt_row(owner_session, wamid).status == "failed"

    lost = _WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="statuses",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload=_payload(statuses=[{"id": wamid, "status": "read"}]),
        processing_status="ignored", received_at=NOW,
    )
    verdict = tool._verdict(owner_session, lost, max_age_days=2, now=LATER)
    assert verdict.action == "replay"

    # and the mirror: a stale «sent» over a «failed» is NOT a forward move,
    # so re-queuing it would spend the tool's one-shot ledger entry on an
    # event the worker then no-ops — a false report the ledger makes permanent
    stale = _WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="statuses",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload=_payload(statuses=[{"id": wamid, "status": "sent"}]),
        processing_status="ignored", received_at=NOW,
    )
    assert tool._verdict(
        owner_session, stale, max_age_days=2, now=LATER).action == "skip"


def test_a_rolled_back_event_is_never_replayed_at_a_customer(
    owner_session: Session, clean_billing: None
) -> None:
    """The default selection is «ignored,failed», and the two are not alike.

    ``ignored`` is a row the Salla sweep marked terminal without any WhatsApp
    code touching it: nothing sent, nothing written, safe to replay.
    ``failed`` is written by the worker AFTER ``rollback()`` — its outbound
    HTTP already happened while the ``inbound_messages`` row that proves it
    did not survive. The tool's own safety argument («idempotent per
    wa_message_id») therefore reads that message as never-handled and replays
    it, and the customer receives it a second time.
    """
    from career.db.models import WebhookEvent as _WebhookEvent

    tool = _replay_tool()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)

    def _event(status: str) -> Any:
        return _WebhookEvent(
            id=uuid.uuid4(), provider="whatsapp", event_type="messages",
            event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
            payload=_payload([_text_msg(f"wamid-{uuid.uuid4()}", phone, "دعم")]),
            processing_status=status, received_at=NOW,
        )

    rolled_back = tool._verdict(
        owner_session, _event(tool.ROLLED_BACK_STATUS),
        max_age_days=2, now=NOW,
    )
    assert rolled_back.action == "skip"
    assert "twice" in rolled_back.reason

    # the class the tool exists for is untouched: never handled, so replay it
    untouched = tool._verdict(
        owner_session, _event("ignored"), max_age_days=2, now=NOW)
    assert untouched.action == "replay"


def test_a_rolled_back_receipt_is_still_recoverable(
    owner_session: Session, clean_billing: None
) -> None:
    """The refusal is scoped to what can SPEAK. ``_handle_status`` sends
    nothing at all, so a `failed` statuses event is exactly the recovery the
    2026-08-03 incident needs and must not be caught by the new guard."""
    from career.db.models import WebhookEvent as _WebhookEvent

    tool = _replay_tool()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid)

    event = _WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="statuses",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload=_payload(statuses=[{"id": wamid, "status": "delivered"}]),
        processing_status=tool.ROLLED_BACK_STATUS, received_at=NOW,
    )
    assert tool._verdict(
        owner_session, event, max_age_days=2, now=NOW).action == "replay"


# ── the watchtower runner's cursor (scripts/run_admin_bot.py) ───────────────
# Placed here because that runner has no test module of its own and this
# change set owns both files; it belongs beside the console tests the day
# someone gives it one.


def _admin_runner() -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "run_admin_bot.py"
    spec = importlib.util.spec_from_file_location("career_run_admin_bot", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_poisoned_update_can_never_wedge_the_watchtower(
    owner_engine: Engine,
) -> None:
    """INCIDENT: the offset was stored AFTER handle_update, in its transaction.
    Anything that escaped left the cursor where it was, so getUpdates handed
    back the same update five seconds later, forever — the operator's only
    window into the system, closed by one tap until someone restarted the
    service. Worse than the wedge: a handler that sends before it fails
    repeated that send at a real customer's phone every five seconds.

    So the cursor moves FIRST and the failure is announced. The dropped update
    is the deliberate cost: every screen here is operator-initiated and
    re-tappable, and at-most-once on a control channel beats at-least-once
    with real customer sends behind it.
    """
    runner = _admin_runner()
    with Session(owner_engine) as s:
        before = runner._load_offset(s)
    seen_while_working: list[int] = []
    skips: list[str] = []

    def poisoned(session: Session, update: dict[str, Any]) -> list[Any]:
        with Session(owner_engine) as inner:
            seen_while_working.append(runner._load_offset(inner))
        raise RuntimeError("poisoned update")

    def healthy(session: Session, update: dict[str, Any]) -> list[Any]:
        return ["one screen"]

    try:
        outcomes = runner.process_one_update(
            owner_engine, {"update_id": before + 1}, update_id=before + 1,
            handler=poisoned, on_skip=lambda: skips.append("said so"),
        )
        assert outcomes == []                      # nothing drawn over a failure
        assert skips == ["said so"]                # and never silently
        assert seen_while_working == [before + 1]  # cursor had ALREADY moved
        with Session(owner_engine) as s:
            assert runner._load_offset(s) == before + 1

        # the healthy path still works, and still returns its screens
        assert runner.process_one_update(
            owner_engine, {"update_id": before + 2}, update_id=before + 2,
            handler=healthy, on_skip=lambda: skips.append("wrong"),
        ) == ["one screen"]
        assert skips == ["said so"]
    finally:
        with Session(owner_engine) as s:
            runner._store_offset(s, before)
            s.commit()


class _FlakyTelegram:
    """A client whose transport dies the way ``requests`` actually dies."""

    def __init__(self, *, fail_on: int = 0) -> None:
        self.sent: list[str] = []
        self.acked: list[str] = []
        self._n = 0
        self._fail_on = fail_on

    def _maybe_die(self) -> None:
        self._n += 1
        if self._n == self._fail_on:
            # requests.ConnectionError is an OSError subclass and is NOT a
            # TelegramSendError — which is the whole point
            raise ConnectionError("connection reset by peer")

    def send_screen(self, text: str, keyboard: Any = None,
                    force_reply: bool = False) -> None:
        self._maybe_die()
        self.sent.append(text)

    def edit_screen(self, message_id: int, text: str, keyboard: Any = None) -> None:
        self._maybe_die()
        self.sent.append(text)

    def answer_callback(self, cbq_id: str, text: str = "") -> None:
        self._maybe_die()
        self.acked.append(cbq_id)

    def send_admin(self, text: str) -> str:
        self._maybe_die()
        self.sent.append(text)
        return "ok"


def test_a_dead_socket_does_not_swallow_the_rest_of_the_screens(
    caplog: Any,
) -> None:
    """The outcome loop caught ``TelegramSendError`` only. That name covers a
    refusal Telegram ARTICULATED («message is not modified» on an unchanged
    refresh); it does not cover the transport underneath, and ``requests``
    raises ``ConnectionError``/``ReadTimeout`` on its own. One of those
    escaped the loop, aborted the poll cycle, and — because the work was
    already committed and the cursor already past it — the operator lost the
    screens for it with nothing anywhere saying so."""
    from career.telegram.console import Outcome

    runner = _admin_runner()
    client = _FlakyTelegram(fail_on=1)
    with caplog.at_level("ERROR"):
        runner.deliver_outcomes(client, [
            Outcome(kind="ack", callback_query_id="cbq1"),
            Outcome(kind="send", text="the screen that must still arrive"),
        ])
    assert client.sent == ["the screen that must still arrive"]
    assert any("outcome delivery failed" in r.getMessage()
               for r in caplog.records)


def test_the_skip_notice_survives_a_dead_socket_too() -> None:
    """The at-most-once bargain is «we drop updates, but never quietly», and
    this notice is the «never quietly» half. It caught ``TelegramSendError``
    only, so a network failure raised out of ``process_one_update``'s OWN
    except block: the announcement never happened AND the cycle died — in
    precisely the case the bargain was made for."""
    runner = _admin_runner()
    runner._skip_poisoned_update(_FlakyTelegram(fail_on=1))   # must not raise
