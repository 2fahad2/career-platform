"""Salla provisioning + lifecycle (DB) — the C3 exit-condition behaviors.

One subscription per paid order despite duplicate events; provisioning only on
paid; refund suspends immediately.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.session import SessionLocal
from career.salla import provisioning
from career.salla import subscriptions as st
from career.salla import tokens as salla_tokens
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import (
    ProvisionStatus,
    process_pending_webhooks,
    provision_order,
)
from career.salla.signature import compute_signature
from career.salla.webhook import receive_webhook
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp import activation_flow as flow
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.worker import _handle_message
from tests.test_salla_tokens import (
    FAKE_ACCESS,
    FAKE_ACCESS_2,
    FAKE_REFRESH,
    FAKE_REFRESH_2,
    RawAdmin,
    authorize_payload,
    isolated_env_file,  # noqa: F401 — autouse: redirects tokens.ENV_FILE
    row,
    seed,
    sweep,
)

SECRET = "test_secret_123"
CATALOG = {"prod_pro": "professional", "prod_riyal": "basic"}
# AUDIT ح-4: the gate fails closed for cataloged products without pricing
PRICING = {"prod_pro": (Decimal("279.00"), "SAR"),
           "prod_riyal": (Decimal("1.00"), "SAR")}


def _probe_phone() -> str:
    """A real Salla order ALWAYS carries the buyer's mobile — the whole
    zero-touch activation path keys on it. A test order without one describes
    a shape the world never sends, and that unrealism is how the phone defect
    survived: the suite was green while no real customer could be activated."""
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _order(order_id: str, *, status: str = "paid", product: str = "prod_pro",
           amount: str = "279.00", currency: str = "SAR") -> SallaOrder:
    return SallaOrder(order_id=order_id, status=status, product_id=product,
                      amount=Decimal(amount), currency=currency,
                      customer_phone=_probe_phone())


def _sub_count(owner_engine: Engine, order_id: str) -> int:
    with Session(owner_engine) as s:
        return s.execute(
            text("SELECT count(*) FROM subscriptions WHERE salla_order_id = :o"),
            {"o": order_id},
        ).scalar_one()


def test_paid_order_provisions_one_subscription(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    result = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    assert result.status is ProvisionStatus.PROVISIONED
    assert result.activation_token  # raw token returned once
    assert _sub_count(owner_engine, order_id) == 1

    with Session(owner_engine) as s:
        row = s.execute(
            text("SELECT status, plan_code, amount_sar, currency FROM subscriptions"
                 " WHERE salla_order_id = :o"), {"o": order_id},
        ).one()
        assert row.status == st.PAID_UNCLAIMED
        assert row.plan_code == "professional"
        assert row.currency == "SAR"
        # Activation token stored as a hash of the returned raw token, never raw.
        token_hash = s.execute(
            text("SELECT token_hash FROM activation_tokens at"
                 " JOIN subscriptions sub ON sub.id = at.subscription_id"
                 " WHERE sub.salla_order_id = :o"), {"o": order_id},
        ).scalar_one()
        assert token_hash == hashlib.sha256(result.activation_token.encode()).hexdigest()


def test_duplicate_provision_is_idempotent(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    first = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    second = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    assert first.status is ProvisionStatus.PROVISIONED
    assert second.status is ProvisionStatus.ALREADY_PROVISIONED
    assert second.subscription_id == first.subscription_id
    assert _sub_count(owner_engine, order_id) == 1  # never a second subscription


def test_unpaid_order_not_provisioned(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id, status="pending")})
    result = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    assert result.status is ProvisionStatus.NOT_PAID
    assert _sub_count(owner_engine, order_id) == 0


def test_unknown_product_ignored(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id, product="prod_unknown")})
    result = provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)
    assert result.status is ProvisionStatus.UNKNOWN_PRODUCT
    assert _sub_count(owner_engine, order_id) == 0


def test_order_not_found_fails(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    result = provision_order(
        owner_session, order_id, salla_client=FakeSallaClient({}), product_catalog=CATALOG,
    )
    assert result.status is ProvisionStatus.ORDER_NOT_FOUND
    assert _sub_count(owner_engine, order_id) == 0


def _post_webhook(order_id: str, event: str) -> None:
    body = json.dumps({"event": event, "data": {"id": order_id}}).encode("utf-8")
    sig = compute_signature(body, SECRET)
    s = SessionLocal()
    try:
        receive_webhook(s, provider="salla", event_type=event,
                        raw_body=body, header_signature=sig, secret=SECRET)
    finally:
        s.close()


def test_end_to_end_duplicate_webhook_one_subscription(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    # Two identical paid webhooks arrive (Salla retry) → deduped at intake.
    _post_webhook(order_id, "order.payment.updated")
    _post_webhook(order_id, "order.payment.updated")
    results = process_pending_webhooks(
        owner_session, salla_client=client, product_catalog=CATALOG, expected_pricing=PRICING,
    )
    provisioned = [r for r in results if r.status is ProvisionStatus.PROVISIONED]
    assert len(provisioned) == 1
    assert _sub_count(owner_engine, order_id) == 1


def test_refund_suspends_immediately(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    provision_order(owner_session, order_id, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)

    _post_webhook(order_id, "order.refunded")
    process_pending_webhooks(owner_session, salla_client=client, product_catalog=CATALOG,
    expected_pricing=PRICING)

    with Session(owner_engine) as s:
        status = s.execute(
            text("SELECT status FROM subscriptions WHERE salla_order_id = :o"),
            {"o": order_id},
        ).scalar_one()
    assert status == st.REFUNDED  # service off immediately


def test_cataloged_product_with_no_pricing_fails_closed(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT ح-4: a paid order for a cataloged product with NO pricing entry
    must NOT provision — it fails to manual review (was: check skipped)."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    result = provision_order(
        owner_session, order_id, salla_client=client,
        product_catalog=CATALOG, expected_pricing={},
    )
    assert result.status is ProvisionStatus.AMOUNT_MISMATCH


def test_bank_transfer_confirmation_event_provisions(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT ك-18: order.status.updated (merchant confirms the transfer)
    must provision — it was ignored forever."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    _post_webhook(order_id, "order.status.updated")
    results = process_pending_webhooks(
        owner_session, salla_client=client, product_catalog=CATALOG,
        expected_pricing=PRICING)
    assert results and results[0].status is ProvisionStatus.PROVISIONED


class TestTheAdminChannelHoldsNoCredential:
    """The activation deep link used to be posted to the admin Telegram
    channel on every fresh provision, live for seven days. The DB never stored
    the raw token — the chat log did, forever, and anyone reading the channel
    could bind any phone to that paid subscription. These tests pin that the
    announcement carries no credential and that the operator kept the
    capability it was standing in for."""

    def test_new_subscription_announcement_carries_no_raw_token(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        from career.salla.provisioning import _announce_provision
        from career.telegram.admin import FakeTelegramAdminClient

        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({order_id: _order(order_id)})
        result = provision_order(
            owner_session, order_id, salla_client=client,
            product_catalog=CATALOG, expected_pricing=PRICING,
        )
        assert result.activation_token

        admin = FakeTelegramAdminClient()
        _announce_provision(
            owner_session, result, admin_client=admin,
            whatsapp_number_e164="+966500000000", whatsapp_client=None,
        )

        assert admin.messages, "the operator must still hear about a sale"
        blob = "\n".join(admin.messages)
        assert result.activation_token not in blob
        assert "wa.me" not in blob
        assert "?text=" not in blob      # the pre-filled deep-link parameter
        # The prose may well say «التفعيل»; what must never appear is the
        # activation PHRASING that a WhatsApp client would send verbatim.
        from career.salla.activation_link import activation_message

        assert activation_message(result.activation_token) not in blob

    def test_operator_can_still_issue_a_link_for_an_unclaimed_order(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        from career.salla.provisioning import (
            LinkIssueStatus,
            issue_activation_link,
        )
        from career.whatsapp.inbound import InboundKind, classify_inbound

        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({order_id: _order(order_id)})
        result = provision_order(
            owner_session, order_id, salla_client=client,
            product_catalog=CATALOG, expected_pricing=PRICING,
        )
        code = owner_session.execute(
            text("SELECT code FROM tenants WHERE id::text = :t"),
            {"t": result.tenant_id},
        ).scalar_one()

        issued = issue_activation_link(
            owner_session, tenant_code=code,
            whatsapp_number_e164="+966500000000",
        )

        assert issued.status is LinkIssueStatus.ISSUED
        assert issued.link and issued.link.startswith("https://wa.me/966500000000")
        # The link the operator hands over must actually activate: the
        # pre-filled text has to survive the inbound classifier.
        from urllib.parse import parse_qs, urlparse

        prefilled = parse_qs(urlparse(issued.link).query)["text"][0]
        kind, token = classify_inbound(prefilled)
        assert kind is InboundKind.ACTIVATION
        assert token is not None

    def test_issuing_retires_the_previous_token(
        self, owner_session: Session, owner_engine: Engine, clean_billing: None
    ) -> None:
        """Two live tokens on one subscription is not merely untidy: the
        zero-touch claim path selects the single unused token with
        scalar_one_or_none, so a leftover would raise inside the WhatsApp
        worker and lock that customer out of activating at all."""
        from career.salla.provisioning import issue_activation_link

        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({order_id: _order(order_id)})
        result = provision_order(
            owner_session, order_id, salla_client=client,
            product_catalog=CATALOG, expected_pricing=PRICING,
        )
        code = owner_session.execute(
            text("SELECT code FROM tenants WHERE id::text = :t"),
            {"t": result.tenant_id},
        ).scalar_one()
        first = issue_activation_link(
            owner_session, tenant_code=code, whatsapp_number_e164="+966500000000")
        second = issue_activation_link(
            owner_session, tenant_code=code, whatsapp_number_e164="+966500000000")

        assert first.link != second.link
        with Session(owner_engine) as s:
            unused = s.execute(
                text("SELECT count(*) FROM activation_tokens at"
                     " JOIN subscriptions sub ON sub.id = at.subscription_id"
                     " WHERE sub.salla_order_id = :o AND at.used_at IS NULL"),
                {"o": order_id},
            ).scalar_one()
        assert unused == 1

    def test_issued_link_is_short_lived(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        from career.salla.provisioning import (
            ACTIVATION_TOKEN_TTL_DAYS,
            OPERATOR_LINK_TTL_MINUTES,
            issue_activation_link,
        )

        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({order_id: _order(order_id)})
        result = provision_order(
            owner_session, order_id, salla_client=client,
            product_catalog=CATALOG, expected_pricing=PRICING,
        )
        code = owner_session.execute(
            text("SELECT code FROM tenants WHERE id::text = :t"),
            {"t": result.tenant_id},
        ).scalar_one()

        issued = issue_activation_link(
            owner_session, tenant_code=code, whatsapp_number_e164="+966500000000")

        assert issued.expires_at is not None
        from datetime import UTC, datetime

        minutes = (issued.expires_at - datetime.now(UTC)).total_seconds() / 60
        assert 0 < minutes <= OPERATOR_LINK_TTL_MINUTES
        assert OPERATOR_LINK_TTL_MINUTES < ACTIVATION_TOKEN_TTL_DAYS * 24 * 60

    def test_no_link_for_an_account_that_is_already_activated(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """Once the order is claimed there is nothing to hand over, and a link
        minted anyway would be a way INTO a live account."""
        from career.salla.provisioning import (
            LinkIssueStatus,
            issue_activation_link,
        )

        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({order_id: _order(order_id)})
        result = provision_order(
            owner_session, order_id, salla_client=client,
            product_catalog=CATALOG, expected_pricing=PRICING,
        )
        owner_session.execute(
            text("UPDATE subscriptions SET status = :s WHERE salla_order_id = :o"),
            {"s": st.ACTIVE, "o": order_id},
        )
        owner_session.commit()
        code = owner_session.execute(
            text("SELECT code FROM tenants WHERE id::text = :t"),
            {"t": result.tenant_id},
        ).scalar_one()

        issued = issue_activation_link(
            owner_session, tenant_code=code, whatsapp_number_e164="+966500000000")

        assert issued.status is LinkIssueStatus.ALREADY_ACTIVATED
        assert issued.link is None

    def test_unknown_tenant_code_yields_nothing(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        from career.salla.provisioning import (
            LinkIssueStatus,
            issue_activation_link,
        )

        issued = issue_activation_link(
            owner_session, tenant_code="TEN-9999",
            whatsapp_number_e164="+966500000000")

        assert issued.status is LinkIssueStatus.NO_UNCLAIMED_ORDER
        assert issued.link is None


def test_tenant_code_survives_deletion_gap(
    owner_session: Session, clean_billing: None
) -> None:
    """AUDIT ك-19: with tenants {2,7} present, the next code is TEN-0008 —
    never a reissued TEN-0003 (row-count scheme) that explodes the unique
    constraint."""
    from career.db.models import Tenant as _T
    for n in (2, 7):
        owner_session.add(_T(id=uuid.uuid4(), code=f"TEN-{n:04d}"))
    owner_session.commit()
    try:
        from career.salla.provisioning import _next_tenant_code
        assert _next_tenant_code(owner_session) == "TEN-0008"
    finally:
        from sqlalchemy import text as _t
        owner_session.execute(_t(
            "DELETE FROM tenants WHERE code IN ('TEN-0002', 'TEN-0007')"))
        owner_session.commit()


# ── the zero-touch welcome: billed by Meta, recorded by nobody ───────────────


def test_zero_touch_welcome_template_is_recorded_before_any_channel_exists(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The 100% case, end to end.

    A buyer pays and receives exactly one approved template — the welcome — and
    their REPLY to it is what creates the CustomerChannel. So the send is
    guaranteed to happen while no channel exists, `delivery_messages.channel_id`
    was NOT NULL, and an order that never activates was billed by Meta for
    every template it received and counted by us for none of them. 0028 made
    the column nullable; this is the row that could not be written.
    """
    from career.whatsapp.client import FakeWhatsAppClient

    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    wa = FakeWhatsAppClient()
    _post_webhook(order_id, "order.payment.updated")
    process_pending_webhooks(
        owner_session, salla_client=client, product_catalog=CATALOG,
        expected_pricing=PRICING, whatsapp_client=wa,
    )

    with Session(owner_engine) as s:
        row = s.execute(text(
            "SELECT dm.kind, dm.template_name, dm.channel_id, dm.wa_message_id"
            " FROM delivery_messages dm JOIN subscriptions sub"
            " ON sub.tenant_id = dm.tenant_id"
            " WHERE sub.salla_order_id = :o"), {"o": order_id}).one()
    assert row.kind == "template"
    assert row.template_name == "welcome_activation"
    assert row.channel_id is None       # there is no channel yet, by design
    assert row.wa_message_id == [
        m.message_id for m in wa.sent if m.kind == "template"
    ][0]


# ── constant 4, written down ─────────────────────────────────────────────────


def _audit(owner_engine: Engine, order_id: str) -> list:
    with Session(owner_engine) as s:
        return list(s.execute(text(
            "SELECT a.action, a.actor, a.details FROM audit_events a"
            " JOIN subscriptions sub ON sub.tenant_id = a.tenant_id"
            " WHERE sub.salla_order_id = :o ORDER BY a.created_at"),
            {"o": order_id}).all())


def test_provisioning_records_that_the_order_was_re_verified(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """Constant 4 says a subscription is created only on ``payment = paid``
    after a re-check against the Salla API. The check was always made and never
    recorded, so «did we re-verify this order?» had no answer — for a money
    control, «we always do» is not evidence."""
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    provision_order(owner_session, order_id, salla_client=client,
                    product_catalog=CATALOG, expected_pricing=PRICING)

    rows = _audit(owner_engine, order_id)
    assert [r.action for r in rows] == ["subscription_provisioned"]
    details = rows[0].details
    assert details["salla_order_id"] == order_id
    assert details["plan_code"] == "professional"
    assert details["reverified_status"] == "paid"
    assert details["renewal"] is False
    # §15.13 — the audit row names ids and amounts, never a human
    assert "customer_phone" not in details
    assert not any("+9665" in str(v) for v in details.values())


def test_a_reversal_is_audited_once_however_often_it_is_redelivered(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The money went back — the one money event with no record at all.

    Also asserts the idempotency: ``apply_order_lifecycle`` leaves an
    already-terminal subscription untouched, so a redelivered refund webhook
    must not grow a second «the money went back» row.
    """
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({order_id: _order(order_id)})
    provision_order(owner_session, order_id, salla_client=client,
                    product_catalog=CATALOG, expected_pricing=PRICING)

    _post_webhook(order_id, "order.refunded")
    process_pending_webhooks(owner_session, salla_client=client,
                             product_catalog=CATALOG, expected_pricing=PRICING)
    _post_webhook(order_id, "order.refunded")
    process_pending_webhooks(owner_session, salla_client=client,
                             product_catalog=CATALOG, expected_pricing=PRICING)

    rows = _audit(owner_engine, order_id)
    actions = [r.action for r in rows]
    assert actions.count("subscription_refunded") == 1
    refund = next(r for r in rows if r.action == "subscription_refunded")
    assert refund.details["event_type"] == "order.refunded"
    assert refund.details["status_after"] == st.REFUNDED
    assert refund.details["source"] == "lifecycle_webhook"


# ── the credential event: whose token is this, and what is the operator told ──
#
# ADVERSARIAL REVIEW 2026-08-07, defects C and D. Both were proven on the LIVE
# sale path — the file on this host holds store 1275699954's credential and it
# is carrying real orders — so both repros are kept here rather than in a
# scratch file. They live beside the provisioning tests because both defects
# are in `_apply_authorize`: `tokens.store_credentials` answered correctly in
# each case and its caller mishandled the answer.

DEMO_STORE = "855028708"      # the store an accidental reinstall comes from
REAL_STORE = "1275699954"     # the store this platform actually sells from
_REAL_SECRETS_FILE = pathlib.Path("/root/career/.env.staging")


def _hold_the_live_credential() -> None:
    """Put a credential for REAL_STORE in the (redirected) secrets file.

    The assertion is not ceremony. These tests write credentials, the real
    file two directories up holds a live token for a real store, and the only
    thing standing between them is an autouse fixture imported from another
    module. If that import is ever dropped, this fails loudly instead of
    rewriting the credential that is carrying the company's orders.
    """
    assert salla_tokens.ENV_FILE != _REAL_SECRETS_FILE, (
        "the isolation fixture is not active — refusing to touch the real "
        "secrets file"
    )
    salla_tokens.store_credentials(
        FAKE_ACCESS, refresh_token=FAKE_REFRESH,
        expires_at=datetime(2026, 8, 21, 7, 24, 22, tzinfo=UTC),
        store_id=REAL_STORE,
    )


def _resweep(session: Session, admin: RawAdmin) -> None:
    """A pass of the worker that does NOT reset the alert timers.

    ``sweep`` calls ``reset_salla_backoff`` first, which is right for a test
    that wants a guaranteed first alert and wrong for one that is asking what
    the SECOND and THIRD alerts say.
    """
    process_pending_webhooks(
        session, salla_client=FakeSallaClient({}),
        product_catalog={"prod_pro": "professional"}, expected_pricing={},
        admin_client=admin,
    )


class TestACredentialWeCannotAttributeIsNeverStored:
    """Defect C. `payload.get("merchant")` missing → store_id=None →
    `StoreIdentity.UNCLAIMED`, whose docstring says «Not ignorance — the
    refresh path passes the store id it read back from the file» → treated as
    SAME → the live credential replaced, the row marked processed, the green
    «a new token landed» alert sent.

    The signature proves Salla sent it. It does not prove which store it is
    for: Salla signs every store's authorize with the same app secret, so the
    merchant id is the only discriminator in the body.
    """

    def test_an_authorize_without_a_merchant_does_not_replace_the_credential(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold_the_live_credential()
        admin = RawAdmin()
        event_id = seed(owner_session, payload=authorize_payload(
            access=FAKE_ACCESS_2, refresh=FAKE_REFRESH_2, merchant=None))

        sweep(owner_session, admin)

        held = salla_tokens.current()
        assert held.access_token == FAKE_ACCESS, (
            "a signed authorize event carrying no merchant id silently "
            f"replaced the live credential of store {REAL_STORE}; row "
            f"status={row(owner_session, event_id).processing_status}, "
            f"alerts={admin.messages!r}"
        )
        assert held.store_id == REAL_STORE

    def test_the_unattributable_row_is_terminal_and_loud(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """Terminal because the row keeps the payload it arrived with: a retry
        re-reads the same absent key forever. Loud because this is the sale
        path and nothing else in the system reports it — and the message
        carries the event id, which is how a `failed` row is still consumable
        by hand (`consume_stored_authorize.py --event-id`)."""
        _hold_the_live_credential()
        admin = RawAdmin()
        event_id = seed(owner_session, payload=authorize_payload(
            access=FAKE_ACCESS_2, refresh=FAKE_REFRESH_2, merchant=None))

        sweep(owner_session, admin)

        after = row(owner_session, event_id)
        assert after.processing_status == "failed"
        assert after.payload["data"]["access_token"] == FAKE_ACCESS_2  # kept
        text_ = "\n".join(admin.messages)
        assert "🔴" in text_
        assert "🟢" not in text_, "nothing landed; the green alert is a lie"
        assert str(event_id) in text_
        assert "consume_stored_authorize.py" in text_
        for secret in (FAKE_ACCESS, FAKE_ACCESS_2, FAKE_REFRESH,
                       FAKE_REFRESH_2):
            assert secret not in text_

    def test_the_app_id_is_never_borrowed_as_a_store_id(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """`data.id` is present on every delivery and is the APP id
        (1410006361). Reading it would give every store the same label —
        always present, always equal, always wrong."""
        _hold_the_live_credential()
        payload = authorize_payload(access=FAKE_ACCESS_2,
                                    refresh=FAKE_REFRESH_2, merchant=None)
        assert payload["data"]["id"] == 1410006361

        sweep(owner_session, RawAdmin())

        assert salla_tokens.current().store_id == REAL_STORE

    @pytest.mark.parametrize("merchant", [None, "", " ", {}, [], True,
                                          "two words", "line\nbreak"])
    def test_nothing_that_is_not_an_identifier_is_read_as_one(
        self, owner_session: Session, clean_billing: None, merchant
    ) -> None:
        """A dict or a bool would have gone through `str()` into the env file
        as a stored «store id» — a value that answers «is this the same
        grant?» with a shape, forever."""
        _hold_the_live_credential()
        payload = authorize_payload(access=FAKE_ACCESS_2,
                                    refresh=FAKE_REFRESH_2, merchant=None)
        if merchant is not None:
            payload["merchant"] = merchant
        event_id = seed(owner_session, payload=payload)

        sweep(owner_session, RawAdmin())

        assert salla_tokens.current().access_token == FAKE_ACCESS
        assert row(owner_session, event_id).processing_status == "failed"

    @pytest.mark.parametrize("shape", ["top", "nested", "object", "store_id"])
    def test_the_merchant_is_found_wherever_salla_puts_it(
        self, owner_session: Session, clean_billing: None, shape: str
    ) -> None:
        """The guard must not turn a renamed key into a stopped sale path, so
        the reader looks in every place whose NAME can only mean a store —
        including `merchant` arriving as Salla's merchant OBJECT."""
        payload = authorize_payload(merchant=None)
        if shape == "top":
            payload["merchant"] = int(REAL_STORE)      # today's live shape
        elif shape == "nested":
            payload["data"]["merchant"] = int(REAL_STORE)
        elif shape == "object":
            payload["merchant"] = {"id": int(REAL_STORE), "domain": "axestore"}
        else:
            payload["store_id"] = REAL_STORE
        event_id = seed(owner_session, payload=payload)

        sweep(owner_session, RawAdmin())

        assert salla_tokens.current().store_id == REAL_STORE
        assert row(owner_session, event_id).processing_status == "processed"

    def test_the_operator_can_still_take_it_by_hand(
        self, owner_session: Session, clean_billing: None,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal is automatic, not absolute. `consume_stored_authorize.py`
        binds the operator's `--allow-store-change` onto `store_credentials`
        and calls this very function; that flag already means «I accept an
        identity that cannot be proved» (the script treats an unrecorded store
        as a crossing needing it), so the same signature answers a payload
        that names no store. Without this, the alert above would send him to a
        command that refuses him."""
        from functools import partial

        _hold_the_live_credential()
        monkeypatch.setattr(
            salla_tokens, "store_credentials",
            partial(salla_tokens.store_credentials, allow_store_change=True),
        )
        event_id = seed(owner_session, payload=authorize_payload(
            access=FAKE_ACCESS_2, refresh=FAKE_REFRESH_2, merchant=None,
            expires=int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())))

        sweep(owner_session, RawAdmin())

        assert salla_tokens.current().access_token == FAKE_ACCESS_2
        assert salla_tokens.current().store_id == REAL_STORE   # label kept
        assert row(owner_session, event_id).processing_status == "processed"


class TestTheForeignStoreRefusalSaysWhatToDo:
    """Defect D. `ForeignStoreCredential` subclasses `CredentialWriteError` on
    purpose — that keeps it out of the green «a new token landed» branch and
    must stay — but it therefore inherited the write-failure alert, whose only
    actionable sentence is «راجع مساحة القرص». Nothing is wrong with the disk,
    the retry can never succeed on its own, and the text never changed however
    long the operator ignored it.
    """

    def _refused(self, session: Session) -> uuid.UUID:
        _hold_the_live_credential()
        return seed(session, payload=authorize_payload(
            access=FAKE_ACCESS_2, refresh=FAKE_REFRESH_2,
            merchant=DEMO_STORE))

    def test_the_operator_is_not_sent_to_check_disk_space(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        admin = RawAdmin()
        self._refused(owner_session)

        sweep(owner_session, admin)

        assert admin.messages, "a refused credential must reach the operator"
        text_ = "\n".join(admin.messages)
        assert "مساحة القرص" not in text_, (
            f"a foreign-store refusal is reported as a disk problem: {text_!r}"
        )

    def test_it_names_both_stores_the_command_and_no_credential(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        admin = RawAdmin()
        event_id = self._refused(owner_session)

        sweep(owner_session, admin)

        text_ = "\n".join(admin.messages)
        assert REAL_STORE in text_ and DEMO_STORE in text_
        assert "consume_stored_authorize.py" in text_
        assert "--allow-store-change" in text_
        assert str(event_id) in text_
        assert "🟢" not in text_
        for secret in (FAKE_ACCESS, FAKE_ACCESS_2, FAKE_REFRESH,
                       FAKE_REFRESH_2):
            assert secret not in text_
        # nothing written, nothing lost: the row still holds the credential
        after = row(owner_session, event_id)
        assert after.processing_status == "received"
        assert after.attempt_count == 1
        assert after.payload["data"]["access_token"] == FAKE_ACCESS_2
        assert salla_tokens.current().access_token == FAKE_ACCESS

    def test_the_repeat_escalates_instead_of_saying_the_same_thing(
        self, owner_session: Session, clean_billing: None,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This state persists until a human chooses, so a message that never
        changes is a message that stops being read. The interval stays at
        twelve hours — nothing is broken while the question is open — but the
        second says «still», with an age and a count, and the third names the
        other way out: remove the app from that store."""
        monkeypatch.setitem(provisioning._NOTIFY_INTERVALS,
                            "authorize_foreign", 0.0)
        admin = RawAdmin()
        self._refused(owner_session)
        sweep(owner_session, admin)      # first pass, timers cleared

        _resweep(owner_session, admin)   # 12h later
        _resweep(owner_session, admin)   # a full day later

        assert len(admin.messages) == 3
        first, reminder, still_open = admin.messages
        assert reminder != first and still_open != reminder
        assert "🔁" in reminder and "تذكير" in reminder
        assert "أزل التطبيق" not in first
        assert "أزل التطبيق" in still_open, (
            "the third message repeats rather than escalating: "
            f"{still_open!r}"
        )
        for message in admin.messages:
            assert "consume_stored_authorize.py" in message
            assert REAL_STORE in message and DEMO_STORE in message

    def test_a_real_write_failure_still_says_disk(
        self, owner_session: Session, clean_billing: None,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The disk sentence was not wrong, it was misapplied. It must survive
        for the case it was written for — and that case must no longer be
        silenced by a pending store decision holding the shared timer."""
        def explode(*_a, **_k):
            raise salla_tokens.CredentialWriteError("could not persist")

        admin = RawAdmin()
        self._refused(owner_session)
        sweep(owner_session, admin)                  # a store decision opens
        monkeypatch.setattr(salla_tokens, "store_credentials", explode)
        seed(owner_session, payload=authorize_payload(merchant=REAL_STORE))

        _resweep(owner_session, admin)

        assert "مساحة القرص" in admin.messages[-1]


# ── §16 renewal: the money arrives again while his messages are off ──────────
#
# ADVERSARIAL REVIEW 2026-08-07, defect D2.1. Activation now notices a silenced
# channel and raises a ticket — but activation only happens on the §04 upgrade
# or a re-buy, which is the RARE route. The common one is this file's: a
# customer silences us WITHOUT cancelling his billing, so Salla charges him
# every 28 days forever and `provision_order`'s RENEWED branch never once looks
# at `opt_out_at`. Every night after closes SKIPPED_OPTED_OUT, which
# `engine.cli` calls honest, so the run exits 0 — the only alert he ever
# produced was the opt-out page a month earlier.

_RENEW_NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def _opted_out_pages(admin: FakeTelegramAdminClient) -> list[str]:
    """Matched on the alert's own first line rather than a phrase, so the count
    cannot quietly start including the worker's «إيقاف» page, which is a
    different message about the same subject."""
    return [
        m for m in admin.messages
        if m.splitlines()[:1] == [flow._OPTED_OUT_ALERT_HEAD]
    ]


def _channel_tickets(session: Session, phone: str) -> list:
    return session.execute(
        text("SELECT e.kind, e.status FROM support_events e"
             " JOIN customer_channels c ON c.id = e.channel_id"
             " WHERE c.phone_e164 = :p"),
        {"p": phone},
    ).all()


def _paying_customer(
    session: Session, wa: FakeWhatsAppClient, admin: FakeTelegramAdminClient,
) -> tuple[str, str, str]:
    """A real ACTIVE subscriber: paid, activated by token, period running."""
    phone = _probe_phone()
    order_id = f"ORD-{uuid.uuid4()}"
    order = SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR",
                       customer_phone=phone)
    result = provision_order(
        session, order_id, salla_client=FakeSallaClient({order_id: order}),
        product_catalog=CATALOG, expected_pricing=PRICING, now=_RENEW_NOW,
    )
    assert result.activation_token is not None
    flow.activate(session, token=result.activation_token, from_phone=phone,
                  display_name=None, now=_RENEW_NOW, whatsapp_client=wa,
                  admin_client=admin)
    session.execute(
        text("UPDATE subscriptions SET status = 'ACTIVE', current_period_end ="
             " :e WHERE id = :i"),
        {"e": _RENEW_NOW + timedelta(days=2), "i": str(result.subscription_id)},
    )
    session.commit()
    return phone, str(result.tenant_id), str(result.subscription_id)


def _silence(
    session: Session, phone: str, wa: FakeWhatsAppClient,
    admin: FakeTelegramAdminClient,
) -> None:
    """«إيقاف الرسائل» through the REAL worker, so ``opt_out_at`` and its
    classified `stop` row are written by the one authority allowed to write
    them — this test never touches the column it is arguing about."""
    _handle_message(
        session,
        {"id": f"wamid.{uuid.uuid4().hex}", "from": phone, "type": "text",
         "text": {"body": "إيقاف الرسائل"}},
        whatsapp_client=wa, admin_client=admin,
        now=_RENEW_NOW + timedelta(minutes=30),
    )


def _renew(
    session: Session, phone: str, *, admin: FakeTelegramAdminClient | None = None,
    days: int = 28,
):
    """Salla charges the card again — straight through provision_order, which
    is where the money becomes a row."""
    order_id = f"ORD-{uuid.uuid4()}"
    order = SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR",
                       customer_phone=phone)
    return provision_order(
        session, order_id, salla_client=FakeSallaClient({order_id: order}),
        product_catalog=CATALOG, expected_pricing=PRICING,
        now=_RENEW_NOW + timedelta(days=days), admin_client_hint=admin,
    )


def _renew_through_the_queue(
    session: Session, phone: str, wa: FakeWhatsAppClient,
    admin: FakeTelegramAdminClient,
) -> None:
    """The same charge as it actually arrives in production: a signed webhook
    the worker drains, so the customer-facing announcement runs too."""
    from career.db.models import WebhookEvent

    order_id = f"ORD-{uuid.uuid4()}"
    order = SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"), "SAR",
                       customer_phone=phone)
    session.add(WebhookEvent(
        id=uuid.uuid4(), provider="salla", event_type="order.payment.updated",
        event_fingerprint=f"fp-{order_id}", signature_valid=True,
        salla_order_id=order_id, payload={"data": {"id": order_id}},
        processing_status="received",
    ))
    session.commit()
    process_pending_webhooks(
        session, salla_client=FakeSallaClient({order_id: order}),
        product_catalog=CATALOG, expected_pricing=PRICING,
        admin_client=admin, whatsapp_client=wa,
        whatsapp_number_e164="+966500000000",
    )


class TestHePaidAgainWhileHisMessagesAreOff:

    def test_the_renewal_reaches_a_screen_and_not_only_a_log(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """THE DEFECT. He paid a second time for a service that physically
        cannot be delivered to him, and before this change the entire system's
        response was a subscription row."""
        wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
        phone, tenant_id, _ = _paying_customer(owner_session, wa, admin)
        _silence(owner_session, phone, wa, admin)
        assert _opted_out_pages(admin) == []   # nothing has been raised yet

        result = _renew(owner_session, phone, admin=admin)

        assert result.status is ProvisionStatus.RENEWED
        tickets = _channel_tickets(owner_session, phone)
        assert [(t.kind, t.status) for t in tickets] == [
            (flow.OPTED_OUT_TICKET_KIND, "open")
        ], (
            "he paid again while silenced and NOTHING was raised — "
            f"tickets: {tickets}; admin pages: {_opted_out_pages(admin)}"
        )
        paged = _opted_out_pages(admin)
        assert len(paged) == 1
        code = owner_session.execute(
            text("SELECT code FROM tenants WHERE id = :t"), {"t": tenant_id},
        ).scalar_one()
        assert code in paged[0]
        for message in admin.messages:          # §15.13, the whole channel
            assert phone not in message and phone.lstrip("+") not in message

    def test_the_customer_who_never_silenced_us_is_untouched(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The renewal that 99 customers in 100 get: no ticket, and the
        operator's notice exactly as it was."""
        wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
        phone, _, _ = _paying_customer(owner_session, wa, admin)

        result = _renew(owner_session, phone, admin=admin)

        assert result.status is ProvisionStatus.RENEWED
        assert _channel_tickets(owner_session, phone) == []
        assert _opted_out_pages(admin) == []

    def test_a_shut_window_and_a_silenced_customer_do_not_read_alike(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """«نافذة واتساب مقفلة — ما وصلت العميل رسالة تأكيد» is TRUE of a
        silenced customer and says the wrong thing: a shut window reopens the
        moment he writes, and this one never reopens by itself. The operator
        cannot act on a sentence that describes the ordinary case."""
        wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
        phone, _, _ = _paying_customer(owner_session, wa, admin)
        _silence(owner_session, phone, wa, admin)

        _renew_through_the_queue(owner_session, phone, wa, admin)

        notices = [m for m in admin.messages if m.startswith("↻ تجديد")]
        assert len(notices) == 1
        assert "نافذة واتساب مقفلة" not in notices[0], (
            "the silenced renewal is reported with the ordinary closed-window "
            f"sentence, which is indistinguishable from it: {notices[0]!r}"
        )
        assert "موقف الرسائل" in notices[0]

    def test_the_ordinary_shut_window_still_says_so(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The other half of the same distinction: a customer who simply has
        not written in three days must keep the sentence he always had."""
        wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
        phone, _, _ = _paying_customer(owner_session, wa, admin)
        owner_session.execute(
            text("UPDATE customer_channels SET last_inbound_at = :t"
                 " WHERE phone_e164 = :p"),
            {"t": _RENEW_NOW - timedelta(days=3), "p": phone},
        )
        owner_session.commit()

        _renew_through_the_queue(owner_session, phone, wa, admin)

        notices = [m for m in admin.messages if m.startswith("↻ تجديد")]
        assert len(notices) == 1
        assert "نافذة واتساب مقفلة" in notices[0]

    def test_the_silenced_customer_is_sent_nothing_at_all(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """He asked for silence and this charge is not a reply to anything he
        sent — unlike activation, where the welcome is a direct answer to a
        message he had just written. So nothing goes out to him, and the whole
        weight of the renewal lands on the operator."""
        wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
        phone, _, _ = _paying_customer(owner_session, wa, admin)
        _silence(owner_session, phone, wa, admin)
        # his window is WIDE OPEN — he wrote «إيقاف الرسائل» minutes ago, so
        # only the opt-out itself stops a free-form send here
        sent = len(wa.sent)

        _renew_through_the_queue(owner_session, phone, wa, admin)

        assert len(wa.sent) == sent
        assert not [m for m in wa.sent if m.kind == "template"]

    def test_the_second_renewal_does_not_grow_the_queue(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """Month after month on the same open ticket: the operator gets ONE
        row and ONE page, not one of each per charge."""
        wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
        phone, _, _ = _paying_customer(owner_session, wa, admin)
        _silence(owner_session, phone, wa, admin)

        _renew(owner_session, phone, admin=admin, days=28)
        _renew(owner_session, phone, admin=admin, days=56)

        assert len(_channel_tickets(owner_session, phone)) == 1
        assert len(_opted_out_pages(admin)) == 1

    def test_a_charge_after_the_ticket_was_closed_is_heard_again(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The dedupe must not become a mute button: once the operator has
        closed it, another month's money against the same silence is a fact he
        has to hear a second time."""
        wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
        phone, _, _ = _paying_customer(owner_session, wa, admin)
        _silence(owner_session, phone, wa, admin)
        _renew(owner_session, phone, admin=admin, days=28)
        owner_session.execute(
            text("UPDATE support_events SET status = 'resolved',"
                 " resolved_at = now() WHERE kind = :k AND channel_id IN"
                 " (SELECT id FROM customer_channels WHERE phone_e164 = :p)"),
            {"k": flow.OPTED_OUT_TICKET_KIND, "p": phone},
        )
        owner_session.commit()

        _renew(owner_session, phone, admin=admin, days=56)

        tickets = _channel_tickets(owner_session, phone)
        assert sorted(t.status for t in tickets) == ["open", "resolved"]
        assert len(_opted_out_pages(admin)) == 2

    def test_the_money_is_not_reversed_behind_the_operator(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """What happens to the money is the ONE thing this code does not
        decide. A refund, a pause or a cancellation is the operator's call on
        a customer we can still be wrong about (he may be reading nothing and
        renewing on purpose), and it is not reversible from here — Salla owns
        the charge. So the renewal is recorded in full, exactly as any other,
        and the ticket is what makes the decision reachable."""
        wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
        phone, tenant_id, _ = _paying_customer(owner_session, wa, admin)
        _silence(owner_session, phone, wa, admin)

        result = _renew(owner_session, phone, admin=admin)

        row = owner_session.execute(
            text("SELECT status, amount_sar FROM subscriptions WHERE id = :i"),
            {"i": str(result.subscription_id)},
        ).one()
        assert row.status == st.ACTIVE
        assert row.amount_sar == Decimal("279.00")
        assert owner_session.execute(
            text("SELECT count(*) FROM subscription_events WHERE"
                 " subscription_id = :i AND event_type = 'renewal_provisioned'"),
            {"i": str(result.subscription_id)},
        ).scalar_one() == 1
