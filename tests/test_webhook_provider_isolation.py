"""One worker must never eat another worker's queue (INCIDENT 2026-08-03).

``webhook_events`` is a single table shared by both providers, and each
provider has its own worker draining it. The Salla sweep selected on
``processing_status == 'received'`` alone, so it also picked up WhatsApp rows;
they matched neither the provision nor the lifecycle event names, fell through
to the final ``else`` and were marked ``ignored`` — a terminal status nothing
in this codebase ever re-reads.

Live proof in staging: four ``whatsapp/statuses`` rows destroyed between
08:05:41 and 08:05:43 on 2026-08-03, inside the few seconds the WhatsApp
worker was busy in an LLM turn. The two workers poll the same table every
three seconds; whichever one arrives first wins the row, so the whole thing
was a race the Salla side was guaranteed to win eventually.

These tests pin the isolation from both directions: the Salla sweep touches
only Salla rows, and it still does everything it always did for its own.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import (
    CustomerChannel,
    DeliveryMessage,
    Tenant,
    WebhookEvent,
)
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import (
    ProvisionStatus,
    process_pending_webhooks,
    reset_salla_backoff,
)
from career.telegram.admin import FakeTelegramAdminClient

CATALOG = {"prod_pro": "professional"}
PRICING = {"prod_pro": (Decimal("279.00"), "SAR")}


def _order(order_id: str) -> SallaOrder:
    return SallaOrder(
        order_id=order_id, status="paid", product_id="prod_pro",
        amount=Decimal("279.00"), currency="SAR",
        customer_phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
    )


def _seed(
    session: Session, *, provider: str, event_type: str,
    status: str = "received", order_id: str | None = None,
    received_at: datetime | None = None, payload: dict | None = None,
) -> uuid.UUID:
    """Insert one raw intake row, exactly as career.webhooks.intake writes it."""
    event = WebhookEvent(
        id=uuid.uuid4(),
        provider=provider,
        event_type=event_type,
        event_fingerprint=f"test:{uuid.uuid4()}",
        signature_valid=True,
        salla_order_id=order_id,
        payload=payload or {},
        processing_status=status,
    )
    if received_at is not None:
        event.received_at = received_at
    session.add(event)
    session.commit()
    return event.id


def _row(session: Session, event_id: uuid.UUID) -> WebhookEvent:
    session.expire_all()
    return session.execute(
        select(WebhookEvent).where(WebhookEvent.id == event_id)
    ).scalar_one()


def _sweep(session: Session, client: FakeSallaClient, **kwargs) -> list:
    reset_salla_backoff()
    return process_pending_webhooks(
        session, salla_client=client, product_catalog=CATALOG,
        expected_pricing=PRICING, **kwargs,
    )


class TestWhatsAppEventsSurviveTheSallaSweep:
    """The regression itself — one class per destroyed event_type, because the
    live incident hit ``statuses`` and the July one hit ``messages``."""

    def test_statuses_event_is_left_untouched(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="statuses")

        _sweep(owner_session, FakeSallaClient({}))

        row = _row(owner_session, event_id)
        assert row.processing_status == "received"   # still the WhatsApp worker's
        assert row.processed_at is None
        assert row.attempt_count == 0

    def test_messages_event_is_left_untouched(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="messages")

        _sweep(owner_session, FakeSallaClient({}))

        row = _row(owner_session, event_id)
        assert row.processing_status == "received"
        assert row.processed_at is None
        assert row.attempt_count == 0

    def test_other_event_is_left_untouched(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """``other`` is the WhatsApp intake's catch-all — account_update,
        template status changes, anything Meta adds next. It is the shape most
        likely to look like junk to a foreign worker, so it is pinned too."""
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="other")

        _sweep(owner_session, FakeSallaClient({}))

        assert _row(owner_session, event_id).processing_status == "received"

    def test_a_provider_we_have_not_invented_yet_is_left_untouched(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The fix is an allow-list on OUR provider, not a deny-list of the
        one provider that got hurt. The next intake added to webhooks/intake.py
        must be safe before its worker is even written."""
        event_id = _seed(owner_session, provider="stripe",
                         event_type="charge.succeeded")

        _sweep(owner_session, FakeSallaClient({}))

        assert _row(owner_session, event_id).processing_status == "received"


class TestTheSallaSweepStillWorks:
    """Isolation that also disabled provisioning would be a worse bug than the
    one it fixes, so every Salla-side behavior is re-pinned here."""

    def test_paid_order_still_provisions_beside_a_whatsapp_event(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        wa_id = _seed(owner_session, provider="whatsapp",
                      event_type="statuses")
        order_id = f"ORD-{uuid.uuid4()}"
        salla_id = _seed(owner_session, provider="salla",
                         event_type="order.payment.updated", order_id=order_id)

        results = _sweep(owner_session, FakeSallaClient({order_id: _order(order_id)}))

        assert [r.status for r in results] == [ProvisionStatus.PROVISIONED]
        assert _row(owner_session, salla_id).processing_status == "processed"
        assert _row(owner_session, wa_id).processing_status == "received"

    def test_unroutable_salla_event_is_still_ignored(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """``app.installed`` and friends genuinely have nothing to do here —
        the ``else`` branch is correct for our OWN provider and must survive."""
        event_id = _seed(owner_session, provider="salla",
                         event_type="app.installed")

        _sweep(owner_session, FakeSallaClient({}))

        assert _row(owner_session, event_id).processing_status == "ignored"

    def test_duplicate_order_event_still_dedupes(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        order_id = f"ORD-{uuid.uuid4()}"
        client = FakeSallaClient({order_id: _order(order_id)})
        _seed(owner_session, provider="salla",
              event_type="order.payment.updated", order_id=order_id)
        second = _seed(owner_session, provider="salla",
                       event_type="order.created", order_id=order_id)

        results = _sweep(owner_session, client)

        assert results[0].status is ProvisionStatus.PROVISIONED
        assert results[1].status is ProvisionStatus.ALREADY_PROVISIONED
        assert _row(owner_session, second).processing_status == "skipped_duplicate"

    def test_salla_outage_still_defers_without_marking_anything_terminal(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """_defer_webhook's whole contract: the row stays 'received' so the
        next pass retries it, and only the attempt counter moves."""
        from career.salla.client import SallaApiError

        class _DownClient:
            def get_order(self, order_id: str):  # noqa: ANN202
                raise SallaApiError("503", retryable=True)

        order_id = f"ORD-{uuid.uuid4()}"
        event_id = _seed(owner_session, provider="salla",
                         event_type="order.payment.updated", order_id=order_id)

        results = _sweep(owner_session, _DownClient())  # type: ignore[arg-type]

        assert [r.status for r in results] == [ProvisionStatus.DEFERRED]
        row = _row(owner_session, event_id)
        assert row.processing_status == "received"
        assert row.attempt_count == 1


class TestTheWaitingCountSpeaksForSallaOnly:
    """The «سلة لا تستجيب» alert quotes how many events are waiting. Counting
    the whole table made that number the WhatsApp backlog — the operator was
    told hundreds of paid orders were stuck behind a dead Salla token when the
    real answer was one."""

    def test_backlog_count_excludes_other_providers(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        from career.salla.client import SallaApiError

        class _DownClient:
            def get_order(self, order_id: str):  # noqa: ANN202
                raise SallaApiError("token dead", retryable=True)

        # The Salla event is the OLDEST, so the sweep reaches it first, defers
        # it and breaks — leaving the five WhatsApp rows still 'received' at
        # the moment the count runs. Ordered the other way the old code marked
        # them 'ignored' on the way past and the wrong count came out right by
        # accident, which is how this stayed invisible.
        order_id = f"ORD-{uuid.uuid4()}"
        _seed(owner_session, provider="salla",
              event_type="order.payment.updated", order_id=order_id,
              received_at=datetime.now(UTC) - timedelta(hours=1))
        for _ in range(5):
            _seed(owner_session, provider="whatsapp", event_type="messages")

        admin = FakeTelegramAdminClient()
        _sweep(owner_session, _DownClient(), admin_client=admin)  # type: ignore[arg-type]

        waiting = [m for m in admin.messages if "أحداث تنتظر الآن" in m]
        assert waiting, "the outage alert must still name the backlog"
        # The count sits on its own line: Fahad's client scrambles any line
        # carrying both Arabic and a Latin digit, and this assertion used to
        # pin the scrambled shape — a test holding a defect in place.
        assert "أحداث تنتظر الآن:\n1" in waiting[0]


class TestTheBatchLimitIsSallaScoped:
    """A provider-scoped query changes what ``limit`` means, and the new
    meaning is the load-bearing one: before the fix a WhatsApp backlog could
    fill the batch and starve paid orders out of every pass."""

    def test_a_whatsapp_backlog_cannot_crowd_out_a_paid_order(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        old = datetime.now(UTC) - timedelta(hours=1)
        for _ in range(3):
            _seed(owner_session, provider="whatsapp", event_type="messages",
                  received_at=old)
        order_id = f"ORD-{uuid.uuid4()}"
        _seed(owner_session, provider="salla",
              event_type="order.payment.updated", order_id=order_id)

        # limit=1 with the old query would have selected the oldest WhatsApp
        # row and provisioned nothing, forever, at three seconds a pass.
        results = _sweep(
            owner_session, FakeSallaClient({order_id: _order(order_id)}), limit=1,
        )

        assert [r.status for r in results] == [ProvisionStatus.PROVISIONED]


# ── the recovery side: the five rows the bug already ate ─────────────────────

REPO = Path(__file__).resolve().parents[1]
REPLAY = REPO / "scripts" / "replay_lost_events.py"

#: A realistic Meta receipt. The phone and the wamid are the two things the
#: redaction test below hunts for in the tool's output.
_PROBE_PHONE = "966500000001"


def _statuses_payload(wamid: str, status: str) -> dict:
    return {"entry": [{"changes": [{"value": {
        "metadata": {"display_phone_number": _PROBE_PHONE},
        "statuses": [{"id": wamid, "status": status,
                      "recipient_id": _PROBE_PHONE}],
    }}]}]}


def _messages_payload(wamid: str, body: str) -> dict:
    return {"entry": [{"changes": [{"value": {
        "messages": [{"id": wamid, "from": _PROBE_PHONE, "type": "text",
                      "text": {"body": body}}],
    }}]}]}


def _delivery(session: Session, *, wamid: str, status: str) -> None:
    """A tenant + channel + one outbound message, so a replayed receipt has
    something real to land on."""
    tenant = Tenant(id=uuid.uuid4(), code=f"TEN-{uuid.uuid4().int % 9000 + 1000}")
    session.add(tenant)
    session.flush()
    channel = CustomerChannel(
        id=uuid.uuid4(), tenant_id=tenant.id, provider="whatsapp",
        phone_e164=f"+{_PROBE_PHONE}",
    )
    session.add(channel)
    session.flush()
    session.add(DeliveryMessage(
        id=uuid.uuid4(), tenant_id=tenant.id, channel_id=channel.id,
        wa_message_id=wamid, kind="bundle", status=status,
    ))
    session.commit()


def _replay(*args: str, ledger: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed interpreter, fixed script path
        [sys.executable, str(REPLAY), "--audit-file", str(ledger), *args],
        capture_output=True, text=True, cwd=str(REPO), check=False,
    )


class TestReplayLostEvents:
    """scripts/replay_lost_events.py — the tool for the five staging rows the
    sweep destroyed before the query was fixed."""

    def test_dry_run_is_the_default_and_writes_nothing(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        wamid = f"wamid.{uuid.uuid4().hex}"
        _delivery(owner_session, wamid=wamid, status="sent")
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="statuses", status="ignored",
                         payload=_statuses_payload(wamid, "delivered"))

        proc = _replay(ledger=tmp_path / "ledger.jsonl")

        assert proc.returncode == 0, proc.stderr
        assert "DRY RUN" in proc.stdout
        assert str(event_id) in proc.stdout
        assert _row(owner_session, event_id).processing_status == "ignored"

    def test_apply_requeues_the_event(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        wamid = f"wamid.{uuid.uuid4().hex}"
        _delivery(owner_session, wamid=wamid, status="sent")
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="statuses", status="ignored",
                         payload=_statuses_payload(wamid, "delivered"))

        proc = _replay("--apply", ledger=tmp_path / "ledger.jsonl")

        assert proc.returncode == 0, proc.stderr
        # 'received' is the only status the WhatsApp worker selects — that is
        # the whole of the recovery.
        assert _row(owner_session, event_id).processing_status == "received"

    def test_running_it_twice_replays_nothing_the_second_time(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        """Idempotency from both guards at once: the row has left the
        ignored/failed selection AND its id is in the ledger."""
        ledger = tmp_path / "ledger.jsonl"
        wamid = f"wamid.{uuid.uuid4().hex}"
        _delivery(owner_session, wamid=wamid, status="sent")
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="statuses", status="ignored",
                         payload=_statuses_payload(wamid, "delivered"))

        first = _replay("--apply", ledger=ledger)
        second = _replay("--apply", ledger=ledger)

        assert "to replay : 1" in first.stdout
        assert str(event_id) not in second.stdout
        assert "to replay : 0" in second.stdout
        assert _row(owner_session, event_id).processing_status == "received"
        applied = [
            json.loads(line) for line in
            ledger.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        replays = [r for r in applied
                   if r["action"] == "replay" and not r["dry_run"]
                   and r["event_id"] == str(event_id)]
        assert len(replays) == 1     # never re-queued a second time

    def test_the_ledger_alone_blocks_a_second_replay(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        """The selection guard only holds while the criteria stay at their
        defaults. Pointed straight at 'received', the ledger must still refuse
        — otherwise a mistyped --status re-queues everything all over again."""
        ledger = tmp_path / "ledger.jsonl"
        wamid = f"wamid.{uuid.uuid4().hex}"
        _delivery(owner_session, wamid=wamid, status="sent")
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="statuses", status="ignored",
                         payload=_statuses_payload(wamid, "delivered"))

        _replay("--apply", ledger=ledger)
        again = _replay("--apply", "--status", "received,ignored,failed",
                        ledger=ledger)

        assert "already re-queued by an earlier run" in again.stdout
        assert str(event_id) in again.stdout
        assert "to replay : 0" in again.stdout

    def test_a_receipt_that_would_roll_a_delivery_backwards_is_skipped(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        """worker._handle_status assigns the receipt's status unconditionally,
        so replaying a stale 'sent' over a message already 'read' would erase
        the newer truth. The tool compares before it re-queues."""
        wamid = f"wamid.{uuid.uuid4().hex}"
        _delivery(owner_session, wamid=wamid, status="read")
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="statuses", status="ignored",
                         payload=_statuses_payload(wamid, "sent"))

        proc = _replay("--apply", ledger=tmp_path / "ledger.jsonl")

        assert "already at or past this receipt" in proc.stdout
        assert _row(owner_session, event_id).processing_status == "ignored"

    def test_a_receipt_with_nothing_to_apply_it_to_is_skipped(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="statuses", status="ignored",
                         payload=_statuses_payload(
                             f"wamid.{uuid.uuid4().hex}", "delivered"))

        proc = _replay("--apply", ledger=tmp_path / "ledger.jsonl")

        assert "no delivery_messages row" in proc.stdout
        assert _row(owner_session, event_id).processing_status == "ignored"

    def test_outbound_capable_kinds_are_refused_by_default(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        """The one guarantee that cannot be left to judgement: no duplicate
        message to a customer. 'messages' is the only kind whose handler can
        speak, so it takes a second, deliberate flag."""
        proc = _replay("--event-type", "messages", "--apply",
                       ledger=tmp_path / "ledger.jsonl")

        assert proc.returncode == 2
        assert "REFUSING" in proc.stdout

    def test_a_stale_inbound_is_not_answered_weeks_later(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        """A row from 2026-07-19. Replaying it would reply, today, to a
        message sent two and a half weeks ago.

        Seeded 'ignored', not 'failed': a 'failed' row is now refused outright
        for message-bearing kinds (the worker had already sent and then rolled
        back), and that refusal comes first, so it would hide the age rule this
        test exists to pin rather than exercise it.
        """
        event_id = _seed(
            owner_session, provider="whatsapp", event_type="messages",
            status="ignored", received_at=datetime.now(UTC) - timedelta(days=18),
            payload=_messages_payload(f"wamid.{uuid.uuid4().hex}", "السلام عليكم"),
        )

        proc = _replay("--event-type", "messages", "--allow-outbound",
                       "--apply", ledger=tmp_path / "ledger.jsonl")

        assert "would answer a conversation that has moved on" in proc.stdout
        assert _row(owner_session, event_id).processing_status == "ignored"

    def test_an_inbound_already_recorded_elsewhere_is_skipped(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        """Verification before recovery: the message reached inbound_messages
        by some other path, so the worker would return before sending anything
        and the replay would be pure risk for no gain.

        Seeded 'ignored' for the same reason as the age test above — a 'failed'
        row never reaches this check now, because the worker's rollback means
        the absence of the row proves nothing about whether it spoke.
        """
        from career.db.models import InboundMessage

        wamid = f"wamid.{uuid.uuid4().hex}"
        tenant = Tenant(id=uuid.uuid4(),
                        code=f"TEN-{uuid.uuid4().int % 9000 + 1000}")
        owner_session.add(tenant)
        owner_session.flush()
        channel = CustomerChannel(
            id=uuid.uuid4(), tenant_id=tenant.id, provider="whatsapp",
            phone_e164=f"+{_PROBE_PHONE}",
        )
        owner_session.add(channel)
        owner_session.flush()
        owner_session.add(InboundMessage(
            id=uuid.uuid4(), tenant_id=tenant.id, channel_id=channel.id,
            wa_message_id=wamid, message_type="text", classification="other",
        ))
        owner_session.commit()
        event_id = _seed(owner_session, provider="whatsapp",
                         event_type="messages", status="ignored",
                         payload=_messages_payload(wamid, "test"))

        proc = _replay("--event-type", "messages", "--allow-outbound",
                       "--apply", ledger=tmp_path / "ledger.jsonl")

        assert "already recorded by another path" in proc.stdout
        assert _row(owner_session, event_id).processing_status == "ignored"

    def test_the_report_leaks_no_phone_body_or_message_id(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        """§15.13 — the recovery report is read and pasted around during an
        incident, which is exactly when nobody is checking what is in it."""
        ledger = tmp_path / "ledger.jsonl"
        wamid = f"wamid.{uuid.uuid4().hex}"
        body = "MY-SECRET-MESSAGE-BODY"
        _delivery(owner_session, wamid=wamid, status="sent")
        _seed(owner_session, provider="whatsapp", event_type="statuses",
              status="ignored", payload=_statuses_payload(wamid, "delivered"))
        _seed(owner_session, provider="whatsapp", event_type="messages",
              status="failed", payload=_messages_payload(wamid, body))

        proc = _replay("--event-type", "statuses,messages", "--allow-outbound",
                       ledger=ledger)

        printed = proc.stdout + proc.stderr + ledger.read_text(encoding="utf-8")
        assert _PROBE_PHONE not in printed
        assert body not in printed
        assert wamid not in printed
