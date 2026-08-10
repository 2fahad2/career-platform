"""Adaptive delivery (DB) — open→direct, closed→template-then-descend, opted-out→none."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, Delivery, DeliveryMessage
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.delivery import (
    DELIVERY_COMPLETED,
    DELIVERY_NO_SEND,
    DELIVERY_PENDING,
    deliver_adaptive,
    descend_pending_delivery,
)
from career.whatsapp.templates import DAILY_UTILITY

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
CATALOG = {"prod_pro": "professional"}
_PR = {k: (Decimal("279.00"), "SAR") for k in CATALOG}
BUNDLE = {"parts": [
    {"kind": "text", "body": "#1 IT Operations Manager @ NEOM"},
    {"kind": "document", "ref": "tenants/x/cv1.pdf", "filename": "CV.pdf", "caption": "CV #1"},
]}


def _probe_phone() -> str:
    """A real Salla order ALWAYS carries the buyer's mobile — the whole
    zero-touch activation path keys on it. Test orders that omitted it were
    describing a shape the world never sends, and that unrealism is exactly
    how the phone defect survived: the suite was green while no real customer
    could have been activated."""
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _channel(owner_session: Session, *, last_inbound_at: datetime | None, opt_out: bool = False
             ) -> CustomerChannel:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR",
                       customer_phone=_probe_phone())
    })
    token = provision_order(owner_session, order_id, salla_client=client,
                            product_catalog=CATALOG,
                            expected_pricing=_PR).activation_token
    assert token is not None
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    activate(owner_session, token=token, from_phone=phone, display_name=None, now=NOW,
             whatsapp_client=FakeWhatsAppClient(), admin_client=FakeTelegramAdminClient())
    ch = owner_session.execute(
        select(CustomerChannel).where(CustomerChannel.phone_e164 == phone)
    ).scalar_one()
    ch.last_inbound_at = last_inbound_at
    if opt_out:
        ch.opt_out_at = NOW
    owner_session.commit()
    return ch


def _dm_count(owner_engine: Engine, delivery_id: uuid.UUID, kind: str) -> int:
    with Session(owner_engine) as s:
        return len(s.execute(
            select(DeliveryMessage).where(
                DeliveryMessage.delivery_id == delivery_id, DeliveryMessage.kind == kind
            )
        ).scalars().all())


def test_open_window_sends_directly(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    ch = _channel(owner_session, last_inbound_at=NOW)  # open
    wa = FakeWhatsAppClient()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()
    assert delivery.status == DELIVERY_COMPLETED
    kinds = [m.kind for m in wa.sent]
    assert kinds == ["text", "document"]  # bundle parts sent directly
    assert _dm_count(owner_engine, delivery.id, "text") == 1
    assert _dm_count(owner_engine, delivery.id, "document") == 1


def test_closed_window_sends_template_then_descends(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))  # closed
    wa = FakeWhatsAppClient()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()
    # Only the morning template goes out; the bundle is held.
    assert delivery.status == DELIVERY_PENDING
    assert [m.kind for m in wa.sent] == ["template"]
    assert delivery.template_message_id is not None

    # Customer taps → window opens → descend the held bundle.
    later = NOW + timedelta(minutes=5)
    ch.last_inbound_at = later
    descended = descend_pending_delivery(owner_session, ch, whatsapp_client=wa, now=later)
    owner_session.commit()
    assert descended is not None
    assert descended.status == DELIVERY_COMPLETED
    assert [m.kind for m in wa.sent] == ["template", "text", "document"]


def test_opted_out_sends_nothing(
    owner_session: Session, clean_billing: None
) -> None:
    ch = _channel(owner_session, last_inbound_at=NOW, opt_out=True)
    wa = FakeWhatsAppClient()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()
    assert delivery.status == DELIVERY_NO_SEND
    assert wa.sent == []


# ── the tap must never be spent on an attempt that delivered nothing ─────────

#: the group id IS the job url (cv/deliver.build_daily_bundle) — the daily
#: close suppresses by exactly this value, so the tests use a real one.
GROUP_URL = "https://careers.neom.example/j/1"

_GROUPED = {
    "grouped": True,
    "header": "فرصك اليوم",
    "jobs": [{
        "group": GROUP_URL,
        "card": {"body": "#1 IT Operations Manager @ NEOM"},
        "document": {"ref": "tenants/x/cv1.pdf", "filename": "CV.pdf",
                     "caption": "CV #1"},
    }],
    "close": {"gate_passes": 1, "cv_resolved": 1, "cv_failed": 0},
}


class _DeadDocuments(FakeWhatsAppClient):
    """Text lands, every document is rejected — the live 27 July shape."""

    def send_document(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("graph HTTP 400 (code 131053)")


def test_a_tap_that_delivered_nothing_stays_claimable(
    owner_session: Session, clean_billing: None
) -> None:
    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    later = NOW + timedelta(minutes=5)
    ch.last_inbound_at = later
    descended = descend_pending_delivery(
        owner_session, ch, whatsapp_client=dead, now=later)
    owner_session.commit()

    assert descended is not None
    # nothing landed → the customer's one chance is NOT spent
    assert descended.status == DELIVERY_PENDING
    assert descended.completed_at is None
    assert descended.bundle["attempts"] == 1


def _day_state(session: Session, tenant_id: uuid.UUID) -> tuple[str, dict] | None:
    row = session.execute(
        sql_text("SELECT state, counts FROM tenant_day_states WHERE tenant_id = :t"),
        {"t": str(tenant_id)},
    ).first()
    return (str(row[0]), dict(row[1] or {})) if row else None


def _suppressions(session: Session, tenant_id: uuid.UUID) -> int:
    return int(session.execute(
        sql_text("SELECT count(*) FROM tenant_job_suppressions WHERE tenant_id = :t"),
        {"t": str(tenant_id)},
    ).scalar_one())


def test_the_operator_can_resend_what_never_arrived(
    owner_session: Session, clean_billing: None
) -> None:
    from career.whatsapp.delivery import RESEND_SENT, resend_pending_delivery

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    later = NOW + timedelta(minutes=5)
    ch.last_inbound_at = later
    descend_pending_delivery(owner_session, ch, whatsapp_client=dead, now=later)
    owner_session.commit()

    # the operator retries once the cause is fixed — with a working client
    healthy = FakeWhatsAppClient()
    again = resend_pending_delivery(
        owner_session, tenant_id=ch.tenant_id, whatsapp_client=healthy,
        now=later + timedelta(minutes=30),
    )
    owner_session.commit()

    assert again.outcome == RESEND_SENT
    assert again.status == DELIVERY_COMPLETED
    assert [m.kind for m in healthy.sent] == ["text", "text", "document"]


def test_a_resend_that_lands_closes_the_day_and_suppresses(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT §15.12 + §15.3: the operator's resend is a delivery like any
    other. Before this, a successful resend left the run_date with NO state
    row — permanently, since the delivery is no longer PENDING_WINDOW and
    neither the descend query nor the stale sweep can reach it again — and
    wrote no suppression, so the same posting passed the gate the next night
    and the identical cached CV was sent a second time."""
    from career.whatsapp.delivery import resend_pending_delivery

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()
    assert _day_state(owner_session, ch.tenant_id) is None   # held: no state yet

    later = NOW + timedelta(minutes=5)
    ch.last_inbound_at = later                               # the window opens
    healthy = FakeWhatsAppClient()
    result = resend_pending_delivery(
        owner_session, tenant_id=ch.tenant_id, whatsapp_client=healthy, now=later,
    )
    owner_session.commit()

    assert result.status == DELIVERY_COMPLETED
    state = _day_state(owner_session, ch.tenant_id)
    assert state is not None, "a landed resend must close the day"
    assert state[0] == "DELIVERED"
    assert state[1]["delivered"] == 1
    # …and the delivered job never repeats tomorrow
    assert _suppressions(owner_session, ch.tenant_id) == 1


def test_a_closed_window_resend_refuses_without_burning_an_attempt(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT: the button existed for exactly the state it could not serve.
    Meta rejects every free-form message outside the 24h window, so each tap
    sent nothing and still spent one of three attempts — the third made the
    bundle terminal, and a customer who tapped the template later that day
    received nothing at all. A refusal now costs nothing."""
    from career.whatsapp.delivery import (
        RESEND_WINDOW_CLOSED,
        resend_pending_delivery,
    )

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    healthy = FakeWhatsAppClient()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=healthy, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()
    healthy.sent.clear()

    for i in range(5):        # more taps than the whole attempt budget
        result = resend_pending_delivery(
            owner_session, tenant_id=ch.tenant_id, whatsapp_client=healthy,
            now=NOW + timedelta(minutes=i + 1),
        )
        owner_session.commit()
        assert result.outcome == RESEND_WINDOW_CLOSED
        assert result.delivery is not None
        assert result.delivery.status == DELIVERY_PENDING
        assert "attempts" not in result.delivery.bundle    # budget untouched
    assert not healthy.sent                                # nothing attempted

    # the bundle is still fully claimable when the customer finally taps
    later = NOW + timedelta(hours=1)
    ch.last_inbound_at = later
    descended = descend_pending_delivery(
        owner_session, ch, whatsapp_client=healthy, now=later)
    owner_session.commit()
    assert descended is not None
    assert descended.status == DELIVERY_COMPLETED


def test_retries_are_bounded_and_then_close_honestly(
    owner_session: Session, clean_billing: None
) -> None:
    """Inside an OPEN window a resend is a real attempt — bounded at three,
    then the day closes on the truth: nothing was delivered, so the state is
    WHATSAPP_FAILED (never a silent, stateless day)."""
    from career.whatsapp.delivery import (
        DELIVERY_PARTIAL,
        MAX_DISPATCH_ATTEMPTS,
        RESEND_NO_BUNDLE,
        RESEND_SENT,
        resend_pending_delivery,
    )

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    ch.last_inbound_at = NOW + timedelta(minutes=1)   # the customer tapped
    owner_session.commit()

    result = None
    for i in range(MAX_DISPATCH_ATTEMPTS):
        result = resend_pending_delivery(
            owner_session, tenant_id=ch.tenant_id, whatsapp_client=dead,
            now=NOW + timedelta(minutes=i + 2),
        )
        owner_session.commit()
        assert result.outcome == RESEND_SENT

    assert result is not None
    assert result.status == DELIVERY_PARTIAL      # honest, not pending forever
    assert result.delivery is not None
    assert result.delivery.bundle["attempts"] == MAX_DISPATCH_ATTEMPTS
    assert result.delivered_groups == []          # nothing ever landed
    # the exhausted day closes honestly instead of staying stateless forever
    state = _day_state(owner_session, ch.tenant_id)
    assert state is not None and state[0] == "WHATSAPP_FAILED"
    assert _suppressions(owner_session, ch.tenant_id) == 0   # nothing delivered
    # and there is nothing left to resend
    assert resend_pending_delivery(
        owner_session, tenant_id=ch.tenant_id, whatsapp_client=dead,
        now=NOW + timedelta(days=1),
    ).outcome == RESEND_NO_BUNDLE


def test_a_resend_never_reaches_someone_who_opted_out(
    owner_session: Session, clean_billing: None
) -> None:
    from career.whatsapp.delivery import RESEND_OPTED_OUT, resend_pending_delivery

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    dead = _DeadDocuments()
    deliver_adaptive(owner_session, ch, _GROUPED, run_date=NOW.date(),
                     whatsapp_client=dead, daily_template=DAILY_UTILITY, now=NOW)
    ch.opt_out_at = NOW + timedelta(minutes=1)
    owner_session.commit()

    healthy = FakeWhatsAppClient()
    assert resend_pending_delivery(
        owner_session, tenant_id=ch.tenant_id, whatsapp_client=healthy,
        now=NOW + timedelta(hours=1),
    ).outcome == RESEND_OPTED_OUT
    assert not healthy.sent


# ── the provider's id must never take the ledger row down with it ────────────
#
# AUDIT 2026-08-06, second review. `salla/lifecycle._record_send` was taught to
# check Meta's message id before it enters the session — but `record_out` has
# twenty-two call sites and that fix guarded ONE. At every other site the row
# destroyed by a refused INSERT is a DELIVERY LEDGER row: the evidence that a
# customer was sent his CVs, the source `close.whatsapp_spend` bills from, and
# the rows `whatsapp/worker._handle_status` writes receipts onto.
#
# The reviewer's reproduction, at an unguarded delivery-shaped site:
#
#     UNGUARDED commit raised: DataError (StringDataRightTruncation)
#       value too long for type character varying(128)
#     ledger row that mattered, persisted: 0
#
# Both halves matter. The send REALLY HAPPENED — Meta delivered it and billed
# us — and the transaction that would have recorded it rolled back whole, so
# the delivery itself disappeared along with the row.


class _OverlongIds(FakeWhatsAppClient):
    """Meta answers with an id longer than the column that holds it.

    Nothing exotic is being simulated. `delivery_messages.wa_message_id` and
    `deliveries.template_message_id` are both ``varchar(128)``, the value in
    them is the PROVIDER's, and no code between the Graph response and the
    INSERT has ever looked at its length. Two hundred characters is only
    «longer than a hundred and twenty-eight».
    """

    def _next_id(self) -> str:
        self._n += 1
        return f"wamid.{self._n}." + "H" * 200


class _NotEvenAString(FakeWhatsAppClient):
    """The other shape a provider hands back: not a string at all.

    A client that returns the parsed response body instead of digging the id
    out of it is one refactor away at all times, and psycopg cannot adapt a
    dict — so this too is a commit that raises and a delivery that vanishes,
    without a single character of it being «too long».
    """

    def _next_id(self):  # type: ignore[no-untyped-def]  # noqa: ANN202
        self._n += 1
        return {"messages": [{"id": f"wamid.{self._n}"}]}


def _messages_of(owner_engine: Engine, delivery_id: uuid.UUID) -> list[DeliveryMessage]:
    with Session(owner_engine) as s:
        return list(s.execute(
            select(DeliveryMessage)
            .where(DeliveryMessage.delivery_id == delivery_id)
            .order_by(DeliveryMessage.created_at)
        ).scalars().all())


def test_an_overlong_provider_id_no_longer_destroys_the_delivery_ledger(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The reviewer's failure, at the site the last wave did not reach.

    An open window sends the bundle directly: two real sends, two ledger rows,
    one commit. Before the fix that commit raised `DataError` and took the
    `Delivery` row down with the two `DeliveryMessage` rows — the customer had
    his CVs, and we had no record that anything was ever sent to him.
    """
    ch = _channel(owner_session, last_inbound_at=NOW)  # open → direct send
    wa = _OverlongIds()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    assert [m.kind for m in wa.sent] == ["text", "document"]  # both really went
    assert delivery.status == DELIVERY_COMPLETED
    rows = _messages_of(owner_engine, delivery.id)
    assert [r.kind for r in rows] == ["text", "document"], (
        "the ledger rows for two sends Meta has already made did not survive "
        "the commit"
    )
    # NULL, never truncated: a truncated id is an id that matches the WRONG
    # send when a receipt arrives, which is worse than matching nothing.
    assert [r.wa_message_id for r in rows] == [None, None]


def test_an_overlong_id_no_longer_destroys_the_held_bundle(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The template path fails in TWO places, and only one of them is
    `record_out`.

    `deliver_adaptive` also stamps the same provider id onto
    `deliveries.template_message_id`, which is `varchar(128)` as well — so a
    fix that funnels every ledger write through one validated door and stops
    there still loses the whole morning template on this path. The delivery
    that is lost here is the HELD one: the customer gets a template, taps it,
    and there is no bundle to descend because the row that held it never
    committed.
    """
    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))  # closed
    wa = _OverlongIds()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    assert delivery.status == DELIVERY_PENDING
    assert delivery.template_message_id is None   # unusable → NULL, not refused
    rows = _messages_of(owner_engine, delivery.id)
    assert [r.kind for r in rows] == ["template"]
    assert rows[0].wa_message_id is None

    # …and the bundle it holds is still claimable, which is the whole point of
    # keeping the row: the tap arrives and the CVs go out.
    later = NOW + timedelta(minutes=5)
    ch.last_inbound_at = later
    descended = descend_pending_delivery(owner_session, ch, whatsapp_client=wa, now=later)
    owner_session.commit()
    assert descended is not None and descended.status == DELIVERY_COMPLETED


def test_an_id_that_is_not_a_string_is_refused_the_same_way(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """Length is not the only way a provider value poisons a transaction, and
    a guard that only measures `len()` would call this one «fine» and hand
    psycopg a dict."""
    ch = _channel(owner_session, last_inbound_at=NOW)
    wa = _NotEvenAString()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    rows = _messages_of(owner_engine, delivery.id)
    assert [r.kind for r in rows] == ["text", "document"]
    assert [r.wa_message_id for r in rows] == [None, None]


def test_a_null_id_is_billed_as_sent_forever_and_that_is_the_price(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The cost of the fix, asserted instead of promised.

    `worker._handle_status` matches receipts on `wa_message_id`. A row written
    with NULL can never be matched, so its status stays `sent` for good — and
    `close.whatsapp_spend` bills every template row whose status is not
    `failed`. This template is therefore billed even though Meta told us it
    FAILED. That over-reports spend, which is the safe direction, and it is
    the reason the row is kept rather than dropped: an unrecorded send is a
    template Meta charged us for and we counted at zero.

    If a later change makes this test fail, it has changed the money.
    """
    from career.cv.close import whatsapp_spend
    from career.whatsapp.worker import _handle_status

    ch = _channel(owner_session, last_inbound_at=NOW - timedelta(hours=25))
    wa = _OverlongIds()
    deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                     whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    owner_session.commit()

    # Meta reports the true id — the full, over-long one it gave us.
    real_id = wa.sent[-1].message_id
    assert len(real_id) > 128
    _handle_status(owner_session, {"id": real_id, "status": "failed"}, now=NOW)
    owner_session.commit()

    delivery_id = owner_session.execute(
        select(Delivery.id).where(Delivery.channel_id == ch.id)
    ).scalars().one()
    rows = _messages_of(owner_engine, delivery_id)
    assert [r.status for r in rows] == ["sent"]   # the receipt found nothing
    billed = whatsapp_spend(owner_session, tenant_id=ch.tenant_id)
    assert sum(count for count, _ in billed.values()) == 1


def test_a_ledger_failure_the_guard_cannot_prevent_still_fails_the_delivery(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The savepoint decision, made visible.

    `record_out` deliberately does NOT wrap its row in `begin_nested()`. What
    validation cannot prevent — a channel deleted underneath us, a tenant id
    that does not exist — is a ledger row we genuinely cannot write, and on
    THIS table that row is the evidence of the delivery (constant 3). Swallowing
    it would commit a delivery we cannot evidence and bill for a send we cannot
    show; failing the transaction refuses to claim it. `salla/lifecycle` needs
    the savepoint for the opposite reason — there one row's failure would
    discard a whole night of OTHER customers' marks.

    WHAT CHANGED ON 2026-08-10, and it is the other half of the same constant
    (CHANGELOG 39). The refused row still fails its transaction — that part is
    unchanged and is what the first assertion below pins. What it may no
    longer do is take the EVIDENCE OF REAL SENDS with it: `deliver_adaptive`
    commits at its durability point after the last send, so the two messages
    this customer actually received keep their rows and their `deliveries`
    parent, and the rollback discards exactly the one row that could not be
    written. The old assertions here — no messages at all, no delivery row —
    described the live incident of that morning rather than the behaviour we
    want: four messages on a real phone, and a ledger that said zero.
    """
    from career.whatsapp.delivery import record_out

    ch = _channel(owner_session, last_inbound_at=NOW)
    wa = FakeWhatsAppClient()
    delivery = deliver_adaptive(owner_session, ch, BUNDLE, run_date=NOW.date(),
                                whatsapp_client=wa, daily_template=DAILY_UTILITY, now=NOW)
    delivery_id = delivery.id
    record_out(owner_session, tenant_id=ch.tenant_id, channel_id=uuid.uuid4(),
               kind="text", wa_message_id="wamid.orphan", delivery_id=delivery_id,
               now=NOW)
    with pytest.raises(IntegrityError):
        owner_session.commit()
    owner_session.rollback()

    # the orphan row is gone — it is the only thing the rollback may cost
    ids = sorted(r.wa_message_id or "" for r
                 in _messages_of(owner_engine, delivery_id))
    assert ids == ["wamid.fake.1", "wamid.fake.2"]   # both real sends kept
    # and the delivery the customer really received is still on the books
    with Session(owner_engine) as s:
        assert s.execute(
            select(Delivery.id).where(Delivery.id == delivery_id)
        ).scalars().first() is not None
