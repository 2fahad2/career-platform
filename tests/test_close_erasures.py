"""The three things `cv/close` destroys that it has no right to.

The first two are the DELETE that landed on 2026-08-10 (CHANGELOG 38) to stop a
retired kind billing forever. Retiring is right; the two paths into it were not
guarded:

* **another rollup's row.** ``fresh`` is read in one statement and the DELETE
  runs in a later one, so under READ COMMITTED the DELETE removes rows a
  concurrent rollup committed in between — the send-time rollup and the nightly
  reach the same ``(tenant, day)``, and the day's archive comes out of it
  missing the generation it paid for.
* **an empty answer that is not a fact about the day.** `spend_by_kind`
  returning nothing was read as «the day cost nothing»; on staging it was
  RAISING on every call for two days behind a `logger.warning`, which is the
  proof that «the derivation cannot answer» is a live state — and an empty
  answer is indistinguishable from a broken one at the moment of deleting.

The third destroys nothing in a table — it destroys an ALARM. A day closed
after a crash that had already delivered is `LEDGER_FAILED` (CHANGELOG 39), and
writing suppression for it would make the operator's re-run find nothing, close
`NO_MATCHES`, and overwrite that state, which `_outranks` does not protect.

Every test here fails on the code as it was on 2026-08-10 evening and passes
after. NO THREADS: `pg_try_advisory_xact_lock` never waits, so the interleaving
is built by hand and there is no timing window to lose (the same rule as
`test_promises`' two sweep races).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.cv import close

NOW = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
DAY = date(2026, 7, 31)

#: Meta's live Saudi rate for the marketing band, `config.py`'s default since
#: 2026-08-08. `welcome_activation` is one of the five templates Meta
#: re-categorised, so a row with no measured band derives marketing.
MARKETING = Decimal("0.0501")


# ── one tenant, one channel, one billed send ────────────────────────────────


def _seed_tenant(session: Session) -> uuid.UUID:
    tid = uuid.uuid4()
    session.execute(
        sql_text("INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tid), "c": f"TEN-E{uuid.uuid4().hex[:4]}"},
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
    session: Session, tid: uuid.UUID, cid: uuid.UUID, *, status: str = "delivered",
) -> str:
    wamid = f"wamid.{uuid.uuid4().hex}"
    session.execute(
        sql_text("INSERT INTO delivery_messages (id, tenant_id, channel_id,"
                 " wa_message_id, kind, template_name, status, created_at)"
                 " VALUES (:i, :t, :c, :w, 'template', 'welcome_activation',"
                 " :s, :d)"),
        {"i": str(uuid.uuid4()), "t": str(tid), "c": str(cid), "w": wamid,
         "s": status, "d": NOW},
    )
    session.commit()
    return wamid


def _kinds(session: Session, tid: uuid.UUID) -> set[str]:
    return {
        str(r.category) for r in session.execute(sql_text(
            "SELECT category FROM cost_allocations WHERE tenant_id = :t"),
            {"t": str(tid)})
    }


# ── D3: a rollup deleting the kind another rollup just committed ────────────


def test_a_concurrent_rollup_erases_the_kind_the_other_one_just_wrote(
    owner_engine: Engine, owner_session: Session, clean_billing: None,
) -> None:
    """The race, in two real transactions, built by hand.

    B is the send-time rollup (`whatsapp/delivery.py`, `whatsapp/worker.py`);
    A is the nightly (`cv/daily_run.py`), which records the day's
    `llm_generation` and rolls the day up. A commits INSIDE the window between
    B's read and B's DELETE — the upsert loop, which is the whole exposed
    window and is milliseconds wide — and B's keep-set predates it. B then
    deletes the generation cost of a day that really did generate a CV.

    The interleaving is driven from `_upsert_allocation` because that is the
    window: it is where B stands while another transaction commits. It is a
    hook, not a stub — the real function runs right after it.

    THE INVARIANT ASSERTED IS THE HONEST ONE: no rollup may delete a kind
    another rollup COMMITTED. The fix satisfies it by making A stand down
    rather than by making A survive, so the second half of the test says what
    standing down costs and that it is only staleness — the next uncontended
    rollup restores the day in full.
    """
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    _seed_message(owner_session, tid, cid)

    nightly = Session(owner_engine)
    committed_by_a: set[str] = set()
    ran_a: list[bool] = []
    real_upsert = close._upsert_allocation

    def let_the_nightly_in_then_upsert(session: Session, **kw: Any) -> None:
        # BEFORE the real upsert, not after: `uq_cost_allocations_tenant_day_
        # category` means A's INSERT would block on B's uncommitted one, and a
        # hand-built interleaving that waits is a hang, not a test.
        if not ran_a:
            ran_a.append(True)
            # A: the nightly records the CV it paid Claude for, then rolls the
            # day up and commits — all of it inside B's window.
            close.record_usage(
                nightly, tenant_id=tid, kind="llm_generation", now=NOW,
                cost_usd=Decimal("0.42"),
            )
            close.rollup_costs(nightly, tenant_id=tid, day=DAY)
            nightly.commit()
            committed_by_a.update(_kinds(nightly, tid))
        real_upsert(session, **kw)

    try:
        close._upsert_allocation = let_the_nightly_in_then_upsert  # type: ignore[assignment]
        try:
            close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
        finally:
            close._upsert_allocation = real_upsert  # type: ignore[assignment]
        owner_session.commit()
        assert ran_a, "the interleaving never happened — B upserted nothing"

        after = _kinds(owner_session, tid)
        assert committed_by_a <= after, (
            "a rollup deleted a kind another rollup had already committed: "
            f"{sorted(committed_by_a - after)} — the day paid for a CV and its "
            f"archive now reads {sorted(after)}"
        )

        # The cost of the fix, stated: whichever rollup stands down leaves the
        # pair as the other one wrote it, and the next one that reaches it
        # rebuilds the whole day. Staleness, never loss.
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
        owner_session.commit()
        assert _kinds(owner_session, tid) == {"wa_marketing", "llm_generation"}
    finally:
        nightly.close()


def test_the_rollup_that_stands_down_writes_nothing_at_all(
    owner_engine: Engine, owner_session: Session, clean_billing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Half a rollup is the defect with the roles swapped.

    A loser that still upserted would write rows the WINNER's keep-set — read
    before them — does not contain, and the winner's DELETE would erase exactly
    those. So the losing side does nothing: no upsert, no retire, one INFO line
    saying so, and the pair keeps the answer the holder is about to write.
    """
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    _seed_message(owner_session, tid, cid)

    holder = Session(owner_engine)
    try:
        assert close._rollup_lock(holder, tenant_id=tid, day=DAY), (
            "the holder could not take a lock nobody else holds"
        )
        close.record_usage(owner_session, tenant_id=tid, kind="llm_generation",
                           now=NOW, cost_usd=Decimal("0.42"))
        with caplog.at_level(logging.INFO, logger="career.cv"):
            close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
        owner_session.commit()

        assert _kinds(owner_session, tid) == set(), (
            "the contended rollup wrote a partial day instead of standing down"
        )
        assert any("standing down" in r.getMessage() for r in caplog.records)
    finally:
        holder.rollback()
        holder.close()


def test_two_rollups_in_one_transaction_are_not_a_conflict(
    owner_session: Session, clean_billing: None,
) -> None:
    """The lock is per SESSION, not per call. Every real caller rolls up more
    than one pair in one transaction (`daily_run` walks the expired deliveries,
    the console resend closes a day it just re-derived), and re-taking the same
    key inside the transaction that holds it must keep working — otherwise the
    guard would silence the ordinary path it was added to protect."""
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    _seed_message(owner_session, tid, cid)

    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    close.rollup_costs(owner_session, tenant_id=tid, day=date(2026, 7, 30))
    owner_session.commit()

    assert _kinds(owner_session, tid) == {"wa_marketing"}


# ── D4: an empty answer is not automatically a fact about the day ───────────


def test_a_derivation_that_answers_nothing_may_not_retire_the_day(
    owner_session: Session, clean_billing: None, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third cause of «nothing», the one the branch did not know about.

    The live shape is `whatsapp_spend` failing on a host whose
    `delivery_messages` has no `category` column — there it RAISES, which never
    reaches the delete. This is the same break one step quieter: a derivation
    that answers nothing while the day's billed send is sitting right there.
    The archive must survive it, and the operator must hear about it on the
    only level that leaves the box.
    """
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    _seed_message(owner_session, tid, cid)

    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()
    assert _kinds(owner_session, tid) == {"wa_marketing"}

    # the derivation stops seeing the send — the WhatsApp half is the one that
    # actually broke on staging, and the usage half is empty on this day.
    monkeypatch.setattr(close, "whatsapp_spend", lambda *a, **k: {})
    with caplog.at_level(logging.ERROR, logger="career.cv"):
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()

    assert _kinds(owner_session, tid) == {"wa_marketing"}, (
        "a broken derivation erased the archive of a day whose billed send is "
        "still in delivery_messages"
    )
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and "derivation is broken" in errors[0].getMessage(), (
        "the refusal was silent, or logged at a level the operator's harvester "
        "does not forward"
    )


def test_a_day_whose_evidence_is_really_gone_is_still_retired(
    owner_session: Session, clean_billing: None, caplog: pytest.LogCaptureFixture,
) -> None:
    """The boundary the refusal must not cross: §12 erasure.

    When the ledger rows are gone the archive has to follow the evidence it is
    derived from rather than outlive it. Nothing here is broken, so nothing is
    logged either — a refusal that fires on the honest path is noise, and noise
    is what teaches an operator to skip the line that matters.
    """
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    wamid = _seed_message(owner_session, tid, cid)

    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()

    owner_session.execute(
        sql_text("DELETE FROM delivery_messages WHERE wa_message_id = :w"),
        {"w": wamid},
    )
    owner_session.commit()
    with caplog.at_level(logging.ERROR, logger="career.cv"):
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()

    assert _kinds(owner_session, tid) == set()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_day_whose_only_send_failed_is_still_retired(
    owner_session: Session, clean_billing: None, caplog: pytest.LogCaptureFixture,
) -> None:
    """And the boundary that says the refusal reads the DERIVATION's own inputs
    rather than «are there any rows».

    A send Meta refused was never billed, so a day whose one template ends
    `failed` costs nothing — truthfully, with the row still sitting there. A
    guard that asked «does this day have any delivery_messages» would refuse to
    retire it and leave a $0.0501 marketing charge in the archive of a message
    that never arrived, which is the over-statement CHANGELOG 38 removed.
    """
    tid = _seed_tenant(owner_session)
    cid = _seed_channel(owner_session, tid)
    wamid = _seed_message(owner_session, tid, cid)

    close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()
    assert _kinds(owner_session, tid) == {"wa_marketing"}

    owner_session.execute(
        sql_text("UPDATE delivery_messages SET status = 'failed'"
                 " WHERE wa_message_id = :w"), {"w": wamid},
    )
    owner_session.commit()
    with caplog.at_level(logging.ERROR, logger="career.cv"):
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
    owner_session.commit()

    assert _kinds(owner_session, tid) == set()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ── D5: the close that would have suppressed a day whose ledger was lost ────


def test_a_close_that_knows_the_ledger_is_lost_writes_no_suppression(
    owner_session: Session, clean_billing: None, caplog: pytest.LogCaptureFixture,
) -> None:
    """CHANGELOG 39, through the one door instead of around it.

    The sends are not undoable and the rows that record them are, so the crash
    close knows something this function could not be told: the delivery landed
    and its ledger write did not. Two things have to come out of that, and they
    pull in opposite directions through the same argument — the state must be
    `LEDGER_FAILED` **with the real counts** (`delivered: 1` is what the
    operator can act on), and NOTHING may be suppressed, because a suppressed
    LEDGER_FAILED day makes his re-run find nothing and close `NO_MATCHES` over
    the alarm.

    So `delivered_groups` still carries the truth and the suppressor is simply
    not called. The coupling is decided here rather than at each caller: a
    caller that forgets it clears an alarm silently, and nothing downstream
    would ever say so.
    """
    tid = _seed_tenant(owner_session)
    suppressed: list[str] = []

    def suppressor(session: Session, **kw: Any) -> None:
        suppressed.append(str(kw["url"]))

    with caplog.at_level(logging.ERROR, logger="career.cv"):
        state = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=6, cv_resolved=1, cv_failed=0,
            delivered_groups=["https://example.test/job/1"], failed_groups=[],
            suppressor=suppressor, ledger_ok=False,
        )
    owner_session.commit()

    assert state.state == "LEDGER_FAILED"
    assert state.counts["delivered"] == 1, (
        "the counts were manufactured — a LEDGER_FAILED day with delivered: 0 "
        "sends the operator into a generation pipeline that worked"
    )
    assert suppressed == [], (
        "the day whose ledger was lost suppressed its jobs anyway: the re-run "
        "now finds nothing, closes NO_MATCHES and overwrites the alarm"
    )
    assert any("UNSUPPRESSED" in r.getMessage() for r in caplog.records), (
        "the deliberate cost was paid silently"
    )


def test_no_caller_can_certify_a_ledger_this_function_watched_fail(
    owner_session: Session, clean_billing: None,
) -> None:
    """The two sources are AND-ed, never replaced. `ledger_ok=True` is «I know
    of no upstream loss» and says nothing about the write this function is
    about to attempt — so the default value, which every existing caller uses,
    cannot turn a failed suppression into a delivered day."""
    tid = _seed_tenant(owner_session)

    def broken(session: Session, **kw: Any) -> None:
        raise RuntimeError("suppression ledger is down")

    state = close.close_tenant_day(
        owner_session, tenant_id=tid, run_date=DAY, now=NOW,
        discovery_ok=True, gate_passes=6, cv_resolved=1, cv_failed=0,
        delivered_groups=["https://example.test/job/1"], failed_groups=[],
        suppressor=broken, ledger_ok=True,
    )
    owner_session.commit()

    assert state.state == "LEDGER_FAILED"
