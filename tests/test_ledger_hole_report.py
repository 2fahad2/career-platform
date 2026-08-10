"""A message Meta delivered, and nothing in our ledger (CHANGELOG §39).

On live staging 115 wamids appear in priced Meta status receipts and fourteen
of them have NO ``delivery_messages`` row at all — the oldest from 2026-07-17,
the newest created while a planner was measuring. Nobody knew for 24 days,
because the only line resembling the question — «codes with no ledger row» in
``scripts/backfill_meta_error_codes`` — counts ERROR-carrying receipts, and
every missing row belonged to a ``delivered`` one.

These tests drive the real report functions, not a model of them, and they pin
the two rulings of §39:

1. NEITHER SCRIPT BECOMES A LEDGER AUTHOR. A receipt names the wamid, the
   status and the price band, and names neither the message kind nor the
   template — the two columns spend is billed on. So the reports COUNT the
   holes and create nothing; :class:`TestNeitherScriptWrites` proves the row
   count does not move even under ``--apply``.

2. THE HOLE HAS THREE CAUSES AND ONLY ONE IS A DEFECT — a lost write, a send
   no code ever records, a refused send. A detector that adds them together
   fires daily on free service acks and teaches the operator to skip the
   report, which is the §37 failure mode that buried seven true complaints
   under one false line. So the narrow line — billable, or a non-``service``
   band — must stay SILENT on the acks and the refusals, and speak for the
   one utility template. That is the test the whole file exists for:
   :meth:`TestThreeCauses.test_the_page_line_is_silent_on_free_service_acks`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, DeliveryMessage, Tenant, WebhookEvent

REPO = Path(__file__).resolve().parents[1]
REPLAY = REPO / "scripts" / "replay_lost_events.py"

#: The phone that must never reach an operator channel (constant 13). Seeded
#: into every payload so the redaction assertions have something to hunt for.
_PROBE_PHONE = "966500000009"


def _load(name: str, filename: str) -> Any:
    """Import a file from ``scripts/``, which is not a package.

    Registered in ``sys.modules`` under its spec name BEFORE it is executed:
    ``dataclasses`` resolves ``from __future__`` string annotations through
    ``sys.modules[cls.__module__]``, and a module that is not there yet raises
    while its own decorators are still running. Same loader shape as
    ``tests/test_whatsapp_worker._backfill`` and the same module name, so the
    two files share one execution instead of racing two.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def backfill() -> Any:
    return _load("career_backfill_codes", "backfill_meta_error_codes.py")


def _status(
    wamid: str, status: str, *,
    band: str | None = None, billable: bool | None = None,
    code: int | None = None,
) -> dict[str, Any]:
    """One Meta status object, shaped exactly as the live ones are.

    ``pricing`` is omitted entirely when Meta said nothing about money — which
    is what the live refusal looks like, and the distinction the report keeps:
    «Meta said free» and «Meta said nothing» are different facts.
    """
    st: dict[str, Any] = {"id": wamid, "status": status,
                          "recipient_id": _PROBE_PHONE}
    if band is not None or billable is not None:
        st["pricing"] = {"pricing_model": "PMP"}
        if band is not None:
            st["pricing"]["category"] = band
        if billable is not None:
            st["pricing"]["billable"] = billable
    if code is not None:
        st["errors"] = [{"code": code, "title": "…"}]
    return st


def _seed_receipt(session: Session, *statuses: dict[str, Any]) -> uuid.UUID:
    event = WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="statuses",
        event_fingerprint=f"test:{uuid.uuid4()}", signature_valid=True,
        payload={"entry": [{"changes": [{"value": {
            "metadata": {"display_phone_number": _PROBE_PHONE},
            "statuses": list(statuses),
        }}]}]},
        processing_status="ignored",
    )
    session.add(event)
    session.commit()
    return event.id


def _ledger_row(session: Session, wamid: str, *, kind: str = "bundle") -> None:
    """A tenant + channel + the delivery row a receipt is supposed to land on.

    The channel's number is unique per call and is NOT :data:`_PROBE_PHONE`:
    `customer_channels` is unique on (provider, phone_e164), so re-using one
    number would make the second delivery row in any test an IntegrityError
    rather than a fixture. The probe number stays where it belongs — inside the
    payload, which is where the redaction assertions look for it.
    """
    tenant = Tenant(id=uuid.uuid4(), code=f"TEN-{uuid.uuid4().int % 9000 + 1000}")
    session.add(tenant)
    session.flush()
    channel = CustomerChannel(
        id=uuid.uuid4(), tenant_id=tenant.id, provider="whatsapp",
        phone_e164=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
    )
    session.add(channel)
    session.flush()
    session.add(DeliveryMessage(
        id=uuid.uuid4(), tenant_id=tenant.id, channel_id=channel.id,
        wa_message_id=wamid, kind=kind, status="sent",
    ))
    session.commit()


def _rows(session: Session) -> int:
    session.expire_all()
    return int(session.execute(
        select(func.count()).select_from(DeliveryMessage)
    ).scalar_one())


def _run(backfill: Any, session: Session, *, apply: bool = False) -> Any:
    """The real pipeline: scan → match → classify. One owner session stands in
    for the CLI's per-tenant ones — it bypasses RLS, so the presence set is the
    union across every tenant, which is exactly what the CLI accumulates."""
    scan = backfill.scan_payloads(session)
    applied = backfill.apply_codes(session, scan, apply=apply)
    holes = backfill.ledger_holes(scan, applied)
    # `apply_codes` flushes and leaves the commit to its caller — which in the
    # CLI is the tenant session closing. Left open here it is an UPDATE holding
    # row locks, and the `clean_billing` teardown's DELETE then waits on it
    # forever: the run hangs with no failure and no output. Ending the
    # transaction is the caller's job and this is the caller.
    session.commit()
    return scan, applied, holes


# ── the three causes, on one fixture that carries all of them ────────────────


class TestThreeCauses:
    """One scan, all three causes present, because they are only separable
    together: the whole finding is that they were being added up."""

    @pytest.fixture()
    def live_shape(self, owner_session: Session, clean_billing: None) -> dict[str, str]:
        """The live staging shape in miniature: one recorded delivery, one
        billed template with no row, one free service ack with no row, one
        refusal with no row and no pricing at all."""
        ids = {name: f"wamid.{uuid.uuid4().hex}" for name in
               ("recorded", "lost", "ack", "refused")}
        _ledger_row(owner_session, ids["recorded"], kind="template")
        _seed_receipt(
            owner_session,
            _status(ids["recorded"], "delivered", band="utility", billable=True),
            _status(ids["lost"], "delivered", band="utility", billable=True),
            _status(ids["ack"], "delivered", band="service", billable=False),
            _status(ids["refused"], "failed", code=131047),
        )
        return ids

    def test_the_hole_is_counted_at_all(
        self, backfill: Any, owner_session: Session, live_shape: dict[str, str]
    ) -> None:
        """The measurement that was missing: three of the four scanned ids have
        no ledger row, and not one of them carries an error code except the
        refusal — so the pre-existing «codes with no ledger row» line saw one
        of the three and the other two were invisible for 24 days."""
        scan, applied, holes = _run(backfill, owner_session)

        assert len(scan.receipt_ids) == 4
        assert applied.present == {live_shape["recorded"]}
        assert holes.total == 3
        # the old line, unchanged: it counts the refusal only, because that is
        # the only hole carrying a code.
        assert len(scan.codes) - applied.matched == 1

    def test_each_cause_lands_on_its_own_line(
        self, backfill: Any, owner_session: Session, live_shape: dict[str, str]
    ) -> None:
        _scan, _applied, holes = _run(backfill, owner_session)

        assert holes.billed == 1        # the lost write — the defect
        assert holes.refused == 1       # correctly absent: the send was declined
        assert holes.unledgered == 1    # the free ack no code records
        assert dict(holes.by_band) == {"utility": 1, "service": 1,
                                       backfill.NO_BAND: 1}
        assert holes.total == holes.billed + holes.refused + holes.unledgered

    def test_the_page_line_is_silent_on_free_service_acks(
        self, backfill: Any, owner_session: Session, clean_billing: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """THE POINT OF THE SPLIT. Twelve of the fourteen live holes are free
        service acks and one is a refusal; a detector that pages on those fires
        every day and teaches the operator to skip the whole report — §37's
        failure mode, where one false line buried seven true ones. On a scan
        with no billed hole the narrow line reads zero and nothing shouts."""
        for _ in range(12):
            _seed_receipt(owner_session, _status(
                f"wamid.{uuid.uuid4().hex}", "delivered",
                band="service", billable=False))
        _seed_receipt(owner_session, _status(
            f"wamid.{uuid.uuid4().hex}", "failed", code=131047))

        scan, applied, holes = _run(backfill, owner_session)
        backfill._report(scan, applied, apply=False)
        out = capsys.readouterr().out

        assert holes.total == 13
        assert holes.billed == 0
        assert "ACT:" not in out          # the pager stays quiet
        assert "BILLED, no row — PAGE THIS" in out
        assert "0" in out.split("BILLED, no row — PAGE THIS")[1].splitlines()[0]

    def test_the_page_line_speaks_for_a_billed_hole(
        self, backfill: Any, owner_session: Session, live_shape: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        scan, applied, _holes = _run(backfill, owner_session)
        backfill._report(scan, applied, apply=False)
        out = capsys.readouterr().out

        assert "ACT: a message Meta charged us for has no ledger row" in out
        # and it says, on the operator's screen, why nothing will reconstruct it
        assert "repair row is a guess" in out

    def test_a_non_service_band_pages_even_when_meta_says_nothing_about_money(
        self, backfill: Any, owner_session: Session, clean_billing: None
    ) -> None:
        """`billable` is missing on plenty of receipts. A band Meta charges for
        is enough on its own — the narrow line is «billable OR non-service»,
        not «billable AND band», because a marketing template with no billable
        flag is still a marketing template we cannot account for."""
        _seed_receipt(owner_session, _status(
            f"wamid.{uuid.uuid4().hex}", "delivered", band="marketing"))

        _scan, _applied, holes = _run(backfill, owner_session)

        assert holes.billed == 1
        assert holes.unledgered == 0

    def test_a_receipt_that_lost_its_pricing_later_is_still_billed(
        self, backfill: Any, owner_session: Session, clean_billing: None
    ) -> None:
        """Meta prices `delivered` and often sends `read` with no pricing at
        all. Last-wins on `billable` would un-bill the message seconds after
        charging for it, so the flag is OR-ed across receipts."""
        wamid = f"wamid.{uuid.uuid4().hex}"
        _seed_receipt(owner_session, _status(wamid, "delivered",
                                             band="utility", billable=True))
        _seed_receipt(owner_session, _status(wamid, "read"))

        _scan, _applied, holes = _run(backfill, owner_session)

        assert holes.billed == 1

    def test_no_phone_reaches_the_report(
        self, backfill: Any, owner_session: Session, live_shape: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Constant 13. This report goes to an operator channel and the
        payloads behind it are full of the customer's number and his wamids."""
        scan, applied, _holes = _run(backfill, owner_session)
        backfill._report(scan, applied, apply=False)
        out = capsys.readouterr().out

        assert _PROBE_PHONE not in out
        for wamid in live_shape.values():
            assert wamid not in out


class TestBillableParsing:
    """`status_is_billable` is total, like the two parsers it sits beside: this
    is somebody else's HTTP body and a shape nobody anticipated must land on
    the quiet line rather than raise in the middle of a sweep."""

    @pytest.mark.parametrize(("pricing", "expected"), [
        ({"billable": True}, True),
        ({"billable": False}, False),
        ({"billable": "true"}, True),
        ({"billable": "FALSE"}, False),
        ({"billable": "maybe"}, None),
        ({"billable": None}, None),
        ({"category": "service"}, None),
        ({}, None),
    ])
    def test_it_answers_for_every_shape(
        self, backfill: Any, pricing: dict[str, Any], expected: bool | None
    ) -> None:
        assert backfill.status_is_billable({"pricing": pricing}) is expected

    def test_a_missing_or_malformed_pricing_object_is_not_an_error(
        self, backfill: Any
    ) -> None:
        assert backfill.status_is_billable({}) is None
        assert backfill.status_is_billable({"pricing": "regular"}) is None


class TestTheOldArithmeticIsUnchanged:
    """The code backfill's counters must read exactly what they read before the
    hole report widened the lookup — an operator has been reading those numbers
    for weeks and a silently changed denominator is its own incident."""

    def test_a_receipt_with_no_code_is_counted_nowhere_but_the_holes(
        self, backfill: Any, owner_session: Session, clean_billing: None
    ) -> None:
        wamid = f"wamid.{uuid.uuid4().hex}"
        _ledger_row(owner_session, wamid, kind="template")
        _seed_receipt(owner_session, _status(wamid, "delivered",
                                             band="utility", billable=True))

        scan, applied, holes = _run(backfill, owner_session)

        assert applied.matched == 0          # matched is about CODES
        assert applied.category_present == 0
        assert applied.category_recoverable == 0
        assert applied.present == {wamid}    # …presence is about ROWS
        assert holes.total == 0

    def test_a_code_carrying_receipt_still_fills_and_still_counts(
        self, backfill: Any, owner_session: Session, clean_billing: None
    ) -> None:
        wamid = f"wamid.{uuid.uuid4().hex}"
        _ledger_row(owner_session, wamid)
        _seed_receipt(owner_session, _status(wamid, "failed", code=131050,
                                             band="marketing"))

        scan, applied, holes = _run(backfill, owner_session, apply=True)

        assert applied.matched == 1
        assert applied.filled == 1
        assert applied.category_recoverable == 1
        assert holes.total == 0
        row = owner_session.execute(
            select(DeliveryMessage).where(DeliveryMessage.wa_message_id == wamid)
        ).scalar_one()
        assert row.meta_error_code == 131050
        assert row.category is None          # measured, never written


# ── neither script becomes a ledger author ───────────────────────────────────


class TestNeitherScriptWrites:
    """§39's first ruling, proved as arithmetic on the table rather than as a
    promise in a docstring: A ROW WE INVENT IS WORSE THAN A HOLE WE CAN SEE."""

    def test_the_backfill_creates_no_row_for_a_hole_even_with_apply(
        self, backfill: Any, owner_session: Session, clean_billing: None
    ) -> None:
        _seed_receipt(
            owner_session,
            _status(f"wamid.{uuid.uuid4().hex}", "delivered",
                    band="utility", billable=True),
            _status(f"wamid.{uuid.uuid4().hex}", "delivered",
                    band="service", billable=False),
        )
        before = _rows(owner_session)

        _scan, _applied, holes = _run(backfill, owner_session, apply=True)

        assert holes.total == 2
        assert _rows(owner_session) == before

    def test_the_refusal_is_written_down_in_both_docstrings(
        self, backfill: Any
    ) -> None:
        """The decision is the finding, so it has to be legible to the next
        person opening either file — not inferable from an absence."""
        replay_doc = REPLAY.read_text(encoding="utf-8")
        for raw in (backfill.__doc__ or "", replay_doc):
            # whitespace-normalised: the sentence is wrapped across lines in
            # one file and not the other, and a line break is not a difference
            # of opinion.
            text = " ".join(raw.lower().split())
            assert "kind" in text and "template_name" in text
            assert "worse than a hole we can see" in text


class TestReplayNamesTheHole:
    """§39, second item: the skip is CORRECT — `_handle_status` iterates the
    rows matching the wamid, so replaying a receipt with no row does nothing,
    and teaching it to create the row would destroy the safety argument that
    it only ever moves rows forward. What was missing is that this is the one
    place in the tree that already knows the number, and it was spending it on
    a truncated tally key."""

    @staticmethod
    def _replay(*args: str, ledger: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — fixed interpreter, fixed script
            [sys.executable, str(REPLAY), "--audit-file", str(ledger), *args],
            capture_output=True, text=True, cwd=str(REPO), check=False,
        )

    def test_the_skip_is_a_named_counted_line(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        wamid = f"wamid.{uuid.uuid4().hex}"
        event_id = _seed_receipt(owner_session, _status(wamid, "delivered",
                                                        band="utility",
                                                        billable=True))
        before = _rows(owner_session)

        proc = self._replay("--apply", ledger=tmp_path / "ledger.jsonl")

        assert proc.returncode == 0, proc.stderr
        assert "no ledger row : 1" in proc.stdout
        assert "no delivery_messages row to apply it to" in proc.stdout
        # named — and pointed at the tool that counts them all
        assert "backfill_meta_error_codes.py" in proc.stdout
        # …and nothing was invented, and nothing was re-queued
        assert _rows(owner_session) == before
        owner_session.expire_all()
        row = owner_session.execute(
            select(WebhookEvent).where(WebhookEvent.id == event_id)
        ).scalar_one()
        assert row.processing_status == "ignored"

    def test_the_line_stays_zero_when_every_receipt_has_its_row(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        """It must not fire on the ordinary case, which is every other event
        this tool was written for."""
        wamid = f"wamid.{uuid.uuid4().hex}"
        _ledger_row(owner_session, wamid)
        _seed_receipt(owner_session, _status(wamid, "delivered"))

        proc = self._replay(ledger=tmp_path / "ledger.jsonl")

        assert proc.returncode == 0, proc.stderr
        assert "no ledger row : 0" in proc.stdout
        assert "backfill_meta_error_codes.py" not in proc.stdout

    def test_the_hole_line_carries_no_pii(
        self, owner_session: Session, clean_billing: None, tmp_path: Path
    ) -> None:
        wamid = f"wamid.{uuid.uuid4().hex}"
        _seed_receipt(owner_session, _status(wamid, "delivered", band="service",
                                             billable=False))

        proc = self._replay(ledger=tmp_path / "ledger.jsonl")

        assert _PROBE_PHONE not in proc.stdout
        assert wamid not in proc.stdout
