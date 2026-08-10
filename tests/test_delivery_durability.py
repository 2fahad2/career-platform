"""The other direction of constant 3 (CHANGELOG 39): a ledger row that was
written after a real send must not be LOST by a later rollback.

On 2026-08-10 a live nightly sent a real customer four WhatsApp messages —
two texts, a document and an interactive card, all four acknowledged by Meta —
and then the `delivery_messages` INSERT hit a column the host database did not
have. The per-tenant `except` called `session.rollback()`, so all four ledger
rows and the `deliveries` row vanished, and the day was closed
`CV_GENERATION_FAILED` with `cv_resolved=0, cv_failed=6, delivered=0`: five
numbers, every one of them false, about a night that generated one CV,
delivered it, and had it read.

These tests pin both halves of the fix and the boundary between them:

* a send that LANDED survives a raise in the work that follows — the ledger
  rows and the `deliveries` row are still there, on a different connection,
  and the day closes LEDGER_FAILED with the counts that were actually true;
* a send that NEVER HAPPENED still writes nothing, because the durability
  point commits what is true and never manufactures a row.

Only the first is a regression proof — run against the code of 2026-08-09 it
fails on «no `deliveries` row at all», which is the incident itself. The second
passed before this change too, and is here so the fix cannot later be
«improved» into writing a row for a send that was refused: that row would be a
bill Meta never charged us, and `close.whatsapp_spend` would price it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.cv import daily_run
from career.whatsapp.client import FakeWhatsAppClient
from tests.test_cv_daily_run import (
    NOW,
    _cleanup,
    _deps,
    _engine_report,
    _seed_active_tenant,
)


class DeadWhatsApp(FakeWhatsAppClient):
    """Every send is refused — the provider is down, nothing reaches anyone."""

    def send_text(self, to_phone: str, body: str) -> str:
        raise RuntimeError("graph api unreachable")

    def send_document(self, to_phone: str, document_ref: str, *,
                      filename: str, caption: str = "") -> str:
        raise RuntimeError("graph api unreachable")

    def send_interactive(self, to_phone: str, body: str,
                         buttons: Any) -> str:
        raise RuntimeError("graph api unreachable")


def _rows(owner_engine: Engine, table: str, tid: uuid.UUID) -> list[Any]:
    """Read on a SEPARATE connection. The whole claim under test is about what
    survived a COMMIT, and a session that still holds the writing transaction
    cannot tell a committed row from an uncommitted one."""
    with Session(owner_engine) as fresh:
        return list(fresh.execute(
            sql_text(f"SELECT * FROM {table} WHERE tenant_id = :t"),  # noqa: S608
            {"t": str(tid)},
        ).mappings())


def _ledger_of(owner_engine: Engine, delivery_id: Any) -> list[Any]:
    """The ledger rows of ONE delivery.

    Not «every row this tenant has»: seeding activates the customer, and the
    activation welcome is itself a ledgered send with a NULL ``delivery_id``.
    Counting it would make «four rows survived» read four when three did."""
    with Session(owner_engine) as fresh:
        return list(fresh.execute(
            sql_text("SELECT * FROM delivery_messages WHERE delivery_id = :d"),
            {"d": str(delivery_id)},
        ).mappings())


def test_a_landed_delivery_survives_a_crash_in_the_work_that_follows(
    owner_session: Session, owner_engine: Engine, clean_billing: None,
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live incident, reproduced: four messages land, then the first piece
    of work after the last send raises. The ledger must still be there and the
    day must say so — with real numbers."""
    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        assert report.per_tenant[tid]["final"], "engine must pass the job"
        deps = _deps(tmp_path)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            # The exact shape of the live failure: the raise comes from the
            # first work after the last send — `close_from_delivery`, whose
            # flush is what emitted the doomed INSERT on 10 August.
            raise RuntimeError('column "meta_error_code" does not exist')

        monkeypatch.setattr(daily_run, "close_from_delivery", _boom)
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=deps, now=NOW,
        )

        # The customer really did receive the delivery.
        assert [m.kind for m in deps.whatsapp_client.sent] == [
            "text", "text", "document", "interactive"
        ]

        # ── the half that used to vanish ────────────────────────────────────
        deliveries = _rows(owner_engine, "deliveries", tid)
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "COMPLETED"
        ledger = _ledger_of(owner_engine, deliveries[0]["id"])
        assert sorted(r["kind"] for r in ledger) == [
            "document", "interactive", "text", "text"
        ], "every landed message keeps its ledger row"

        # ── the honest day, with counts nobody invented ─────────────────────
        assert states[tid].state == "LEDGER_FAILED"
        counts = dict(states[tid].counts)
        assert counts["cv_resolved"] == 1        # was a hard-coded 0
        assert counts["delivered"] == 1          # was 0, for a CV he read
        assert counts["cv_failed"] == 0          # was max(passes, 1)
        assert counts["gate_passes"] == 1
        assert counts["failed_sends"] == 0
        # and it is durable, on its own connection
        persisted = _rows(owner_engine, "tenant_day_states", tid)
        assert [(r["state"], dict(r["counts"])["delivered"])
                for r in persisted] == [("LEDGER_FAILED", 1)]
    finally:
        owner_session.rollback()
        _cleanup(owner_session, tid)


def test_a_send_that_never_happened_writes_nothing(
    owner_session: Session, owner_engine: Engine, clean_billing: None,
    tmp_path: Any,
) -> None:
    """The boundary. The durability point commits what is TRUE, so a night
    where every send was refused must still write zero ledger rows — a row for
    a send that failed is a bill Meta never charged us, and
    `close.whatsapp_spend` would price it."""
    tid, _ = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        deps = _deps(tmp_path, whatsapp_client=DeadWhatsApp())
        states = daily_run.run_daily_delivery(
            owner_session, report=report, deps=deps, now=NOW,
        )
        owner_session.commit()

        # the delivery row itself is committed (it is the FK precondition and
        # it claims nothing), and the day is honest about the sends
        deliveries = _rows(owner_engine, "deliveries", tid)
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "PARTIAL"
        assert _ledger_of(owner_engine, deliveries[0]["id"]) == []
        assert states[tid].state == "WHATSAPP_FAILED"
        assert dict(states[tid].counts)["delivered"] == 0
        assert dict(states[tid].counts)["cv_resolved"] == 1
    finally:
        owner_session.rollback()
        _cleanup(owner_session, tid)
