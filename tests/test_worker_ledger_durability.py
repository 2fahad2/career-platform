"""The other direction of constant 3: a send that landed must not be un-recorded.

«لا كتابة في السجلّ إلا بعد اكتمال التسليم، والكتابة ذرية» names ONE direction —
never write early — and CHANGELOG 39 names the silent one: never lose the write
afterwards. On 2026-08-10 a live nightly sent four real messages and then lost
every ledger row when later work raised and the per-tenant ``except`` rolled
back. Every test here is a different unit standing in the same place: a send has
happened, later work raises, and the question is what is still on disk.

Four holes, one shape:

1. `worker._handle_message` descends a held bundle — the SAME four-message
   bundle the nightly sends — and then runs close, rollup, enrichment,
   escalation, four replies and an admin send before its commit ~130 lines
   later, while both failure paths in the event loop call ``rollback()``
   unconditionally.
2. `orchestrator.send_due_reminders` owned no commit at all: one Graph error on
   tenant N discarded tenants 1..N-1's BILLED template rows *and* their
   `last_reminder_at`, so the next hour sent and paid for them again.
3. `enrichment.run_hourly_sweep` put both sends inside a savepoint — the wrong
   side of the wire — so the second send's failure rolled back the FIRST send's
   ledger row.
4. And the money half: `_handle_status` filled `delivery_messages.category`
   once and never again, so a band frozen from the template REGISTRY at hour 48
   shut out the receipt that landed at hour 49 — $0.0501 recorded where Meta
   charged $0.0107, permanently.

Each test asserts on ROWS after a rollback, never on a return value: the bug in
every case is what was left behind.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel
from career.onboarding import enrichment as enr
from career.onboarding import orchestrator
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp import worker as wa_worker
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient, WhatsAppSendError
from career.whatsapp.delivery import deliver_adaptive
from career.whatsapp.templates import DAILY_UTILITY, REGISTRY, billed_category

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
CATALOG = {"prod_pro": "professional"}


def _phone() -> str:
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


# ── ① the descend path: a bundle that landed, and later work that raises ─────


def _activated_channel(session: Session, wa: Any, admin: Any) -> str:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"),
                             "SAR", customer_phone=_phone())
    })
    pricing = {k: (Decimal("279.00"), "SAR") for k in CATALOG}
    token = provision_order(session, order_id, salla_client=client,
                            product_catalog=CATALOG,
                            expected_pricing=pricing).activation_token
    assert token is not None
    phone = _phone()
    activate(session, token=token, from_phone=phone, display_name=None,
             now=NOW, whatsapp_client=wa, admin_client=admin)
    session.commit()
    return phone


def _hold_a_bundle(session: Session, phone: str, wa: Any) -> None:
    """Shut the 24h window and queue a bundle behind it — the state every
    customer who went quiet for a day is in by morning."""
    channel = session.execute(
        sql_text("SELECT id FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    session.execute(
        sql_text("UPDATE customer_channels SET last_inbound_at = :t"
                 " WHERE id = :i"),
        {"t": NOW - timedelta(hours=25), "i": channel},
    )
    session.commit()
    ch = session.get(CustomerChannel, channel)
    assert ch is not None
    deliver_adaptive(session, ch, {"parts": [{"kind": "text", "body": "#1 job"}]},
                     run_date=NOW.date(), whatsapp_client=wa,
                     daily_template=DAILY_UTILITY, now=NOW)
    session.commit()


def _event(session: Session, payload: dict[str, Any]) -> None:
    session.execute(sql_text(
        "INSERT INTO webhook_events (id, provider, event_type,"
        " event_fingerprint, signature_valid, payload, processing_status)"
        " VALUES (:i, 'whatsapp', 'messages', :f, true, CAST(:p AS jsonb),"
        " 'received')"),
        {"i": str(uuid.uuid4()), "f": f"wa:{uuid.uuid4()}",
         "p": json.dumps(payload)})
    session.commit()


def test_a_descended_bundle_survives_a_raise_in_the_work_that_follows(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The worker's own copy of the 10 August incident.

    The customer taps, `descend_pending_delivery` sends the held bundle, and
    the very next call — `close_from_delivery` — raises. The event loop's
    poison path calls ``session.rollback()``, and before the durability point
    that rollback took the ledger rows for messages the customer was already
    reading. `_dead_letter` then reports a turn that «failed», about a bundle
    that had in fact been delivered.
    """
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    phone = _activated_channel(owner_session, wa, admin)
    _hold_a_bundle(owner_session, phone, wa)
    sent_before = len(wa.sent)

    from career.cv import daily_run

    def _boom(*_a: Any, **_k: Any) -> Any:
        # Exactly the 10 August shape: the FIRST work after the last send, and
        # a deterministic failure (a column the host database did not have) —
        # poison, so the loop dead-letters and rolls back.
        raise ValueError("a column the host database does not have")

    original = daily_run.close_from_delivery
    daily_run.close_from_delivery = _boom  # type: ignore[assignment]
    try:
        _event(owner_session, {"entry": [{"changes": [{"value": {
            "messaging_product": "whatsapp",
            "messages": [{"id": f"wamid.{uuid.uuid4().hex}", "from": phone,
                          "type": "text", "text": {"body": "عرض"}}],
        }}]}]})
        counts = wa_worker.process_pending_whatsapp(
            owner_session, whatsapp_client=wa, admin_client=admin,
            now=NOW + timedelta(minutes=1),
        )
    finally:
        daily_run.close_from_delivery = original  # type: ignore[assignment]

    assert counts["failed"] == 1, "the raise did not reach the loop's classifier"
    assert len(wa.sent) > sent_before, "the bundle never went out — wrong test"

    # …and the ledger says so, on disk, in a session that never saw the
    # rolled-back one.
    with Session(owner_engine) as fresh:
        # `delivery_id IS NOT NULL` is what makes this the BUNDLE's rows and
        # not the activation welcome's: only the delivery path attaches one,
        # and the welcome — committed during setup — is a `text` row too.
        rows = fresh.execute(sql_text(
            "SELECT dm.kind FROM delivery_messages dm JOIN customer_channels c"
            " ON c.id = dm.channel_id WHERE c.phone_e164 = :p"
            " AND dm.kind = 'text' AND dm.delivery_id IS NOT NULL"),
            {"p": phone}).all()
    assert rows, (
        "the customer has the bundle and the ledger has nothing — the ledger "
        "rows were rolled back after a successful send (CHANGELOG 39)"
    )


# ── ② the stall reminders: one commit per tenant, not one per pass ───────────


class _FailsOnSecondSend:
    """Sends once, then refuses — Meta 503 on the tenant after the first."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_template(self, to_phone: str, name: str, language: str,
                      variables: Any = None, buttons: Any = ()) -> str:
        if self.sent:
            raise WhatsAppSendError("graph HTTP 503 (code 131026)")
        mid = f"wamid.{uuid.uuid4().hex}"
        self.sent.append(mid)
        return mid


def _stalled_journey(session: Session, tenant_id: str) -> None:
    sub_id, channel_id = uuid.uuid4(), uuid.uuid4()
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t, 'basic',"
        " 'ACTIVE', :o, 149, 'SAR')"),
        {"i": str(sub_id), "t": tenant_id, "o": f"O-{uuid.uuid4()}"})
    session.execute(sql_text(
        "INSERT INTO customer_channels (id, tenant_id, subscription_id,"
        " provider, phone_e164, verified_at, opt_in_at, last_inbound_at)"
        " VALUES (:i, :t, :s, 'whatsapp', :p, :n, :n, :stall)"),
        {"i": str(channel_id), "t": tenant_id, "s": str(sub_id),
         "p": _phone(), "n": NOW, "stall": NOW - timedelta(hours=30)})
    session.execute(sql_text(
        "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
        " channel_id, state, last_interaction_at) VALUES (:i, :t, :s, :c,"
        " 'CV_UPLOAD_PENDING', :stall)"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "s": str(sub_id),
         "c": str(channel_id), "stall": NOW - timedelta(hours=30)})
    session.commit()


def _cleanup(session: Session, *tenant_ids: str) -> None:
    session.rollback()
    for tenant_id in tenant_ids:
        for table in ("delivery_messages", "role_enrichments", "usage_events",
                      "onboarding_sessions", "customer_channels",
                      "subscriptions", "profile_facts"):
            session.execute(
                sql_text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                {"t": tenant_id})
    session.commit()


def test_one_tenants_graph_error_cannot_unsend_another_tenants_reminder(
    owner_session: Session, owner_engine: Engine,
    two_tenants: tuple[str, str],
) -> None:
    """`onboarding_reminder` is a BILLED template to a customer whose window is
    shut by definition. With one flush at the end of the pass and no commit,
    a 503 on the second tenant discarded the first tenant's ledger row AND its
    `last_reminder_at` — so Meta had charged us for a message we recorded
    nowhere, and the next hour sent and paid for it again."""
    a, b = two_tenants
    _stalled_journey(owner_session, a)
    _stalled_journey(owner_session, b)
    client = _FailsOnSecondSend()
    deps = orchestrator.Deps(
        whatsapp_client=client, scanner=object(), storage=object(),
        extractor=object(),
    )
    try:
        with pytest.raises(WhatsAppSendError):
            orchestrator.send_due_reminders(owner_session, deps=deps, now=NOW)
        # The caller's session is discarded without a commit — the worker loop
        # opens it in a `with` block and the raise leaves it by that door.
        owner_session.rollback()

        with Session(owner_engine) as fresh:
            rows = fresh.execute(sql_text(
                "SELECT tenant_id::text FROM delivery_messages"
                " WHERE tenant_id::text IN (:a, :b)"), {"a": a, "b": b}).all()
            stamped = fresh.execute(sql_text(
                "SELECT count(*) FROM onboarding_sessions"
                " WHERE tenant_id::text IN (:a, :b)"
                " AND last_reminder_at IS NOT NULL"), {"a": a, "b": b},
            ).scalar_one()
        assert len(rows) == 1, (
            "the reminder that WAS sent has no ledger row — Meta billed it and "
            f"the next hour will send it again (rows: {rows})"
        )
        assert stamped == 1, (
            "`last_reminder_at` went back with the rollback, so the same "
            "customer is nudged and billed again within the hour"
        )
    finally:
        _cleanup(owner_session, a, b)


# ── ③ the enrichment sweep: the savepoint was on the wrong side of the wire ──


class _InteractiveOkTextFails:
    """The opening question lands; the examples message raises."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_interactive(self, to_phone: str, body: str, buttons: Any) -> str:
        mid = f"wamid.{uuid.uuid4().hex}"
        self.sent.append(mid)
        return mid

    def send_text(self, to_phone: str, body: str) -> str:
        raise WhatsAppSendError("graph HTTP 500 (code 131000)")


class _ExamplesWriter:
    """Arabic, digit-free, no Latin — passes `scrub_examples` untouched."""

    def write(self, title: str, description: str) -> list[str]:
        return ["قللت وقت الإغلاق الشهري", "رفعت جودة الشبكة"]


def _sweep_candidate(session: Session, tenant_id: str) -> uuid.UUID:
    """ACTIVE journey, no enrichment cursor, one thin role, window OPEN."""
    role_id, sub_id, channel_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session.execute(sql_text(
        "INSERT INTO profile_facts (id, tenant_id, category, payload, status,"
        " source) VALUES (:i, :t, 'experience', CAST(:p AS jsonb),"
        " 'CUSTOMER_CONFIRMED', 'test')"),
        {"i": str(role_id), "t": tenant_id, "p": json.dumps(
            {"title": "محلل بيانات", "employer": "شركة",
             "start_date": "2021-01", "end_date": "2023-01",
             "achievements": []})})
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t, 'basic',"
        " 'ACTIVE', :o, 149, 'SAR')"),
        {"i": str(sub_id), "t": tenant_id, "o": f"O-{uuid.uuid4()}"})
    session.execute(sql_text(
        "INSERT INTO customer_channels (id, tenant_id, subscription_id,"
        " provider, phone_e164, verified_at, opt_in_at, last_inbound_at)"
        " VALUES (:i, :t, :s, 'whatsapp', :p, :n, :n, :li)"),
        {"i": str(channel_id), "t": tenant_id, "s": str(sub_id),
         "p": _phone(), "n": NOW, "li": NOW - timedelta(hours=1)})
    session.execute(sql_text(
        "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
        " channel_id, state, completed_at) VALUES (:i, :t, :s, :c, 'ACTIVE',"
        " :done)"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "s": str(sub_id),
         "c": str(channel_id), "done": NOW - timedelta(days=10)})
    session.commit()
    return role_id


def test_the_examples_send_cannot_unsend_the_question_before_it(
    owner_session: Session, owner_engine: Engine,
    two_tenants: tuple[str, str],
) -> None:
    """Two sends, one savepoint around BOTH: the customer received the opening
    question, the examples message then failed, and the savepoint's rollback
    deleted the ledger row for the question he was looking at — plus the ASKED
    row that says we ever asked."""
    a, _ = two_tenants
    role_id = _sweep_candidate(owner_session, a)
    client = _InteractiveOkTextFails()
    try:
        counts = enr.run_hourly_sweep(
            owner_session, whatsapp_client=client,
            examples_writer=_ExamplesWriter(), now=NOW,
        )
        assert counts["swept"] == 1
        assert len(client.sent) == 1, "the opening question never went out"
        # The caller's session is discarded without a commit (the worker loop's
        # `with Session(engine)` block), so only what was COMMITTED survives.
        owner_session.rollback()

        with Session(owner_engine) as fresh:
            rows = fresh.execute(sql_text(
                "SELECT kind FROM delivery_messages WHERE tenant_id::text = :t"),
                {"t": a}).scalars().all()
            asked = fresh.execute(sql_text(
                "SELECT status FROM role_enrichments WHERE fact_id = :f"),
                {"f": str(role_id)}).scalars().all()
        assert "interactive" in rows, (
            "the question is with the customer and its ledger row was rolled "
            "back by the NEXT send's failure (CHANGELOG 39)"
        )
        assert asked == ["ASKED"], (
            "the once-ever ledger says we never asked, so the customer will be "
            f"asked the same thing again next hour: {asked}"
        )
    finally:
        _cleanup(owner_session, a)


def test_a_refused_first_send_gives_the_once_ever_nudge_back(
    owner_session: Session, owner_engine: Engine,
    two_tenants: tuple[str, str],
) -> None:
    """The counterweight, and the reason the savepoint stays open across the
    FIRST send (AUDIT ك-8): nothing reached the customer, so the ASKED row must
    go back — otherwise the once-ever rule spends his only nudge on a question
    he never saw."""
    a, _ = two_tenants
    role_id = _sweep_candidate(owner_session, a)

    class _AllSendsFail:
        def send_interactive(self, to_phone: str, body: str,
                             buttons: Any) -> str:
            raise WhatsAppSendError("graph HTTP 503 (code 131026)")

    try:
        counts = enr.run_hourly_sweep(
            owner_session, whatsapp_client=_AllSendsFail(),
            examples_writer=None, now=NOW,
        )
        owner_session.commit()
        assert counts["swept"] == 0
        with Session(owner_engine) as fresh:
            asked = fresh.execute(sql_text(
                "SELECT count(*) FROM role_enrichments WHERE fact_id = :f"),
                {"f": str(role_id)}).scalar_one()
            rows = fresh.execute(sql_text(
                "SELECT count(*) FROM delivery_messages WHERE tenant_id::text"
                " = :t"), {"t": a}).scalar_one()
        assert asked == 0, "a refused send spent the customer's only nudge"
        assert rows == 0, "a ledger row for a send that never happened"
    finally:
        _cleanup(owner_session, a)


# ── ④ the money half: a belief must never outrank a measurement ──────────────


def _ledger_row(session: Session, tenant_id: str, *, wamid: str,
                template_name: str, category: str | None,
                kind: str = "template") -> None:
    sub_id, channel_id = uuid.uuid4(), uuid.uuid4()
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency) VALUES (:i, :t, 'basic',"
        " 'ACTIVE', :o, 149, 'SAR')"),
        {"i": str(sub_id), "t": tenant_id, "o": f"O-{uuid.uuid4()}"})
    session.execute(sql_text(
        "INSERT INTO customer_channels (id, tenant_id, subscription_id,"
        " provider, phone_e164, verified_at, opt_in_at)"
        " VALUES (:i, :t, :s, 'whatsapp', :p, :n, :n)"),
        {"i": str(channel_id), "t": tenant_id, "s": str(sub_id),
         "p": _phone(), "n": NOW})
    session.execute(sql_text(
        "INSERT INTO delivery_messages (id, tenant_id, channel_id, kind,"
        " wa_message_id, template_name, status, status_updated_at, category)"
        " VALUES (:i, :t, :c, :k, :w, :tn, 'sent', :n, :cat)"),
        {"i": str(uuid.uuid4()), "t": tenant_id, "c": str(channel_id),
         "k": kind, "w": wamid, "tn": template_name, "n": NOW,
         "cat": category})
    session.commit()


def _receipt(wamid: str, category: str, *, billable: bool = True,
             status: str = "delivered") -> dict[str, Any]:
    return {"id": wamid, "status": status,
            "pricing": {"billable": billable, "pricing_model": "PMP",
                        "category": category}}


def _a_belief_and_a_different_receipt() -> tuple[str, str, str]:
    """A template in the REGISTRY, the band the nightly freeze would stamp on
    it, and a DIFFERENT band for Meta's receipt to carry. Computed rather than
    typed: `billed_category` reads a live measurement that a
    `template_category_update` push can move under this test."""
    name = DAILY_UTILITY.name
    assert name in REGISTRY
    belief = str(billed_category(name))
    return name, belief, "utility" if belief != "utility" else "marketing"


def test_a_frozen_belief_loses_to_the_receipt_that_arrives_after_it(
    owner_session: Session, two_tenants: tuple[str, str],
) -> None:
    """The 4.7× defect. `freeze_unmeasured_categories` stamps the REGISTRY's
    band on a row 48h after the send; `_handle_status` fills the column ONCE.
    A receipt delayed past that window — a webhook outage, a deferred
    `webhook_events` row, a night the worker spent down — therefore lost, and
    the row recorded $0.0501 for a send Meta charged $0.0107 at, for ever."""
    a, _ = two_tenants
    name, belief, measured = _a_belief_and_a_different_receipt()
    wamid = f"wamid.{uuid.uuid4().hex}"
    _ledger_row(owner_session, a, wamid=wamid, template_name=name,
                category=belief)          # ← exactly what the freeze writes
    try:
        wa_worker._handle_status(
            owner_session, _receipt(wamid, measured), now=NOW)
        owner_session.commit()
        recorded = owner_session.execute(sql_text(
            "SELECT category FROM delivery_messages WHERE wa_message_id = :w"),
            {"w": wamid}).scalar_one()
        assert recorded == measured, (
            f"the row kept the guess ({belief!r}) and threw away what Meta "
            f"charged ({measured!r})"
        )
    finally:
        _cleanup(owner_session, a)


def test_a_receipt_never_loses_to_a_receipt_and_never_blames_meta(
    owner_session: Session, two_tenants: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half of the same rule. A band that is NOT the freeze's value
    can only have come from a receipt, so fill-once still holds — and the line
    the operator reads must stop saying «receipt» about the value it kept: it
    sent him to Meta to explain a number this repository had written itself."""
    a, _ = two_tenants
    name, _belief, measured = _a_belief_and_a_different_receipt()
    wamid = f"wamid.{uuid.uuid4().hex}"
    # `service` is not a band the freeze can write (it stamps only what
    # `billed_category` returns), so this row's value is a measurement.
    _ledger_row(owner_session, a, wamid=wamid, template_name=name,
                category="service")
    try:
        with caplog.at_level(logging.ERROR, logger="career.whatsapp"):
            wa_worker._handle_status(
                owner_session, _receipt(wamid, measured), now=NOW)
        owner_session.commit()
        recorded = owner_session.execute(sql_text(
            "SELECT category FROM delivery_messages WHERE wa_message_id = :w"),
            {"w": wamid}).scalar_one()
        assert recorded == "service", "a measurement was overwritten"
        line = " ".join(r.getMessage() for r in caplog.records)
        assert "billed category" in line, \
            f"the disagreement was not reported at all: {line}"
        assert "reports billed category" not in line, (
            "the ERROR still tells the operator that the RECEIPT is the odd "
            "one out, about a column the nightly freeze also writes"
        )
        assert "freeze" in line, (
            "the line names only one of the two writers of this column, so the "
            "operator cannot tell whose value he is looking at"
        )
    finally:
        _cleanup(owner_session, a)


# ── ⑤ a receipt with no ledger row: loud for the ones that matter ────────────


def test_an_unledgered_billable_receipt_is_loud_and_a_service_ack_is_not(
    owner_session: Session, caplog: pytest.LogCaptureFixture,
) -> None:
    """Measured on 24 days of live data: 14 wa_message_ids carry a receipt and
    no ledger row, and TWELVE are free `service` acks from paths that
    deliberately record nothing (the conversational replies, the funnel, the
    samples). Alarming on those is how an operator learns to skip the report —
    entry 37's exact failure mode. Billable, or a band that is not `service`:
    one member, zero false alarms."""
    unknown = f"wamid.{uuid.uuid4().hex}"
    with caplog.at_level(logging.ERROR, logger="career.whatsapp"):
        wa_worker._handle_status(
            owner_session,
            _receipt(unknown, "service", billable=False), now=NOW)
    assert not [r for r in caplog.records if "never recorded" in r.getMessage()], (
        "a free service ack — unledgered BY DESIGN — was reported as a hole"
    )

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="career.whatsapp"):
        wa_worker._handle_status(
            owner_session,
            _receipt(f"wamid.{uuid.uuid4().hex}", "utility"), now=NOW)
    assert [r for r in caplog.records if "never recorded" in r.getMessage()], (
        "a BILLABLE send with no ledger row passed in silence — money and a "
        "delivery nobody can see"
    )
