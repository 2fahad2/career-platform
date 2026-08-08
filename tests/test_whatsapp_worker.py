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


# ── transient failure vs poisoned event (0022) ──────────────────────────────
# Theme: `failed` was written on ANY exception and `failed` is terminal —
# nothing in this codebase re-selects it. One row from 2026-07-19 is still
# sitting in staging because Claude, or Meta, or the database, was briefly
# unavailable. A retryable failure now waits behind a clock; only an event we
# could look at and could not process, an exhausted budget, or an attempt that
# had already answered the customer is allowed to be terminal.


class _MetaRefusingWithStatus(FakeWhatsAppClient):
    """Meta answering 503: a COMPLETE HTTP answer, so the send was refused
    outright and the customer received nothing."""

    def send_text(self, to_phone: str, body: str) -> str:
        from career.whatsapp.client import WhatsAppSendError

        raise WhatsAppSendError("graph HTTP 503 (code 131026)")


class _MetaTimingOut(FakeWhatsAppClient):
    """The other shape entirely: the request never came back, so whether the
    customer got the message is unknowable — and unknowable is not retryable."""

    def send_text(self, to_phone: str, body: str) -> str:
        raise TimeoutError("read timed out")


class _RefusingAfterFirstSend(FakeWhatsAppClient):
    """The first send lands; every later one is refused with a status."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def send_text(self, to_phone: str, body: str) -> str:
        from career.whatsapp.client import WhatsAppSendError

        self.calls += 1
        if self.calls > 1:
            raise WhatsAppSendError("graph HTTP 503 (code 131026)")
        return super().send_text(to_phone, body)


def _wa_event(owner_session: Session, msgs: list[dict[str, Any]]) -> uuid.UUID:
    ev = WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="messages",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload=_payload(msgs), processing_status="received",
    )
    owner_session.add(ev)
    owner_session.commit()
    return ev.id


def _ev_row(owner_engine: Engine, ev_id: uuid.UUID) -> Any:
    with Session(owner_engine) as s:
        return s.execute(select(WebhookEvent).where(
            WebhookEvent.id == ev_id)).scalar_one()


def test_a_meta_5xx_leaves_the_message_alive_behind_a_clock(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The 2026-07-19 row, replayed. An unknown number gets the «أرسل رمز
    التفعيل» reply; Meta answers 503. Nothing about that event is wrong, and
    marking it `failed` — the status nothing re-selects — threw the customer's
    first message away over thirty seconds of Meta being unwell."""
    admin = FakeTelegramAdminClient()
    ev_id = _wa_event(owner_session, [
        _text_msg(f"wamid-{uuid.uuid4()}", _phone(), "السلام عليكم")])

    counts = _run(owner_session, _MetaRefusingWithStatus(), admin)

    assert counts == {"messages": 0, "statuses": 0, "failed": 0, "deferred": 1}
    row = _ev_row(owner_engine, ev_id)
    assert row.processing_status == "received"   # still the worker's to do
    assert row.attempt_count == 1
    assert row.failure_kind == "transient"
    assert row.failure_detail == "career.WhatsAppSendError"
    assert row.next_attempt_at == NOW + timedelta(seconds=30)
    assert row.processed_at is None              # because it was not
    assert admin.messages == []                 # a blip is not an incident


def test_a_deferred_event_is_invisible_until_its_clock_runs_out(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The clock is the whole difference between a retry and a retry storm:
    the loop polls every three seconds, so an event left «received» with no
    next-attempt time comes straight back to the front of the queue and is
    tried twenty times a minute — which is exactly why `failed` was made
    terminal in the first place."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    ev_id = _wa_event(owner_session, [
        _text_msg(f"wamid-{uuid.uuid4()}", _phone(), "السلام عليكم")])
    _run(owner_session, _MetaRefusingWithStatus(), admin)

    early = _run(owner_session, wa, admin, now=NOW + timedelta(seconds=29))
    assert early == {"messages": 0, "statuses": 0, "failed": 0, "deferred": 0}
    assert wa.sent == []                         # not even looked at

    late = _run(owner_session, wa, admin, now=NOW + timedelta(seconds=31))
    assert late["messages"] == 1
    row = _ev_row(owner_engine, ev_id)
    assert row.processing_status == "processed"
    assert row.attempt_count == 2
    assert row.next_attempt_at is None
    assert len(wa.sent) == 1                     # the reply it owed, once


def test_a_retry_never_repeats_what_the_customer_may_already_have(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """THE constraint. A timeout is retryable weather by every rule in
    `_is_transient`, and it is still not retried here: the request never came
    back, so the reply may well have landed, and the retry would send it a
    second time. Refused, named, and handed to the operator instead."""
    admin = FakeTelegramAdminClient()
    phone = _phone()
    ev_id = _wa_event(owner_session, [
        _text_msg(f"wamid-{uuid.uuid4()}", phone, "السلام عليكم")])

    counts = _run(owner_session, _MetaTimingOut(), admin)

    assert counts["failed"] == 1 and counts["deferred"] == 0
    row = _ev_row(owner_engine, ev_id)
    assert row.processing_status == "failed"
    assert row.failure_kind == "already_spoke"
    assert row.failure_detail == "builtins.TimeoutError"
    assert row.next_attempt_at is None
    # the operator hears about it, by TEN code, never by phone (§15.13)
    assert len(admin.messages) == 1
    assert phone not in admin.messages[0]
    assert phone.lstrip("+") not in admin.messages[0]


def test_a_reply_that_recorded_nothing_makes_the_whole_event_unsafe(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The sticky half of the ledger. The «أرسل رمز التفعيل» reply to an
    unknown number writes no inbound row, so there is nothing for a retry to
    skip on: once one message in an event has answered a customer off the
    record, a later failure cannot be retried either, however cleanly it
    failed."""
    admin = FakeTelegramAdminClient()
    ev_id = _wa_event(owner_session, [
        _text_msg(f"wamid-{uuid.uuid4()}", _phone(), "السلام عليكم"),
        _text_msg(f"wamid-{uuid.uuid4()}", _phone(), "وعليكم السلام"),
    ])

    counts = _run(owner_session, _RefusingAfterFirstSend(), admin)

    assert counts["failed"] == 1
    row = _ev_row(owner_engine, ev_id)
    assert row.processing_status == "failed"
    assert row.failure_kind == "already_spoke"


def test_a_recorded_sibling_does_not_block_the_retry(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """And the mirror, which is what makes the rule usable at all. The STOP in
    the same event committed its inbound row BEFORE it spoke, so the retry
    skips it (`_inbound_exists`) and cannot repeat its confirmation — the
    second message is therefore free to be deferred like any other."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    ev_id = _wa_event(owner_session, [
        _text_msg(f"wamid-{uuid.uuid4()}", phone, "إيقاف الرسائل"),
        _text_msg(f"wamid-{uuid.uuid4()}", _phone(), "السلام عليكم"),
    ])

    counts = _run(owner_session, _RefusingAfterFirstSend(), admin)

    assert counts["deferred"] == 1 and counts["failed"] == 0
    row = _ev_row(owner_engine, ev_id)
    assert row.processing_status == "received"
    assert row.failure_kind == "transient"
    with Session(owner_engine) as s:
        opt_out = s.execute(text(
            "SELECT opt_out_at FROM customer_channels WHERE phone_e164 = :p"),
            {"p": phone}).scalar_one()
    assert opt_out is not None       # committed before the sibling failed


def test_the_attempt_budget_is_bounded(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """A retry that never gives up is a slower version of the retry storm: it
    holds a batch slot forever and, on the conversation paths, pays for a
    Claude call every time round. Five attempts across roughly forty minutes,
    then it stops and says so."""
    from career.whatsapp.worker import _BACKOFF_SECONDS, MAX_ATTEMPTS

    admin = FakeTelegramAdminClient()
    ev_id = _wa_event(owner_session, [
        _text_msg(f"wamid-{uuid.uuid4()}", _phone(), "السلام عليكم")])

    at = NOW
    for attempt in range(1, MAX_ATTEMPTS + 1):
        counts = _run(owner_session, _MetaRefusingWithStatus(), admin, now=at)
        row = _ev_row(owner_engine, ev_id)
        assert row.attempt_count == attempt
        if attempt < MAX_ATTEMPTS:
            assert counts["deferred"] == 1
            assert row.processing_status == "received"
            at = row.next_attempt_at
        else:
            assert counts["failed"] == 1
            assert row.processing_status == "failed"
            assert row.failure_kind == "exhausted"
            assert row.next_attempt_at is None
    assert at == NOW + timedelta(seconds=sum(_BACKOFF_SECONDS))
    assert len(admin.messages) == 1  # one notice at the end, not five


def test_a_poisoned_event_is_terminal_on_its_first_attempt(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """Nothing about the budget applies to an event we can read and cannot
    process: `text: None` crashes `_text_of` the same way five times, and
    spending forty minutes proving it would only delay the operator's notice."""
    admin = FakeTelegramAdminClient()
    ev_id = _wa_event(owner_session, [
        {"id": f"wamid-{uuid.uuid4()}", "from": _phone(),
         "type": "text", "text": None}])

    counts = _run(owner_session, FakeWhatsAppClient(), admin)

    assert counts["failed"] == 1
    row = _ev_row(owner_engine, ev_id)
    assert row.processing_status == "failed"
    assert row.failure_kind == "poison"
    assert row.failure_detail == "builtins.AttributeError"
    assert row.attempt_count == 1
    assert len(admin.messages) == 1


def test_the_dead_letter_can_be_listed_not_just_logged(
    owner_session: Session, clean_billing: None
) -> None:
    """A status nobody reads is what the 2026-07-19 row proves, so the reasons
    are countable from the database and not only from journalctl."""
    from career.whatsapp.worker import dead_letter_summary

    before = dead_letter_summary(owner_session)
    _wa_event(owner_session, [
        {"id": f"wamid-{uuid.uuid4()}", "from": _phone(),
         "type": "text", "text": None}])
    _run(owner_session, FakeWhatsAppClient(), FakeTelegramAdminClient())

    after = dead_letter_summary(owner_session)
    assert after["poison"] == before.get("poison", 0) + 1
    assert after["total"] == before["total"] + 1


def test_the_classifier_defaults_to_the_side_that_cannot_double_send() -> None:
    """Salla's default is «retry, never destroy a paid order»; this worker's
    is the opposite, and the asymmetry is the argument. A Salla retry re-reads
    an API and writes idempotently — it cannot embarrass anyone. A WhatsApp
    retry re-runs a CONVERSATION, so an exception nobody has thought about
    must not be handed a licence to answer a customer twice."""
    from sqlalchemy.exc import IntegrityError, OperationalError

    from career.salla.client import SallaApiError
    from career.whatsapp.client import WhatsAppSendError
    from career.whatsapp.worker import _is_transient

    class _Anthropicish(Exception):
        __module__ = "anthropic"
        status_code: int | None = None

    def _anthropic(status: int | None) -> Exception:
        exc = _Anthropicish("boom")
        exc.status_code = status
        return exc

    retryable: list[BaseException] = [
        WhatsAppSendError("graph HTTP 503 (code 131026)"),
        WhatsAppSendError("graph HTTP 429 (code 130429)"),
        _anthropic(None),                       # connection reset / timeout
        _anthropic(429),                        # the 06:00 fan-out's own risk
        _anthropic(529),
        OperationalError("SELECT 1", {}, Exception("server closed")),
        TimeoutError("read timed out"),
        ConnectionResetError("peer went away"),
        SallaApiError("token", retryable=True),
    ]
    terminal: list[BaseException] = [
        WhatsAppSendError("graph HTTP 400 (code 131047)"),
        WhatsAppSendError("no storage wired for document refs"),
        _anthropic(400),
        IntegrityError("INSERT", {}, Exception("duplicate key")),
        SallaApiError("bad order", retryable=False),
        AttributeError("'NoneType' object has no attribute 'get'"),
        KeyError("messages"),
        TypeError("unhashable"),
        ValueError("not a uuid"),
        RuntimeError("something new nobody has classified"),
    ]
    for exc in retryable:
        assert _is_transient(exc) is True, type(exc).__name__
    for exc in terminal:
        assert _is_transient(exc) is False, f"{type(exc).__name__}: {exc}"


# ── the لمّاح+ direct line: heard, and only when he actually spoke ───────────
# Theme of this block. The tier sells «تواصل مباشر معي — اكتب لي وقت ما تحتاج»,
# and the escalation that delivers it sits in the worker's LAST branch. Two
# opposite failures live one line apart there: a real message that reaches
# nobody, and a machine event that raises a ticket against a customer who said
# nothing. The dedupe is one open ticket per customer, so the second failure
# also SPENDS the first one's only slot.


def _plus_customer(owner_session: Session, wa: Any, deps: Any) -> str:
    """An ACTIVE لمّاح+ (449) customer, past onboarding — the tier that buys
    the direct line."""
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE")
    _set_subscription(owner_session, phone, plan_code="executive",
                      status="ACTIVE")
    return phone


def _direct_tickets(owner_session: Session, phone: str) -> list[Any]:
    return list(owner_session.execute(text(
        "SELECT e.id, e.inbound_message_id FROM support_events e"
        " JOIN customer_channels c ON c.id = e.channel_id"
        " WHERE c.phone_e164 = :p AND e.kind = 'executive_direct_message'"
        " AND e.status = 'open'"), {"p": phone}))


def _hold_a_bundle(owner_session: Session, phone: str, wa: Any) -> None:
    """Shut the 24h window and queue tonight's bundle behind it — the state
    every customer who went quiet for a day is in by morning."""
    ch = owner_session.execute(
        select(CustomerChannel).where(CustomerChannel.phone_e164 == phone)
    ).scalar_one()
    ch.last_inbound_at = NOW - timedelta(hours=25)
    owner_session.commit()
    deliver_adaptive(owner_session, ch, {"parts": [{"kind": "text",
                                                    "body": "#1 job"}]},
                     run_date=NOW.date(), whatsapp_client=wa,
                     daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()


def test_a_landed_bundle_never_swallows_the_customers_own_question(
    owner_session: Session, clean_billing: None
) -> None:
    """D1.1 — the likeliest message of all.

    `descend_pending_delivery` fires on ANY inbound while a bundle sits
    PENDING_WINDOW, and the first message a customer sends after a quiet day
    is exactly the one that lands it. The escalation was gated on
    `landed is None`, so a لمّاح+ customer asking a real question got a job
    bundle and reached nobody: no ticket, no page, no card.

    `landed` is a DELIVERY event. It says a held bundle went out; it says
    nothing whatever about what the customer wrote.
    """
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _plus_customer(owner_session, wa, deps)
    _hold_a_bundle(owner_session, phone, wa)
    admin.messages.clear()

    _deliver(owner_session, phone,
             _text_msg(f"wamid-{uuid.uuid4()}", phone,
                       "أبي رأيك في عرض وظيفي وصلني، أوافق ولا أنتظر؟"),
             wa, admin, deps)

    tickets = _direct_tickets(owner_session, phone)
    assert tickets, (
        "a لمّاح+ customer's real question reached NOBODY because a held "
        f"bundle happened to land on the same message — {tickets}"
    )
    # D1.5 — and the ticket points at the message that caused it, so the
    # operator's queue can tell a real sentence from a machine event.
    assert tickets[0][1] is not None, "the ticket links to no message"
    assert any("⭐" in m for m in admin.messages), "the operator was not paged"
    assert all(phone.lstrip("+") not in m for m in admin.messages)  # §15.13
    # and the customer is still answered: a bundle is tonight's scheduled
    # delivery, never a reply to a sentence somebody typed.
    assert "وصلتني رسالتك" in (wa.sent[-1].body or "")


def test_a_voice_note_from_a_plus_customer_reaches_a_human(
    owner_session: Session, clean_billing: None
) -> None:
    """D1.2 — a voice note is the commonest thing a Saudi customer sends.

    A document paged the operator; audio, image and video fell through to a
    media ack and reached nobody. The tier sells access, not a file format.
    The operator cannot hear it from Telegram, so the alert says so and sends
    him to the conversation instead of pretending there is a body to read.
    """
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _plus_customer(owner_session, wa, deps)
    admin.messages.clear()

    _deliver(owner_session, phone, _media_msg(phone, "audio"), wa, admin, deps)

    assert _direct_tickets(owner_session, phone), \
        "a لمّاح+ voice note reached nobody"
    pages = [m for m in admin.messages if "⭐" in m]
    assert pages, "the operator was not paged"
    assert "رسالة صوتية" in pages[0], "the alert does not say what he gets"
    assert "واتساب" in pages[0], "he is not told where to go and hear it"
    assert phone.lstrip("+") not in pages[0]
    # the customer still gets the honest media answer
    assert "ما أقدر أقرأ إلا النص المكتوب" in (wa.sent[-1].body or "")


def test_a_tap_on_a_stale_card_is_never_a_ticket(
    owner_session: Session, clean_billing: None
) -> None:
    """D1.3/D1.4 — WhatsApp keeps every card tappable forever.

    A tap on a months-old enrichment card («مضبوط ✅», id `enr_ok`) is a
    machine event from a card WE sent, not a customer writing to us. It used
    to open the ONE direct-line ticket the dedupe allows, so Tuesday's real
    question returned `already_open` and paged nobody.
    """
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _plus_customer(owner_session, wa, deps)
    admin.messages.clear()

    _deliver(owner_session, phone, {
        "id": f"wamid.{uuid.uuid4().hex}", "from": phone, "type": "interactive",
        "interactive": {"type": "button_reply",
                        "button_reply": {"id": "enr_ok", "title": "مضبوط ✅"}},
    }, wa, admin, deps)

    assert _direct_tickets(owner_session, phone) == [], \
        "a stale card tap opened the one ticket a real message needs"
    assert not [m for m in admin.messages if "⭐" in m]
    assert wa.sent, "the tap was met with silence"


def test_a_typed_outcome_label_is_the_answer_not_a_ticket(
    owner_session: Session, clean_billing: None
) -> None:
    """D1.3 — «ما ردّوا» typed instead of tapped.

    `outcome_followup.parse_answer` reads BUTTON IDS only, so a customer who
    types the label he can see fell all the way through to the direct line: a
    ticket raised against someone answering OUR question, and the §20 datum —
    which can never be collected twice — thrown away.
    """
    from career.cv import outcome_followup as followup

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _plus_customer(owner_session, wa, deps)
    owner_session.execute(text(
        "INSERT INTO outcome_events (id, tenant_id, job_ref, outcome,"
        " occurred_at) SELECT :i, c.tenant_id, 'JOB-1', :o, :t"
        " FROM customer_channels c WHERE c.phone_e164 = :p"),
        {"i": str(uuid.uuid4()), "o": followup.ASKED, "t": NOW, "p": phone})
    owner_session.commit()
    admin.messages.clear()

    _deliver(owner_session, phone,
             _text_msg(f"wamid-{uuid.uuid4()}", phone, "ما ردوا"),
             wa, admin, deps)

    recorded = owner_session.execute(text(
        "SELECT o.outcome FROM outcome_events o JOIN customer_channels c"
        " ON c.tenant_id = o.tenant_id WHERE c.phone_e164 = :p"
        " AND o.outcome = :v"), {"p": phone, "v": followup.NO_REPLY}).all()
    assert recorded, "the customer's own answer to our question was discarded"
    assert _direct_tickets(owner_session, phone) == [], \
        "answering our question raised a ticket against the customer"


def test_a_typed_label_with_no_question_open_is_still_the_customers_sentence(
    owner_session: Session, clean_billing: None
) -> None:
    """The other half of the typed reading, and the expensive half to get wrong.

    «اعتذروا» is a word. With one of our questions open it is an answer to it;
    with nothing open it is a sentence a human wrote, and consuming it would
    both lose his message and lie back to him («مسجّلة عندنا 👍» about a job he
    never mentioned). This module has paid for the over-eager reading twice
    already — «مساعده» read as a support command, «تسويق، دعم، مبيعات» read as
    one — so the narrow direction is asserted end to end, not only at the
    parser: nothing is recorded, and the message travels all the way to the
    direct line like any other sentence.
    """
    from career.cv import outcome_followup as followup

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _plus_customer(owner_session, wa, deps)   # no ASKED row anywhere
    admin.messages.clear()

    _deliver(owner_session, phone,
             _text_msg(f"wamid-{uuid.uuid4()}", phone, "اعتذروا"),
             wa, admin, deps)

    recorded = owner_session.execute(text(
        "SELECT o.outcome FROM outcome_events o JOIN customer_channels c"
        " ON c.tenant_id = o.tenant_id WHERE c.phone_e164 = :p"),
        {"p": phone}).all()
    assert recorded == [], \
        "a word was recorded as the answer to a question nobody asked"
    assert followup.ALREADY_ANSWERED_AR not in (wa.sent[-1].body or ""), \
        "the customer was told we had filed an answer he never gave"
    assert _direct_tickets(owner_session, phone), \
        "his sentence was eaten by the survey and reached nobody"


def test_the_worker_holds_no_second_reading_of_the_outcome_labels() -> None:
    """The guard against the workaround growing back.

    While `outcome_followup.parse_answer` read machine ids only, this file
    carried its own label map — derived from `BUTTONS`, folded with
    `normalize_ar`, inside the message loop — so that a customer who TYPED
    «ما ردّوا» was not answered with a support ticket. The parser reads both
    now, and two readings of one vocabulary is the shape
    `test_the_replay_tool_and_the_worker_can_never_rank_receipts_differently`
    already caught once in this module: not two opinions, one bug waiting for
    the day they disagree.

    So the worker is asserted to fold nothing and to know no labels. It routes
    by machine id (`_button_id_of`) and asks the module that owns the words.
    """
    import ast
    from pathlib import Path

    from career.whatsapp import worker as wa_worker

    assert not hasattr(wa_worker, "_typed_outcome"), (
        "the typed-label workaround is back in the worker — its permanent "
        "home is outcome_followup.parse_answer(question_open=...)"
    )
    source = Path(str(wa_worker.__file__)).read_text(encoding="utf-8")
    names = {
        node.attr for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
    } | {
        node.id for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name)
    }
    assert "BUTTONS" not in names, \
        "the worker reads the button labels again — one vocabulary, one reader"
    assert "normalize_ar" not in names, \
        "the worker folds Arabic itself again — folding belongs to the module "\
        "that owns the words being compared"


def test_an_enrichment_answer_is_never_a_ticket_when_the_renderer_is_unwired(
    owner_session: Session, clean_billing: None
) -> None:
    """D1.3 — the «structural guarantee» had a hole with no floor under it.

    `orchestrator.handle_enrichment` used to return False when
    `deps.achievement_renderer is None`, and `_mid_flow` never looked at the
    enrichment session at all (the journey is ACTIVE while it runs). So a
    process without a renderer let a genuine achievement answer fall through
    to the direct line, where it became a support ticket raised against the
    customer for answering us.

    An earlier version of this docstring called the unwired renderer «the
    SHIPPED default», and that was never true: `scripts/run_worker_loop.py` —
    the only process that serves customers — has passed
    `AnthropicAchievementRenderer` since the feature's own commit, and `None`
    is a dataclass default that only tests reach. The sentence was wrong,
    load-bearing in argument, and copied onward twice before anybody read the
    wiring; it is corrected rather than deleted so the correction travels too.
    The hole itself was real either way — the guard must not rest on which
    boundaries a caller happened to wire.

    `_mid_flow` is asserted BEFORE the message is processed, which is what it
    actually protects: the escalation is decided while the customer is
    mid-conversation. Afterwards he genuinely is not — `handle_enrichment` now
    OWNS the message it cannot render and closes the session rather than
    leaving a cursor open — so asserting it after processing would assert the
    bug the orchestrator was just fixed for.
    """
    from career.promises import career_session

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)                      # achievement_renderer is None
    assert deps.achievement_renderer is None
    phone = _plus_customer(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE",
                 '{"enrichment": {"open": true}}')
    admin.messages.clear()

    # the guard is in the promises module, not only in branch order
    tenant = owner_session.execute(text(
        "SELECT tenant_id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone}).scalar_one()
    assert career_session._mid_flow(owner_session, tenant) is True, \
        "an open enrichment session did not read as mid-flow"

    _deliver(owner_session, phone,
             _text_msg(f"wamid-{uuid.uuid4()}", phone,
                       "قللت وقت الإغلاق الشهري من عشرة أيام لأربعة"),
             wa, admin, deps)

    assert _direct_tickets(owner_session, phone) == [], \
        "an answer to our own enrichment question became a support ticket"


# ── a buyer who is still silenced (D2.2 / D2.3) ─────────────────────────────


def _silence(owner_session: Session, phone: str) -> None:
    owner_session.execute(text(
        "UPDATE customer_channels SET opt_out_at = :n WHERE phone_e164 = :p"),
        {"n": NOW, "p": phone})
    owner_session.commit()


def test_a_silenced_buyer_is_not_walked_through_a_journey_he_cannot_read(
    owner_session: Session, clean_billing: None
) -> None:
    """D2.2 — `ActivationResult.opted_out` was added to be read and had no
    reader anywhere in the tree.

    A customer who bought while still opted out gets ONE welcome saying why
    nothing will arrive — and then the handover started the journey on top of
    it, buried that line under the consent gate, and carried him all the way
    to ACTIVE. «Fully onboarded» with every prompt unread, to someone whose
    standing instruction is silence.
    """
    from career.whatsapp.worker import _handle_message

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _phone()
    order_id = f"ORD-{uuid.uuid4()}"
    salla = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"),
                             "SAR", customer_phone=_probe_phone())
    })
    result = provision_order(
        owner_session, order_id, salla_client=salla, product_catalog=CATALOG,
        expected_pricing={k: (Decimal("279.00"), "SAR") for k in CATALOG})
    token = result.activation_token
    assert token is not None
    # He opted out before he paid — the channel exists and is silenced.
    owner_session.execute(text(
        "INSERT INTO customer_channels (id, tenant_id, provider, phone_e164,"
        " opt_out_at) VALUES (:i, :t, 'whatsapp', :p, :n)"),
        {"i": str(uuid.uuid4()), "t": result.tenant_id, "p": phone, "n": NOW})
    owner_session.commit()

    _handle_message(owner_session,
                    _text_msg(f"wamid-{uuid.uuid4()}", phone, f"تفعيل {token}"),
                    whatsapp_client=wa, admin_client=admin, now=NOW,
                    onboarding=deps)
    owner_session.commit()

    journeys = owner_session.execute(text(
        "SELECT o.state FROM onboarding_sessions o JOIN customer_channels c"
        " ON c.tenant_id = o.tenant_id WHERE c.phone_e164 = :p"),
        {"p": phone}).all()
    assert journeys == [], \
        f"a silenced buyer was walked into the journey anyway — {journeys}"
    # the notice is the LAST thing on his screen, not the first of five
    assert "موقفة" in (wa.sent[-1].body or "") or \
           "موقوفة" in (wa.sent[-1].body or ""), wa.sent[-1].body

    # …and the handover is deferred, never lost: the word brings it back.
    _handle_message(owner_session,
                    _text_msg(f"wamid-{uuid.uuid4()}", phone, "تشغيل الرسائل"),
                    whatsapp_client=wa, admin_client=admin, now=NOW,
                    onboarding=deps)
    owner_session.commit()
    resumed = owner_session.execute(text(
        "SELECT o.state FROM onboarding_sessions o JOIN customer_channels c"
        " ON c.tenant_id = o.tenant_id WHERE c.phone_e164 = :p"),
        {"p": phone}).all()
    assert resumed, "the deferred handover never ran — he is activated and mute"


def test_the_way_back_is_printed_again_for_a_near_miss_resume(
    owner_session: Session, clean_billing: None
) -> None:
    """D2.3 — «طيب تشغيل الرسائل من فضلك» classifies as OTHER.

    RESUME is matched on the WHOLE message and deliberately so. Its docstring
    argued the miss was cheap «because the confirmation prints تشغيل
    الرسائل» — true only for a customer who still has that message on screen,
    and the opted-out branch answered him with ZERO outbound. He asked to come
    back, in words, and heard nothing at all.
    """
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _start_journey(owner_session, wa, deps)
    _set_journey(owner_session, phone, "ACTIVE")
    _silence(owner_session, phone)
    before = len(wa.sent)

    _deliver(owner_session, phone,
             _text_msg(f"wamid-{uuid.uuid4()}", phone,
                       "طيب تشغيل الرسائل من فضلك"),
             wa, admin, deps)

    assert len(wa.sent) > before, \
        "a customer asking to come back was answered with nothing at all"
    reply = wa.sent[-1].body or ""
    assert "تشغيل الرسائل" in reply, "the exact word back is not printed"
    # still silenced — a guess must never un-mute someone on record as having
    # asked for silence (that is the surface RESUME's narrowness protects)
    still = owner_session.execute(text(
        "SELECT opt_out_at FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone}).scalar_one()
    assert still is not None, "a near miss un-silenced him by guessing"


# ── the forgotten ticket, swept while the operator sleeps ───────────────────


def _worker_loop() -> Any:
    """`scripts/run_worker_loop.py` as a module — the process the systemd unit
    runs. Loaded exactly like `_replay_tool` above, and for the same reason:
    the wiring in a script is still code, and code with no test is how a
    feature ends up built and reaching nobody. `main()` is not executed —
    importing under a name other than `__main__` cannot start the loop."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "run_worker_loop.py"
    spec = importlib.util.spec_from_file_location("career_worker_loop", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["career_worker_loop"] = module
    spec.loader.exec_module(module)
    return module


def _forgotten_ticket(
    owner_session: Session, phone: str, *, age_hours: int = 72
) -> tuple[str, str]:
    """An open support ticket nobody has closed for `age_hours`. Returns
    (ticket id, TEN code)."""
    channel_id, tenant_id, code = owner_session.execute(text(
        "SELECT c.id, c.tenant_id, t.code FROM customer_channels c"
        " JOIN tenants t ON t.id = c.tenant_id WHERE c.phone_e164 = :p"),
        {"p": phone}).one()
    ticket_id = str(uuid.uuid4())
    owner_session.execute(text(
        "INSERT INTO support_events (id, tenant_id, channel_id, kind, status,"
        " created_at) VALUES (:i, :t, :c, 'executive_direct_message', 'open',"
        " :a)"),
        {"i": ticket_id, "t": tenant_id, "c": channel_id,
         "a": NOW - timedelta(hours=age_hours)})
    owner_session.commit()
    return ticket_id, str(code)


def _ticket_status(owner_engine: Engine, ticket_id: str) -> str:
    """Read from a session of its own, so «released» means COMMITTED and not
    merely pending in the caller's transaction."""
    with Session(owner_engine) as s:
        return str(s.execute(text(
            "SELECT status FROM support_events WHERE id = :i"),
            {"i": ticket_id}).scalar_one())


def test_a_forgotten_ticket_is_released_while_the_operator_sleeps(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """`console.release_forgotten_tickets` had one caller: the operator's own
    console traffic.

    Which releases fastest for the customers who need it least — the operator
    who has stopped opening the watchtower is exactly the operator who forgot
    the ticket, and while it sits open the dedupe («one open ticket per
    customer») silences that customer's direct line. The worker loop is the
    process that is awake at 03:00, so the sweep runs there too, hourly.
    """
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _plus_customer(owner_session, wa, deps)
    ticket, code = _forgotten_ticket(owner_session, phone)
    admin.messages.clear()

    released = _worker_loop().sweep_forgotten_tickets(
        owner_session, admin_client=admin, now=NOW)

    assert released >= 1
    assert _ticket_status(owner_engine, ticket) == "released", \
        "the loop swept nothing — the mute outlives the operator's attention"
    pages = [m for m in admin.messages if code in m]
    assert len(pages) == 1, f"paged {len(pages)} times, not once: {pages}"
    assert phone.lstrip("+") not in pages[0]           # §15.13
    # …and once EVER: a second pass an hour later has nothing to release,
    # because «open → released» is a one-way edge on the row itself.
    admin.messages.clear()
    again = _worker_loop().sweep_forgotten_tickets(
        owner_session, admin_client=admin, now=NOW + timedelta(hours=1))
    assert not [m for m in admin.messages if code in m], \
        f"the same ticket paged twice (released={again})"


def test_a_dead_telegram_never_costs_the_customer_his_released_line(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The ordering, asserted rather than described.

    The release commits and the page follows it, so an operator channel that
    is down loses a notice that can never be raised again — the trade named in
    `console.release_forgotten_tickets` and taken here deliberately. What it
    must NEVER do is the other thing: hold the customer's line shut because
    Telegram is unwell. The repair is his, the page is the operator's, and the
    ticket is still on the operator's queue with its age and its ⏳ mark
    either way.
    """
    class _DeafOperator:
        def send_admin(self, text: str) -> str:
            raise RuntimeError("telegram is down")

    wa = FakeWhatsAppClient()
    deps = _worker_deps(wa)
    phone = _plus_customer(owner_session, wa, deps)
    ticket, _code = _forgotten_ticket(owner_session, phone)

    # it does not raise: a housekeeping sweep never takes the message loop
    # (nor this cycle's watchdog heartbeat) down with it
    released = _worker_loop().sweep_forgotten_tickets(
        owner_session, admin_client=_DeafOperator(), now=NOW)

    assert released >= 1
    assert _ticket_status(owner_engine, ticket) == "released", \
        "a Telegram outage rolled back the customer's own repair"


def test_the_worker_loop_sweeps_forgotten_tickets_every_hour() -> None:
    """The wiring itself, at the one place it can be read: the hourly block.

    A sweep with a tested function and no call in the loop is the defect this
    change exists to close, and the loop's `main()` cannot be executed in a
    test (it polls forever against a live Graph client), so the CALL is
    asserted from the source. It has to sit inside the hourly branch — one per
    cycle would be a release sweep every three seconds — and after the sends
    it must not be able to skip.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1]
              / "scripts" / "run_worker_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "sweep_forgotten_tickets"]
    assert len(calls) == 1, "the sweep is called once per hourly pass or not at all"
    # inside the hourly gate, never in the 3-second body
    hourly = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and "REMINDER_SWEEP_SECONDS" in ast.dump(n.test)]
    assert len(hourly) == 1
    assert id(calls[0]) in {id(node) for node in ast.walk(hourly[0])}, \
        "the ticket sweep runs on the poll cycle, not on the hourly sweep"


def test_no_alert_from_this_worker_reverses_on_the_operator_screen() -> None:
    """The two alerts Fahad actually reads from here, held direction-pure.

    `tests/test_alert_direction_purity` owns the rule and scans the whole
    tree — but that test is red for files other agents own, so a regression
    in THIS file would land inside an already-failing assertion and be read
    by nobody. This is the green half, and it fails for one file only.

    Both lines it protects carried a TEN code INSIDE an Arabic sentence:
    «🔇 عميل أوقف الرسائل TEN-0002 — …» when a paying customer silences us,
    and «📎 عميل أرسل ملفًا بعد التفعيل TEN-0002 — …» when one sends a file.
    His client reverses such a line, and the code — the only part that says
    WHOSE line just went quiet — is the part that moves.
    """
    from tests.test_alert_direction_purity import SLOT, verdict

    hits = verdict().get("src/career/whatsapp/worker.py", [])
    assert not hits, "mixed-direction operator line(s) — " + " ; ".join(
        f"L{n}: {line.replace(SLOT, '{…}')!r}" for n, line in hits
    )


# ── the «+» boundary, which this file has already been bitten by ────────────


def test_a_customers_own_number_is_not_a_stranger_because_of_a_plus(
    owner_session: Session, clean_billing: None
) -> None:
    """`_channel_for_phone` compared the phone EXACTLY while the two functions
    that resolve the same fact out of the same payload — `_event_ten_codes`,
    thirty lines above it in this very file, and
    `activation_flow._channel_for_phone`, one import away — both go through
    `phone_variants`.

    Latent when it was found, and the honest reason is worth writing down: the
    only writer of `customer_channels.phone_e164` in the tree is
    `activation_flow._activate_with_token`, which stores Meta's own ``from``,
    and Meta always delivers it without the ``+``. So today's rows and today's
    lookups happen to be the same shape. One row written the human way — a
    restore, a backfill, an operator linking a number by hand, a second
    provider — is all it takes, and the unique constraint is on the STRING, so
    the database is perfectly happy to hold both spellings.

    What it would cost is the whole point: an ACTIVE paying customer read as
    an UNKNOWN NUMBER. He is answered «أرسل رمز التفعيل» for a code we only
    ever send to the operator, no inbound row is written, his 24h window is
    never opened, a held bundle never descends, no ticket is raised — while
    `_event_ten_codes` names him perfectly well in the operator's failure
    notice, so the same file disagrees with itself about whether he exists.
    This repository has paid for that exact boundary three times already (the
    evening window-nudge, the §14 canary ordering, the whole zero-touch claim
    path), each time as «a comparison that could not match a real customer
    even once».
    """
    from career.whatsapp.worker import (
        _UNRECOGNIZED,
        _channel_for_phone,
        _event_ten_codes,
    )

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    deps = _worker_deps(wa)
    phone = _plus_customer(owner_session, wa, deps)     # row holds «+9665…»
    assert phone.startswith("+")
    meta = phone.lstrip("+")                            # what Meta delivers
    admin.messages.clear()
    before = len(wa.sent)

    wamid = f"wamid-{uuid.uuid4()}"
    _deliver(owner_session, meta,
             _text_msg(wamid, meta, "متى توصلني فرص اليوم؟"),
             wa, admin, deps)

    # THIS message, on HIS channel — not merely «some inbound row exists»,
    # which his own activation already satisfies.
    recorded = owner_session.execute(text(
        "SELECT i.classification FROM inbound_messages i"
        " JOIN customer_channels c ON c.id = i.channel_id"
        " WHERE c.phone_e164 = :p AND i.wa_message_id = :w"),
        {"p": phone, "w": wamid}).all()
    assert recorded, \
        "an ACTIVE customer's message was dropped as an unknown number"
    said = [m.body or "" for m in wa.sent[before:]]
    assert all(_UNRECOGNIZED not in body for body in said), (
        "a paying customer was asked for the activation code he used months "
        f"ago — {said}"
    )
    # …and the file no longer disagrees with itself about who he is
    codes = _event_ten_codes(owner_session, _payload([_text_msg(
        f"wamid-{uuid.uuid4()}", meta, "أي شي")]))
    assert codes.startswith("TEN-"), codes
    # the lookup itself, stated once directly: one number, either spelling,
    # the same channel — the property that has to stay true when the routing
    # around it changes
    both = {_channel_for_phone(owner_session, p) for p in (phone, meta)}
    assert len(both) == 1 and None not in both, \
        "the same customer resolves to two different answers"


def test_the_two_readings_of_the_phone_column_pick_the_same_row(
    owner_session: Session, clean_billing: None
) -> None:
    """Both spellings of ONE number in the table, bound to two tenants, and the
    older binding inserted SECOND — so a sequential scan hands back the newer
    row first.

    `_channel_for_phone` orders by ``(created_at, id)``, and so does
    `activation_flow._channel_for_phone`: the oldest binding, the proven one.
    `_event_ten_codes` was the THIRD reading of this column and had no ORDER BY
    at all, so it named whichever row the scan reached first — proved here on
    2026-08-07 by an adversarial pass, which got a different TEN code out of it
    than the row the message was actually routed to. An operator's failure
    notice that names the wrong customer is worse than one that names nobody,
    and it is silently non-deterministic besides. It now calls
    `_channel_for_phone`, so there are two readings and not three, and this
    pins the property rather than the implementation: whatever the tie-break
    is, the function that NAMES him and the function that DECIDES for him must
    agree on which row he is.
    """
    from career.whatsapp import activation_flow
    from career.whatsapp.worker import _channel_for_phone, _event_ten_codes

    digits = f"96650{uuid.uuid4().int % 10_000_000:07d}"
    rows = {}
    for label, spelling, created in (
        ("newer", digits, NOW - timedelta(days=1)),      # physically first
        ("older", f"+{digits}", NOW - timedelta(days=30)),
    ):
        tid = uuid.uuid4()
        owner_session.execute(text(
            "INSERT INTO tenants (id, code) VALUES (:i, :c)"),
            {"i": str(tid), "c": f"TEN-Q{uuid.uuid4().int % 100_000:05d}"})
        owner_session.execute(text(
            "INSERT INTO customer_channels (id, tenant_id, provider,"
            " phone_e164, created_at) VALUES (:i, :t, 'whatsapp', :p, :c)"),
            {"i": str(uuid.uuid4()), "t": str(tid), "p": spelling,
             "c": created})
        rows[label] = tid
    owner_session.commit()

    older_code = owner_session.execute(text(
        "SELECT code FROM tenants WHERE id = :t"),
        {"t": str(rows["older"])}).scalar_one()

    for lookup in (_channel_for_phone, activation_flow._channel_for_phone):
        channel = lookup(owner_session, digits)
        assert channel is not None and channel.tenant_id == rows["older"], (
            f"{lookup.__module__}._channel_for_phone took the newer binding")

    named = _event_ten_codes(owner_session, _payload([_text_msg(
        f"wamid-{uuid.uuid4()}", digits, "أي شي")]))
    assert named == older_code, (
        "the operator's failure notice names a different customer than the "
        f"one the message was routed to: {named} vs {older_code}")


# ── «did the hourly block run at all» ───────────────────────────────────────
#
# THE INCIDENT. On 2026-08-07 the worker unit was ACTIVE, watchdog-fed, with
# no ERROR line for seven hours — and `delivery_guarantees` was empty,
# `/run/career/worker-housekeeping` did not exist, and
# `journalctl -u career-worker | grep "guarantee sweep"` had never matched.
# The cause was not in the block: the RUNNING PROCESS predated it. PID
# 1831439 started 19:29:46 from the checkout at 47bbf9d, whose
# `run_worker_loop.py` has no `HousekeepingGate`, no `sweep_and_commit` and no
# SLA sweep at all; the tracebacks it logged put `process_pending_whatsapp` at
# line 327, which is where that revision has it and 293 lines from where the
# current file does. The block was added at 01:19 the next morning and the
# unit was never restarted.
#
# What made it INVISIBLE for that long is the thing these tests hold shut:
# every stanza logs only when its count is non-zero, so an idle hour and a
# binary with no housekeeping in it produce byte-identical journals. A
# question that cannot distinguish «nothing was due» from «nothing ran» is not
# a question, and it was the only one anybody had.
#
# These live here rather than beside the gate's other tests in
# `test_promises.py` because that file is another agent's working tree today;
# `_worker_loop` above already loads the same script for the same reason.


def _gate(tmp_path: Any, *, wall: float = 1_754_600_000.0) -> Any:
    """A gate on a private `/run`, with a wall clock a test can hold still."""
    return _worker_loop().HousekeepingGate(
        path=str(tmp_path / "career" / "worker-housekeeping"),
        wall=lambda: wall, mono=lambda: 2_170_615.0,
    )


def test_a_claimed_hour_and_a_finished_pass_are_different_facts(
    tmp_path: Any,
) -> None:
    """`mark()` is written BEFORE the block, deliberately — so it says «an
    hour was taken», never «the work happened». Nothing recorded the second
    fact, so a pass that raised inside the enrichment sweep left behind a
    stamp indistinguishable from one that swept all six promises: the only
    durable record of housekeeping read «fresh» for an hour in which no
    promise was measured."""
    gate = _gate(tmp_path)

    gate.mark()
    assert gate.last_completed() is None, (
        "a claimed interval reports itself as finished work — the stamp "
        "cannot tell a pass that died halfway from one that ran")

    gate.completed()
    assert gate.last_completed() == 1_754_600_000.0

    # …and the claim survives it, because `due()` reads line one and the
    # gate's whole rate-limiting behaviour hangs off that number.
    assert gate.due(3600.0) is False
    stamp = tmp_path / "career" / "worker-housekeeping"
    assert stamp.read_text(encoding="utf-8").splitlines()[0].startswith(
        "1754600000 "), "recording the finish rewrote the claim"

    # A NEW hour claimed after it drops the old proof rather than inheriting
    # it: «claimed, not yet finished» must be readable as exactly that.
    gate.mark()
    assert gate.last_completed() is None


def test_the_stamp_survives_a_gate_that_cannot_write(tmp_path: Any) -> None:
    """The degraded path is the one where nothing may raise: a `/run` this
    process cannot write already costs the block its catch-up, and it must not
    also cost the loop its cycle."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    gate = _worker_loop().HousekeepingGate(
        path=str(blocked / "career" / "worker-housekeeping"),
        wall=lambda: 1_754_600_000.0, mono=lambda: 2_170_615.0,
    )
    gate.mark()
    gate.completed()                       # no raise, and nothing to read
    assert gate.last_completed() is None


def test_an_idle_hour_and_a_block_that_never_ran_are_distinguishable(
    caplog: Any, monkeypatch: Any,
) -> None:
    """The defect that hid the incident, stated as a property.

    The pass publishes itself on counts that are ALL ZERO. Two channels, and
    neither is an hourly log line: a systemd `STATUS=`, which `systemctl
    status career-worker` prints and which costs one line of state however
    idle the week is, and exactly ONE `INFO` line on the first completed pass
    of a process — the fact the journal could not previously answer, which is
    that this binary has a housekeeping block and reached the end of it.
    """
    loop = _worker_loop()
    sent: list[str] = []
    monkeypatch.setattr(loop.sd_notify, "notify",
                        lambda state: sent.append(state) or True)
    idle = {"reminders": 0, "enrich": 0, "outcome": 0, "guarantee": 0,
            "sla": 0, "tickets": 0}
    when = datetime(2026, 8, 8, 7, 0, tzinfo=UTC)

    with caplog.at_level("INFO"):
        first = loop.publish_housekeeping(idle, when=when, first=True)
        loop.publish_housekeeping(idle, when=when, first=False)

    assert len(sent) == 2, "an idle pass told systemd nothing"
    assert all(s.startswith("STATUS=") for s in sent)
    assert "2026-08-08T07:00:00" in first and "guarantee=0" in first

    # once per PROCESS, not once per hour: an idle system must not pay a
    # journal line an hour for the privilege of being observable
    announcements = [r for r in caplog.records
                     if "housekeeping" in r.getMessage()]
    assert len(announcements) == 1, (
        f"the block announces itself {len(announcements)} times per process — "
        "an hourly all-quiet line is the alarm nobody reads")


def test_the_hourly_block_records_its_completion_unconditionally() -> None:
    """Read from the TREE, because the claim is about WIRING.

    Two things the incident makes non-negotiable, and neither can be seen from
    inside a function: the completion record has to be the LAST thing in the
    hourly block — so a stanza that raises leaves the stamp claimed and
    unfinished rather than falsely fresh — and it must not sit under an `if`,
    because «the block ran» is exactly the fact that must not depend on the
    block having found work.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_worker_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    gated = [n for n in ast.walk(tree) if isinstance(n, ast.If)
             and "attr='due'" in ast.dump(n.test)]
    assert len(gated) == 1, "the hourly block is not gated by the stamp"
    block = gated[0]

    def _calls(node: Any) -> list[str]:
        return [n.func.attr if isinstance(n.func, ast.Attribute)
                else getattr(n.func, "id", "")
                for n in ast.walk(node) if isinstance(n, ast.Call)]

    assert "completed" in _calls(block), (
        "the pass never records that it FINISHED — the stamp goes on saying "
        "«fresh» for an hour that swept nothing")
    assert "publish_housekeeping" in _calls(block), (
        "nothing outside the journal can answer «did the block run»")

    # top level of the block only: under an `if`, an idle hour is silent again
    top = [s for s in block.body if "publish_housekeeping" in _calls(s)]
    assert len(top) == 1 and isinstance(top[0], ast.Expr), (
        "the pass publishes itself conditionally — an idle hour and a binary "
        "with no housekeeping block would look identical again")
    assert "completed" in _calls(block.body[-2]) or \
           "completed" in _calls(block.body[-1]), (
        "the completion record is not at the END of the block, so a stanza "
        "that raises above it would still stamp the hour as finished")


# ── Meta's `errors` array, which this handler used to drop ──────────────────
#
# THE HARM. `_handle_status` read `st["status"]` and never `st["errors"]`, so
# 131049 (the per-user marketing cap), 131050 (the recipient switched
# «Offers and announcements» off) and 131047 (re-engagement required) all
# landed as one undifferentiated `failed`. The bundle they suppressed then
# expired as EXPIRED_WINDOW three hours later, and the console rendered that
# «⌛ انتهت مهلتها دون تسليم» — so the operator read «the customer ignored us»
# about a message Meta declined to hand over. Six of sixteen bundles in the
# live data have expired and the 72-hour start guarantee is a financial
# promise resting on that number.
#
# Note WHERE the defect was, because it is easy to state one step wrong: the
# code was never FILED as EXPIRED_WINDOW. It was discarded here, at the status
# webhook, and the expiry sweep then supplied a wrong-by-omission explanation
# afterwards. This is the discard.


def _refusal(owner_session: Session, wamid: str, code: int, *,
             status: str = "failed", now: datetime = NOW) -> None:
    """One status callback shaped the way Meta sends a refusal: HTTP 200 was
    returned at send time, and the reason arrives only here."""
    _insert_event(owner_session, _payload(statuses=[{
        "id": wamid, "status": status, "recipient_id": "966500000000",
        "errors": [{"code": code, "title": "…", "error_data": {}}],
    }]))
    _run(owner_session, FakeWhatsAppClient(), FakeTelegramAdminClient(), now=now)


def test_a_refused_send_names_the_meta_code_on_the_operators_screen(
    owner_session: Session, clean_billing: None, caplog: Any
) -> None:
    """The reason reaches the one screen he reads, with no migration.

    `run_admin_bot._HealthProbes.error_lines` harvests «ERROR:» lines from
    this unit onto «🧾 آخر الأخطاء المسجلة», so an ERROR here is on his phone
    today — which is why the code goes there and not into a new column.
    """
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid, template="daily_opportunities")

    with caplog.at_level("ERROR"):
        _refusal(owner_session, wamid, 131049)

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    named = [m for m in errors if "131049" in m]
    assert named, f"Meta's reason was discarded again: {errors}"
    # the code, the customer, and the correction of the wrong reading — the
    # three things the expiry screen could not say
    assert "TEN-" in named[0]
    assert "REFUSED" in named[0]
    assert "going quiet" in named[0]
    # §15.13: a TEN code, never the phone
    assert phone.lstrip("+") not in named[0]

    # …and the STATUS WORD is untouched. `cv/close.whatsapp_spend` bills every
    # template whose status is not exactly «failed», so spelling a refusal any
    # other way would start billing us for messages Meta never delivered.
    assert _receipt_row(owner_session, wamid).status == "failed"


def test_a_refusal_we_never_recorded_is_still_reported(
    owner_session: Session, clean_billing: None, caplog: Any
) -> None:
    """Not hypothetical: staging holds a 131047 refusal whose wa_message_id
    appears in NO `delivery_messages` row — a send we were refused and never
    wrote down. With the report hung off the row, that one is invisible
    forever, which is the worst case of the three and the easiest to miss."""
    orphan = f"wamid-{uuid.uuid4()}"

    with caplog.at_level("ERROR"):
        _refusal(owner_session, orphan, 131047)

    named = [r.getMessage() for r in caplog.records
             if r.levelname == "ERROR" and "131047" in r.getMessage()]
    assert named, "a refusal with no ledger row was swallowed"
    assert "(no row)" in named[0], \
        "the line implies a customer we cannot actually name"


def test_a_redelivered_refusal_does_not_refill_the_error_screen(
    owner_session: Session, clean_billing: None, caplog: Any
) -> None:
    """Meta redelivers webhooks freely. The report is hung on the TRANSITION —
    the receipt that actually won — so ten redeliveries of one refusal are one
    line. A screen that repeats itself is a screen that stops being read, and
    this repository has spent two days on that lesson."""
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid)

    with caplog.at_level("ERROR"):
        _refusal(owner_session, wamid, 131050)
        _refusal(owner_session, wamid, 131050, now=LATER)

    named = [r for r in caplog.records if "131050" in r.getMessage()]
    assert len(named) == 1, f"one refusal reported {len(named)} times"


def test_an_unrecognised_meta_code_is_reported_rather_than_guessed_at(
    owner_session: Session, clean_billing: None, caplog: Any
) -> None:
    """The blind spot the known-codes table would otherwise create. A code we
    have no words for is still a refusal, and the number itself is enough to
    look up — silence is the only answer that helps nobody."""
    from career.whatsapp import worker as wa_worker

    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activate_phone(owner_session, wa, admin)
    wamid = f"wamid-{uuid.uuid4()}"
    _seed_outbound(owner_session, phone, wamid)
    assert 133015 not in wa_worker.META_REFUSAL_CODES

    with caplog.at_level("ERROR"):
        _refusal(owner_session, wamid, 133015)

    named = [r.getMessage() for r in caplog.records if "133015" in r.getMessage()]
    assert named and "unrecognised" in named[0]


def test_the_error_array_is_read_whatever_shape_meta_sends() -> None:
    """`status_error_codes` is total, because the caller's whole job is to
    stop throwing this array away — and a parser that raises on an unexpected
    shape would throw away the cycle instead."""
    from career.whatsapp.worker import status_error_codes

    assert status_error_codes({}) == []
    assert status_error_codes({"errors": None}) == []
    assert status_error_codes({"errors": ["not a dict"]}) == []
    assert status_error_codes({"errors": [{"title": "no code"}]}) == []
    assert status_error_codes({"errors": [{"code": "131049"}]}) == [131049]
    assert status_error_codes(
        {"errors": [{"code": 131049}, {"code": 131050}]}) == [131049, 131050]
