"""Two money defects an adversarial reviewer proved against live staging data,
and the boundaries their fixes must not cross.

Both are about the SAME column read two different ways:

* `delivery_messages.category` — the band Meta's receipt says it billed a send
  at (0030) — was being written from receipts this code cannot read at all
  (`worker._handle_status`, D1); and
* `cost_allocations` kept a row for a kind the day no longer had, so the archive
  counted one WhatsApp message twice (`cv/close.rollup_costs`, D2).

Every test here fails on the code as it was on 2026-08-10 morning and passes
after. They are in their own file because they are the JOINT of two modules —
the receipt handler decides the band, the rollup spends it — and neither
module's own test file is the place a reader would look for the other half.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.cv import close
from career.db.models import WebhookEvent
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.worker import process_pending_whatsapp

NOW = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
LATER = datetime(2026, 7, 31, 6, 5, tzinfo=UTC)
DAY = date(2026, 7, 31)

#: Meta's live reading on 2026-08-08, and `config.py`'s defaults since.
UTILITY = Decimal("0.0107")
MARKETING = Decimal("0.0501")


# ── fixtures-by-hand: a tenant, a channel and one ledger row ─────────────────


def _seed_tenant(session: Session) -> uuid.UUID:
    tid = uuid.uuid4()
    session.execute(
        sql_text("INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tid), "c": f"TEN-R{uuid.uuid4().hex[:4]}"},
    )
    session.commit()
    return tid


def _seed_channel(session: Session, tid: uuid.UUID) -> uuid.UUID:
    cid = uuid.uuid4()
    session.execute(
        sql_text("INSERT INTO customer_channels (id, tenant_id, provider,"
                 " phone_e164) VALUES (:i, :t, 'whatsapp', :p)"),
        {"i": str(cid), "t": str(tid),
         "p": f"+96650{uuid.uuid4().int % 10**7:07d}"},
    )
    session.commit()
    return cid


def _seed_message(
    session: Session, tid: uuid.UUID, cid: uuid.UUID, *,
    template_name: str, kind: str = "template", status: str = "sent",
    category: str | None = None, created_at: datetime = NOW,
) -> str:
    """One outbound ledger row; returns its wa_message_id so a receipt can be
    aimed at it. ``category`` NULL is the state of every row until Meta's
    receipt lands."""
    wamid = f"wamid.{uuid.uuid4().hex}"
    session.execute(
        sql_text("INSERT INTO delivery_messages (id, tenant_id, channel_id,"
                 " wa_message_id, kind, template_name, status, category,"
                 " created_at) VALUES (:i, :t, :c, :w, :k, :n, :s, :g, :d)"),
        {"i": str(uuid.uuid4()), "t": str(tid), "c": str(cid), "w": wamid,
         "k": kind, "n": template_name, "s": status, "g": category,
         "d": created_at},
    )
    session.commit()
    return wamid


def _receipt(session: Session, wamid: str, status: str, *,
             category: str | None = None, now: datetime = NOW) -> None:
    """One status callback through the REAL worker path — the webhook row the
    intake writes, then the poller that drains it."""
    st: dict[str, Any] = {"id": wamid, "status": status,
                          "recipient_id": "966500000000"}
    if category is not None:
        st["pricing"] = {"billable": True, "pricing_model": "PMP",
                         "category": category}
    session.add(WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="messages",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload={"entry": [{"changes": [{"value": {
            "messaging_product": "whatsapp", "messages": [], "statuses": [st],
        }}]}]},
        processing_status="received",
    ))
    session.commit()
    process_pending_whatsapp(
        session, whatsapp_client=FakeWhatsAppClient(),
        admin_client=FakeTelegramAdminClient(), now=now,
    )
    session.commit()


def _row(session: Session, wamid: str) -> Any:
    return session.execute(sql_text(
        "SELECT status, category FROM delivery_messages"
        " WHERE wa_message_id = :w"), {"w": wamid}).one()


def _allocations(session: Session, tid: uuid.UUID) -> list[tuple[str, int, Decimal]]:
    return [
        (str(r.category), int(r.events), Decimal(r.cost_usd))
        for r in session.execute(sql_text(
            "SELECT category, events, cost_usd FROM cost_allocations"
            " WHERE tenant_id = :t ORDER BY day, category"), {"t": str(tid)})
    ]


# ── D1: the receipt refused for STATUS was trusted for MONEY ────────────────


def test_a_receipt_we_cannot_read_never_prices_the_send(
    owner_session: Session, clean_billing: None, caplog: Any
) -> None:
    """`_handle_status` refuses to record `status` from a word that is not on
    :data:`RECEIPT_ORDER` — a rung nobody has placed must not be billed or
    displayed on the strength of a name we have never seen — and then took the
    price band off that same unreadable callback. Because the band is FILL-ONCE,
    the first writer is permanent: the real `delivered` that follows loses, is
    logged as if META had re-priced the send, and nothing repairs the row
    (`scripts/backfill_meta_error_codes.py` writes `meta_error_code`, never
    `category`).

    The reviewer's reproduction, and the shape of the bill it produced:
    `subscription_daily_report` is the ONE template Meta calls UTILITY today,
    so a row pinned at `marketing` is billed 4.7× — $0.0501 against $0.0107 —
    for the life of the row.
    """
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    wamid = _seed_message(owner_session, tid, cid,
                          template_name="subscription_daily_report")

    with caplog.at_level("ERROR"):
        _receipt(owner_session, wamid, "deleted", category="marketing")

    row = _row(owner_session, wamid)
    assert row.status == "sent", "an unranked receipt walked the status"
    assert row.category is None, (
        "the price band was taken from a receipt whose own status word this "
        "code refused to record — half-trusting a callback, on the money half"
    )
    assert [r for r in caplog.records if "unknown whatsapp receipt status" in
            r.getMessage()], "the unreadable rung did not reach the operator"

    # …and the recognised receipt that follows still gets to say what it cost.
    _receipt(owner_session, wamid, "delivered", category="utility", now=LATER)
    assert _row(owner_session, wamid).category == "utility"

    spend = close.whatsapp_spend(owner_session, tenant_id=tid, day=DAY)
    assert spend == {"wa_utility": (1, UTILITY)}, (
        "Meta charged utility and the bill says otherwise"
    )


def test_a_receipt_that_merely_ARRIVED_LATE_still_prices_the_send(
    owner_session: Session, clean_billing: None
) -> None:
    """The boundary the D1 fix must not cross, and the reason it is `rank and`
    rather than moving the write below the rank gate.

    UNRANKED IS NOT OUT-OF-ORDER. A `delivered` landing after a `read` is a
    receipt we UNDERSTAND that arrived late — authoritative about money, merely
    stale about order — and Meta hangs `pricing` on exactly those `sent`/
    `delivered` receipts, so gating the band on WINNING the ladder would throw
    the billed category away on the messages that were read fastest.

    Deliberately paired here with the test above:
    `test_whatsapp_worker.test_the_price_band_is_not_on_the_receipt_ladder`
    asserts this same fact from the receipt handler's side, and the two halves
    of one distinction are worth reading in one place — a future reader who
    "fixes" D1 by moving the write below `if rank <= receipt_rank(...)` breaks
    this one and sees why immediately.
    """
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    wamid = _seed_message(owner_session, tid, cid,
                          template_name="subscription_daily_report")

    _receipt(owner_session, wamid, "read")
    _receipt(owner_session, wamid, "delivered", category="utility", now=LATER)

    row = _row(owner_session, wamid)
    assert row.status == "read", "a losing receipt walked the delivery back"
    assert row.category == "utility", (
        "the billed band was discarded because its receipt lost the ladder"
    )


# ── D2: a kind that stopped being produced kept billing ─────────────────────


def test_a_kind_the_day_no_longer_has_leaves_the_archive(
    owner_session: Session, clean_billing: None
) -> None:
    """The ordinary path since 0030, not an edge case. `whatsapp/delivery.py`
    rolls the day up in the same breath as the send, when the row still carries
    no band and the kind derives from the registry as `wa_marketing`; Meta's
    receipt lands seconds later and fills in `utility`; the next rollup derives
    `wa_utility`. Keyed `(tenant, day, category)` and writing only the kinds
    that exist NOW, the rollup left both — and the archive claimed the day had
    TWO WhatsApp messages costing $0.0608 when one message was sent.

    `test_cost_metering.test_rollup_folds_whatsapp_in_and_stays_idempotent`
    cannot see this: it re-runs with identical inputs, and «SET, not increment»
    is exactly right for a kind that is still there. The defect only appears
    when the row CHANGES between two runs, which is what this test does.
    """
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    # `welcome_activation` is one of the five Meta re-categorised to MARKETING,
    # so a row with no measured band derives marketing — the send-time answer.
    wamid = _seed_message(owner_session, tid, cid,
                          template_name="welcome_activation", status="delivered")

    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()
    assert _allocations(owner_session, tid) == [("wa_marketing", 1, MARKETING)]

    # Meta's receipt arrives and says what it actually billed.
    _receipt(owner_session, wamid, "read", category="utility", now=LATER)
    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()

    assert _allocations(owner_session, tid) == [("wa_utility", 1, UTILITY)], (
        "the retired kind kept its row: one message, billed twice, at 5.7x its "
        "real price in total"
    )


def test_retiring_a_kind_reaches_only_that_tenant_and_that_day(
    owner_session: Session, clean_billing: None
) -> None:
    """`rollup_costs` recomputes ONE tenant-day in full, so that pair is the
    only thing it may delete from. A WHERE clause that slipped either bound
    would silently erase another day's archive — and nothing reads this table,
    so nobody would find out."""
    mine = _seed_tenant(owner_session)
    other = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, mine)
    other_cid = _seed_channel(owner_session, other)
    yesterday = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)

    _seed_message(owner_session, mine, cid, template_name="welcome_activation",
                  status="delivered", created_at=yesterday)
    wamid = _seed_message(owner_session, mine, cid,
                          template_name="welcome_activation", status="delivered")
    _seed_message(owner_session, other, other_cid,
                  template_name="welcome_activation", status="delivered")

    close.rollup_costs(owner_session, tenant_id=mine, day=yesterday.date())
    close.rollup_costs(owner_session, tenant_id=mine, day=DAY)
    close.rollup_costs(owner_session, tenant_id=other, day=DAY)
    owner_session.commit()

    _receipt(owner_session, wamid, "read", category="utility", now=LATER)
    close.rollup_costs(owner_session, tenant_id=mine, day=DAY)
    owner_session.commit()

    assert _allocations(owner_session, mine) == [
        ("wa_marketing", 1, MARKETING),   # 30 July — a different day, untouched
        ("wa_utility", 1, UTILITY),       # 31 July — retired and rewritten
    ]
    assert _allocations(owner_session, other) == [("wa_marketing", 1, MARKETING)]


def test_a_day_whose_ledger_rows_are_gone_stops_claiming_a_bill(
    owner_session: Session, clean_billing: None
) -> None:
    """The empty-answer branch, which delete makes reachable and zeroing would
    not. `spend_by_kind` returning nothing means the day cost nothing — or that
    its ledger rows were erased under §12, in which case the archive should
    follow the evidence it is derived from rather than outlive it. Either way
    the day must stop asserting a bill nothing supports."""
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    wamid = _seed_message(owner_session, tid, cid,
                          template_name="welcome_activation", status="delivered")

    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()
    assert _allocations(owner_session, tid) == [("wa_marketing", 1, MARKETING)]

    owner_session.execute(
        sql_text("DELETE FROM delivery_messages WHERE wa_message_id = :w"),
        {"w": wamid},
    )
    owner_session.commit()
    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()

    assert _allocations(owner_session, tid) == []
