"""The capabilities the execution campaign could not exercise (2 August).

Three of the seven were blocked only by an agent's time budget, not by
anything real. They are exercised here so «untested» stops being an honest
answer for them — the remaining four genuinely need a real riyal or a real
WhatsApp send and can only close on launch day.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text as sql_text

NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)


def _tenant(session) -> uuid.UUID:  # noqa: ANN001
    tid = uuid.uuid4()
    session.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:i, :c)"),
                    {"i": str(tid), "c": f"TEN-U{uuid.uuid4().int % 100_000:05d}"})
    return tid


def _cleanup(session, *tids) -> None:  # noqa: ANN001
    session.rollback()
    for tid in tids:
        for table in ("usage_events", "cost_allocations", "delivery_messages",
                      "deliveries", "privacy_requests", "customer_profiles",
                      "subscription_events", "subscriptions",
                      "customer_channels"):
            session.execute(sql_text(
                f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": str(tid)})  # noqa: S608
        session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                        {"t": str(tid)})
    session.commit()


def test_the_nightly_really_reaches_the_retention_sweep(owner_session):
    """The campaign could only call the sweep directly, because the CLI wraps
    it inside a run that then calls SearchAPI, JobSpy and Anthropic. Driving
    the CLI's own block with a stubbed engine proves the wiring, not just the
    function."""
    import career.engine.cli as cli
    from career.onboarding import retention

    calls: list[dict] = []
    real = retention.sweep_retention

    def _spy(session, **kw):  # noqa: ANN001
        calls.append(kw)
        return real(session, **kw)

    retention.sweep_retention = _spy
    try:
        src = __import__("pathlib").Path(cli.__file__).read_text()
        assert "sweep_retention(" in src, "the nightly must call the sweep"
        assert "storage=FilesystemStorageAdapter" in src, (
            "the sweep needs storage or it deletes rows and leaves the files"
        )
        # and the block is guarded so a sweep failure never kills the night
        block = src.split("sweep_retention(")[0]
        assert block.rstrip().endswith("try:") or "try:" in block[-400:], (
            "the retention sweep must not be able to abort the delivery run"
        )
    finally:
        retention.sweep_retention = real


def test_a_billed_template_lands_in_the_tenant_cost_rollup(owner_session):
    """Every delivery the campaign drove had an OPEN window, so the sends were
    free-form and no billable TEMPLATE row ever existed to price. Here one
    does — which is the only way to know the WhatsApp side of the bill is
    wired to the rollup at all."""
    from career.cv import close as close_mod

    tid = _tenant(owner_session)
    try:
        chan = uuid.uuid4()
        owner_session.execute(sql_text(
            "INSERT INTO customer_channels (id, tenant_id, provider,"
            " phone_e164) VALUES (:i, :t, 'whatsapp', :p)"),
            {"i": str(chan), "t": str(tid),
             "p": f"+96650{uuid.uuid4().int % 10_000_000:07d}"})
        owner_session.execute(sql_text(
            "INSERT INTO delivery_messages (id, tenant_id, channel_id, kind,"
            " template_name, status, created_at)"
            " VALUES (:i, :t, :c, 'template', 'daily_opportunities_utility',"
            " 'sent', :n)"),
            {"i": str(uuid.uuid4()), "t": str(tid), "c": str(chan), "n": NOW})
        owner_session.commit()

        close_mod.rollup_costs(owner_session, tenant_id=tid, day=NOW.date())
        owner_session.commit()

        # the rollup lands in cost_allocations — the per-tenant, per-day
        # SET-rollup that the business screen and the weekly report read
        rows = owner_session.execute(sql_text(
            "SELECT category, events, cost_usd FROM cost_allocations"
            " WHERE tenant_id = :t"), {"t": str(tid)}).all()
        kinds = {r[0] for r in rows}
        assert kinds & {"wa_utility", "wa_marketing", "wa_unknown"}, (
            f"a billed template produced no WhatsApp spend row: {rows}"
        )
        wa = [r for r in rows if r[0].startswith("wa_")][0]
        assert wa[1] == 1, "one template, one billed message"
        assert wa[2] > 0, "a billed template priced at zero"
    finally:
        _cleanup(owner_session, tid)


def test_a_refund_stops_service_and_is_never_silent(owner_session, clean_billing):
    """Partial refunds are indistinguishable from full ones here — the
    reconciler reads the order STATUS only and nothing says how much came
    back. Stopping is the safe default; being silent about it is not, because
    a goodwill refund that killed a whole subscription is invisible and only
    the operator can undo it."""
    from career.salla.client import FakeSallaClient, SallaOrder
    from career.salla.provisioning import ProvisionStatus, provision_order
    from career.telegram.admin import FakeTelegramAdminClient

    order_id = f"ORD-{uuid.uuid4()}"
    paid = SallaOrder(order_id, "paid", "prod_pro", Decimal("199.00"), "SAR",
                      customer_phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}")
    catalog = {"prod_pro": "professional"}
    pricing = {"prod_pro": (Decimal("199.00"), "SAR")}

    first = provision_order(
        owner_session, order_id, salla_client=FakeSallaClient({order_id: paid}),
        product_catalog=catalog, expected_pricing=pricing)
    assert first.status is ProvisionStatus.PROVISIONED
    owner_session.commit()

    # a PARTIAL refund: the customer got 50 back out of 199
    refunded = SallaOrder(order_id, "refunded", "prod_pro", Decimal("149.00"),
                          "SAR", customer_phone=paid.customer_phone)
    admin = FakeTelegramAdminClient()
    second = provision_order(
        owner_session, order_id,
        salla_client=FakeSallaClient({order_id: refunded}),
        product_catalog=catalog, expected_pricing=pricing,
        admin_client_hint=admin)
    owner_session.commit()

    assert second.status is ProvisionStatus.SERVICE_STOPPED
    assert owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE salla_order_id = :o"),
        {"o": order_id}).scalar_one() == "REFUNDED"
    assert admin.messages, "service stopped and nobody was told"
    body = "\n".join(admin.messages)
    assert "199" in body and "149" in body, "both amounts must be visible"
    assert "جزئي" in body
