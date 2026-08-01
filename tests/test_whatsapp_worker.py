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
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR")
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
