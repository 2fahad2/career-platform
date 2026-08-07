"""§05 «التجديد والنهايات»: reminders d27/d29 → GRACE 48h → EXPIRED →
recovery after 7 days — one idempotent sweep, template sends guarded."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.lifecycle import sweep_subscription_lifecycle
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.activation_flow import activate
from career.whatsapp.client import FakeWhatsAppClient

_PR = {"prod_pro": (Decimal("279.00"), "SAR"), "prod_basic": (Decimal("149"), "SAR"),
       "prod_cv": (Decimal("29.00"), "SAR")}

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def _probe_phone() -> str:
    """A real Salla order ALWAYS carries the buyer's mobile — the whole
    zero-touch activation path keys on it. Test orders that omitted it were
    describing a shape the world never sends, and that unrealism is exactly
    how the phone defect survived: the suite was green while no real customer
    could have been activated."""
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _active_sub(owner_session: Session, *, period_end: datetime) -> uuid.UUID:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("279.00"), "SAR",
                       customer_phone=_probe_phone())
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_pro": "professional"},
                             expected_pricing=_PR)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    activate(owner_session, token=result.activation_token, from_phone=phone,
             display_name=None, now=NOW, whatsapp_client=FakeWhatsAppClient(),
             admin_client=FakeTelegramAdminClient())
    sub_id = uuid.UUID(str(result.subscription_id))
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'ACTIVE', current_period_end = :e"
        " WHERE id = :id"), {"e": period_end, "id": str(sub_id)})
    owner_session.commit()
    return sub_id


def _status(s: Session, sub_id: uuid.UUID) -> str:
    return s.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :id"),
        {"id": str(sub_id)}).scalar_one()


def test_day27_reminder_sent_once(owner_session: Session, clean_billing: None) -> None:
    sub_id = _active_sub(owner_session, period_end=NOW + timedelta(days=2, hours=12))
    wa = FakeWhatsAppClient()
    c1 = sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    c2 = sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    owner_session.commit()
    assert c1["reminded"] == 1 and c2["reminded"] == 0    # idempotent
    templates = [m for m in wa.sent if m.kind == "template"]
    assert len(templates) == 1
    assert templates[0].template_name == "renewal_reminder"
    assert _status(owner_session, sub_id) == "ACTIVE"


def test_period_end_enters_grace_then_48h_expires(
    owner_session: Session, clean_billing: None
) -> None:
    sub_id = _active_sub(owner_session, period_end=NOW - timedelta(hours=1))
    wa = FakeWhatsAppClient()
    sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    owner_session.commit()
    assert _status(owner_session, sub_id) == "GRACE"

    still_grace = NOW + timedelta(hours=40)
    sweep_subscription_lifecycle(owner_session, now=still_grace, whatsapp_client=wa)
    owner_session.commit()
    assert _status(owner_session, sub_id) == "GRACE"      # 48h not elapsed

    after = NOW + timedelta(hours=48)
    sweep_subscription_lifecycle(owner_session, now=after, whatsapp_client=wa)
    owner_session.commit()
    assert _status(owner_session, sub_id) == "EXPIRED"


def test_recovery_message_seven_days_after_expiry_once(
    owner_session: Session, clean_billing: None
) -> None:
    period_end = NOW - timedelta(days=10)
    sub_id = _active_sub(owner_session, period_end=period_end)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED' WHERE id = :id"),
        {"id": str(sub_id)})
    owner_session.commit()
    wa = FakeWhatsAppClient()
    c1 = sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    c2 = sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    owner_session.commit()
    assert c1["recovered"] == 1 and c2["recovered"] == 0
    assert [m.template_name for m in wa.sent if m.kind == "template"] == ["recovery"]
    assert _status(owner_session, sub_id) == "EXPIRED"


def test_cv_analysis_one_shot_is_untouched(
    owner_session: Session, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_cv", Decimal("29.00"), "SAR",
                       customer_phone=_probe_phone())
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_cv": "cv_analysis"},
                             expected_pricing=_PR)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'ACTIVE',"
        " current_period_end = :e WHERE id = :id"),
        {"e": NOW - timedelta(days=5), "id": str(result.subscription_id)})
    owner_session.commit()
    counts = sweep_subscription_lifecycle(owner_session, now=NOW)
    owner_session.commit()
    # the one-shot analysis product has no period to sweep, so EVERY counter
    # the sweep reports must be zero — asserted over whatever the sweep
    # currently counts, so a new counter cannot quietly start firing here
    assert set(counts) >= {"reminded", "graced", "expired", "recovered",
                           "unclaimed_reminded", "unclaimed_expired"}
    assert all(v == 0 for v in counts.values()), counts


def test_amount_or_currency_mismatch_never_provisions(
    owner_session: Session, clean_billing: None
) -> None:
    """§09 triple match: paid order with a tampered amount is refused."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("10.00"), "SAR",
                       customer_phone=_probe_phone())   # should be 279.00
    })
    result = provision_order(
        owner_session, order_id, salla_client=client,
        product_catalog={"prod_pro": "professional"},
        expected_pricing={"prod_pro": (Decimal("279.00"), "SAR")},
    )
    assert result.status == "amount_mismatch"
    count = owner_session.execute(sql_text(
        "SELECT count(*) FROM subscriptions WHERE salla_order_id = :o"),
        {"o": order_id}).scalar_one()
    assert count == 0

    # the honest price passes
    order2 = f"ORD-{uuid.uuid4()}"
    client2 = FakeSallaClient({
        order2: SallaOrder(order2, "paid", "prod_pro",
                           Decimal("279.00"), "SAR",
                       customer_phone=_probe_phone())
    })
    ok = provision_order(
        owner_session, order2, salla_client=client2,
        product_catalog={"prod_pro": "professional"},
        expected_pricing={"prod_pro": (Decimal("279.00"), "SAR")},
    )
    assert ok.status == "provisioned"


def test_unclaimed_subscription_expires_after_claim_deadline(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT ك-20: PAID_UNCLAIMED older than 7 days expires honestly."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("279.00"), "SAR",
                       customer_phone=_probe_phone())
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_pro": "professional"},
                             expected_pricing=_PR)
    owner_session.commit()
    # age the subscription 8 days back
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET created_at = now() - interval '8 days'"
        " WHERE id = :i"), {"i": result.subscription_id})
    owner_session.commit()
    counts = sweep_subscription_lifecycle(
        owner_session, now=datetime.now(UTC), whatsapp_client=None)
    owner_session.commit()
    assert counts["unclaimed_expired"] == 1
    status = owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE id = :i"),
        {"i": result.subscription_id}).scalar_one()
    assert status == "EXPIRED"


def test_unclaimed_gets_one_reminder_before_deadline(
    owner_session: Session, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro",
                             Decimal("279.00"), "SAR",
                             customer_phone="0555000111")
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_pro": "professional"},
                             expected_pricing=_PR)
    owner_session.commit()
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET created_at = now() - interval '6 days'"
        " WHERE id = :i"), {"i": result.subscription_id})
    owner_session.commit()
    wa = FakeWhatsAppClient()
    counts = sweep_subscription_lifecycle(
        owner_session, now=datetime.now(UTC), whatsapp_client=wa)
    owner_session.commit()
    assert counts["unclaimed_reminded"] == 1
    assert any(m.kind == "template" for m in wa.sent)
    # idempotent — second sweep sends nothing
    counts2 = sweep_subscription_lifecycle(
        owner_session, now=datetime.now(UTC), whatsapp_client=wa)
    assert counts2["unclaimed_reminded"] == 0


# ── the template spend that was never written down ───────────────────────────


def _template_rows(s: Session, tenant_id: str) -> list[tuple]:
    return list(s.execute(sql_text(
        "SELECT template_name, channel_id, status, wa_message_id FROM"
        " delivery_messages WHERE tenant_id = :t AND kind = 'template'"
        " ORDER BY created_at"), {"t": tenant_id}).all())


def _tenant_of(s: Session, sub_id: uuid.UUID) -> str:
    return str(s.execute(sql_text(
        "SELECT tenant_id FROM subscriptions WHERE id = :i"),
        {"i": str(sub_id)}).scalar_one())


def test_renewal_reminder_is_recorded_and_therefore_billed(
    owner_session: Session, clean_billing: None
) -> None:
    """The reminder Meta charges us for now exists in the ledger we bill from.

    Before this, `_channel_phone` loaded the whole channel, kept the phone and
    dropped the row, and `_send_template` received a message id and dropped
    that too — so a renewing customer's three lifecycle templates per period
    were billed by Meta and counted by nobody. `close.whatsapp_spend` derives
    the entire WhatsApp bill from `delivery_messages`, so this assertion is
    the money one: the send is visible to the operator's cost screen.
    """
    from career.cv import close as close_mod

    sub_id = _active_sub(owner_session, period_end=NOW + timedelta(days=2, hours=12))
    tenant_id = _tenant_of(owner_session, sub_id)
    wa = FakeWhatsAppClient()
    sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    owner_session.commit()

    rows = _template_rows(owner_session, tenant_id)
    assert [r.template_name for r in rows] == ["renewal_reminder"]
    # attributed to the channel it actually went to, and carrying the id Meta
    # will quote back in its delivery receipt
    assert rows[0].channel_id is not None
    assert rows[0].status == "sent"
    assert rows[0].wa_message_id == [
        m.message_id for m in wa.sent if m.kind == "template"
    ][0]

    spend = close_mod.whatsapp_spend(owner_session, tenant_id=uuid.UUID(tenant_id))
    # renewal_reminder is a UTILITY template — priced, not free
    assert spend["wa_utility"][0] == 1
    assert spend["wa_utility"][1] > 0


def test_recovery_and_grace_reminders_are_recorded_too(
    owner_session: Session, clean_billing: None
) -> None:
    """The other two channel-bound sites, so «three of five» is a fact."""
    graced = _active_sub(owner_session, period_end=NOW - timedelta(hours=1))
    lapsed = _active_sub(owner_session, period_end=NOW - timedelta(days=10))
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED' WHERE id = :id"),
        {"id": str(lapsed)})
    owner_session.commit()

    wa = FakeWhatsAppClient()
    sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
    owner_session.commit()

    assert [r.template_name for r in
            _template_rows(owner_session, _tenant_of(owner_session, graced))] \
        == ["renewal_reminder"]
    assert [r.template_name for r in
            _template_rows(owner_session, _tenant_of(owner_session, lapsed))] \
        == ["recovery"]


def test_claim_reminder_to_an_order_phone_is_recorded_with_no_channel(
    owner_session: Session, clean_billing: None
) -> None:
    """The structural half: this template fires BEFORE any channel exists.

    A buyer who pays and never activates has one CustomerChannel — none — and
    `delivery_messages.channel_id` was NOT NULL, so 100% of that order's
    template spend was unrecordable rather than merely unrecorded. 0028 made
    the column nullable for exactly this row.
    """
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"),
                             "SAR", customer_phone="0555000222")
    })
    result = provision_order(owner_session, order_id, salla_client=client,
                             product_catalog={"prod_pro": "professional"},
                             expected_pricing=_PR)
    owner_session.commit()
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET created_at = now() - interval '6 days'"
        " WHERE id = :i"), {"i": result.subscription_id})
    owner_session.commit()

    counts = sweep_subscription_lifecycle(
        owner_session, now=datetime.now(UTC),
        whatsapp_client=FakeWhatsAppClient())
    owner_session.commit()
    assert counts["unclaimed_reminded"] == 1

    rows = _template_rows(owner_session, str(result.tenant_id))
    assert [r.template_name for r in rows] == ["welcome_activation"]
    assert rows[0].channel_id is None       # nothing to point at, and that is fine


class _OverlongIdClient(FakeWhatsAppClient):
    """Meta returns, on ONE send of the night, a message id longer than the
    column that stores it. Everything else about this client is honest.

    This is the shape the probe used, and it is the cheapest way for a
    provider-controlled value to become an INSERT the database refuses.
    """

    def __init__(self, poison_call: int = 2) -> None:
        super().__init__()
        self.template_calls = 0
        self._poison = poison_call
        self.poisoned_phone: str | None = None

    def send_template(self, to_phone, template_name, language,  # type: ignore[no-untyped-def]  # noqa: ANN001,ANN201
                      variables=None, buttons=()):
        super().send_template(to_phone, template_name, language,
                              variables, buttons)
        self.template_calls += 1
        if self.template_calls == self._poison:
            self.poisoned_phone = to_phone
            return "wamid.HBg" + "X" * 200      # varchar(128) cannot hold it
        return self.sent[-1].message_id


def _template_phones(wa: FakeWhatsAppClient) -> list[str]:
    return [m.to_phone for m in wa.sent if m.kind == "template"]


def _sub_of_phone(s: Session, phone: str) -> str:
    return str(s.execute(sql_text(
        "SELECT sub.id FROM subscriptions sub JOIN customer_channels c"
        " ON c.tenant_id = sub.tenant_id WHERE c.phone_e164 = :p"),
        {"p": phone}).scalar_one())


def _marks(s: Session, sub_ids: list[uuid.UUID]) -> set[str]:
    return {str(r[0]) for r in s.execute(sql_text(
        "SELECT subscription_id FROM subscription_events WHERE event_type ="
        " 'renewal_reminder_d3' AND subscription_id::text = ANY(:ids)"),
        {"ids": [str(i) for i in sub_ids]})}


def test_one_unwritable_ledger_row_never_discards_the_nights_marks(
    owner_session: Session, clean_billing: None
) -> None:
    """«accounting never blocks the lifecycle» — the promise, now true.

    The guard around `record_out` could not fire: `record_out` only calls
    `session.add()`, so the INSERT is emitted by the NEXT flush — `_mark`'s,
    one line later and OUTSIDE the try. And `engine/cli.py` runs the whole
    sweep in ONE Session with ONE commit under one broad `except`, so a single
    ledger row the database refuses discarded the `subscription_events` marks
    of EVERY customer already processed that night. Meta had charged for those
    templates, the database recorded none of them, and the next night re-sent
    and re-paid for all of them.

    Reproduced exactly: three customers due a reminder, the second one's
    provider message id too long for its column, one session, one commit.
    """
    subs = [_active_sub(owner_session, period_end=NOW + timedelta(days=2, hours=12))
            for _ in range(3)]
    wa = _OverlongIdClient(poison_call=2)

    # the nightly caller's shape, verbatim (engine/cli.py)
    blew_up = False
    try:
        sweep_subscription_lifecycle(owner_session, now=NOW, whatsapp_client=wa)
        owner_session.commit()
    except Exception:  # noqa: BLE001 — exactly what cli.py does
        owner_session.rollback()
        blew_up = True

    assert blew_up is False
    phones = _template_phones(wa)
    assert len(phones) == 3
    assert wa.poisoned_phone == phones[1]

    # the customer served BEFORE the bad row keeps his mark — the blast radius
    marked = _marks(owner_session, subs)
    assert _sub_of_phone(owner_session, phones[0]) in marked
    # and so does everybody else, including the one whose ledger row was bad:
    # he was charged for, so tomorrow must not send and pay again
    assert len(marked) == 3

    # the send is still billed — with no receipt id, because the provider gave
    # us one we cannot store, and inventing a truncated one would be worse
    rows = list(owner_session.execute(sql_text(
        "SELECT wa_message_id FROM delivery_messages WHERE kind = 'template'"
        " AND tenant_id IN (SELECT tenant_id FROM subscriptions WHERE"
        " id::text = ANY(:ids))"),
        {"ids": [str(i) for i in subs]}).all())
    assert len(rows) == 3
    assert sum(1 for r in rows if r[0] is None) == 1


def test_a_ledger_row_the_database_refuses_is_rolled_back_alone(
    owner_session: Session, clean_billing: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The general case, not just the id length: ANY unwritable ledger row.

    Validation catches the value we know about; the savepoint catches the ones
    we do not. Here the row names a channel that does not exist — a foreign
    key the database refuses at flush — and the night's other marks must
    still be there afterwards.
    """
    from career.salla import lifecycle as lc

    subs = [_active_sub(owner_session, period_end=NOW + timedelta(days=2, hours=12))
            for _ in range(3)]
    real = lc.record_out
    calls = {"n": 0}

    def _poisoned(session, **kw):  # type: ignore[no-untyped-def]  # noqa: ANN001,ANN202
        calls["n"] += 1
        if calls["n"] == 2:
            kw = {**kw, "channel_id": uuid.uuid4()}   # no such channel
        return real(session, **kw)

    monkeypatch.setattr(lc, "record_out", _poisoned)

    blew_up = False
    try:
        sweep_subscription_lifecycle(owner_session, now=NOW,
                                     whatsapp_client=FakeWhatsAppClient())
        owner_session.commit()
    except Exception:  # noqa: BLE001
        owner_session.rollback()
        blew_up = True

    assert blew_up is False
    assert len(_marks(owner_session, subs)) == 3
    # the refused row is gone; the two writable ones are there
    written = owner_session.execute(sql_text(
        "SELECT count(*) FROM delivery_messages WHERE kind = 'template' AND"
        " tenant_id IN (SELECT tenant_id FROM subscriptions WHERE"
        " id::text = ANY(:ids))"),
        {"ids": [str(i) for i in subs]}).scalar_one()
    assert written == 2


def test_a_template_that_failed_to_send_is_never_billed(
    owner_session: Session, clean_billing: None
) -> None:
    """The mirror image of the hole, and the reason the row is written AFTER
    the client returns: a bill we were never charged is as wrong as one we
    were charged and never counted."""
    class _Refusing(FakeWhatsAppClient):
        def send_template(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("meta refused")

    sub_id = _active_sub(owner_session, period_end=NOW + timedelta(days=2, hours=12))
    tenant_id = _tenant_of(owner_session, sub_id)
    counts = sweep_subscription_lifecycle(
        owner_session, now=NOW, whatsapp_client=_Refusing())
    owner_session.commit()
    assert counts["reminded"] == 0
    assert _template_rows(owner_session, tenant_id) == []
