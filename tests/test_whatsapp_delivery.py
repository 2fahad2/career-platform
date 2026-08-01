"""Adaptive delivery (DB) — open→direct, closed→template-then-descend, opted-out→none."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, DeliveryMessage
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


def _channel(owner_session: Session, *, last_inbound_at: datetime | None, opt_out: bool = False
             ) -> CustomerChannel:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR")
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
