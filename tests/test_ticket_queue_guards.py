"""The support queue, read the way an operator actually reads it.

Three defects an adversarial review proved on 2026-08-10, each with a customer
on the other end of it, and each pinned here by a test that FAILS on the code
as it was:

* **D4 — the derivation ran backwards.** `career/support.py` promised that a
  fourth status invented by «an author who forgets this file» would show up as
  NOISE on the operator's screen. It could not: an author who forgets the file
  does not add his word to ``ALL_STATUSES``, ``QUEUED_STATUSES`` was derived
  from it, and the screen selected `status.in_(QUEUED_STATUSES)`. So his
  ticket was filtered off the queue, the 48-hour sweep never touched it (it
  selects the MUTING set) and its own close button called it «مغلقة أصلًا» —
  paged once at write time, then invisible to every reader.
* **D5 — the queue had no tiebreaker and ties are manufactured.**
  `whatsapp.worker.process_pending_whatsapp` captures ONE ``now`` for a batch
  of up to 100 events and the escalation paths write ``created_at=now``, so
  two customers escalating in one worker pass carry byte-identical stamps.
  Ordered by `created_at` alone, a tie group comes back in whatever order the
  storage feels like — and that order CHANGES when an update rewrites one of
  the tuples, which moves the page-1/page-2 boundary under the operator.
* **the owed reader** — `views._support_history_lines` was written, tested and
  dead, because the only builder of the customer-card dict never carried the
  key. «هل تعامل معه أحد من قبل؟» had no answer anywhere on the console.

These are DB tests rather than unit tests on purpose: every one of them is a
claim about what Postgres hands back, and D5 in particular cannot be made in
Python at all — the whole defect is that the storage layer is free to choose.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.support import CLOSED_STATUSES, OPEN
from career.telegram import console, views

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


@pytest.fixture()
def tenant(owner_session: Session) -> Iterator[tuple[uuid.UUID, uuid.UUID, str]]:
    """One tenant + one WhatsApp channel, removed afterwards (CASCADE takes the
    support events with it). The TEN code is unique per run so a test that
    leaves a row behind cannot silently pass by matching another one's."""
    tenant_id, channel_id = uuid.uuid4(), uuid.uuid4()
    code = f"TEN-T{uuid.uuid4().hex[:4].upper()}"
    owner_session.execute(
        sql_text("INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tenant_id), "c": code},
    )
    owner_session.execute(
        sql_text(
            "INSERT INTO customer_channels (id, tenant_id, provider, phone_e164)"
            " VALUES (:i, :t, 'whatsapp', :p)"
        ),
        {"i": str(channel_id), "t": str(tenant_id),
         "p": f"+9665{uuid.uuid4().int % 10**8:08d}"},
    )
    owner_session.commit()
    try:
        yield tenant_id, channel_id, code
    finally:
        owner_session.rollback()
        owner_session.execute(sql_text("DELETE FROM tenants WHERE id = :i"),
                              {"i": str(tenant_id)})
        owner_session.commit()


def _insert_ticket(
    session: Session,
    *,
    ticket_id: uuid.UUID,
    tenant_id: uuid.UUID,
    channel_id: uuid.UUID,
    status: str,
    created_at: datetime,
) -> uuid.UUID:
    """A ticket row, written straight to the table.

    Raw SQL rather than a production path, for the one case the production
    paths cannot produce: a status NOBODY has invented yet. That is the whole
    subject of D4 — the word this codebase has never heard of — and there is
    no writer for it by construction. Everything else in this file that could
    go through the real code does (the close below is the operator's own
    button, not an UPDATE).
    """
    session.execute(
        sql_text(
            "INSERT INTO support_events (id, tenant_id, channel_id, kind,"
            " status, created_at) VALUES (:i, :t, :c, 'support_request', :s, :d)"
        ),
        {"i": str(ticket_id), "t": str(tenant_id), "c": str(channel_id),
         "s": status, "d": created_at},
    )
    session.commit()
    return ticket_id


def _listed_ids(session: Session, *, now: datetime) -> list[uuid.UUID]:
    """Every ticket the operator can actually reach, in the order he sees it.

    Walked page by page through the same «التالي» button he taps, because the
    screen is paginated and «is it on the queue» means «is it on SOME page» —
    a ticket that exists only in the database is exactly the failure being
    tested. Ticket ids are read off the close buttons: the body carries TEN
    codes only (§15.13), and the button is where the row identifies itself.
    """
    seen: list[uuid.UUID] = []
    page = 0
    while page < 50:
        _text, keyboard = console._tickets_screen(session, now=now, page=page)
        rows = [button for row in keyboard for button in row]
        seen += [
            uuid.UUID(button[1].split("|")[2])
            for button in rows if button[1].startswith("v1|tclose|")
        ]
        if not any(button[1] == f"v1|tickets|{page + 1}" for button in rows):
            return seen
        page += 1
    raise AssertionError("the tickets screen never stopped paging")


def _close_through_the_button(
    session: Session, *, ticket_id: uuid.UUID, now: datetime
) -> str:
    """Close a ticket the way the operator does: confirm card → nonce → run.

    Not an UPDATE. «resolved» is the one claim in this system that only a human
    may make, and a state a test conjures with SQL is a state that was never
    really tested — `telegram/console` says both in its own words.
    """
    card = console._ticket_close_card(session, subject=f"{ticket_id}|0", now=now)
    assert card is not None, "the confirm card refused a ticket that exists"
    _text, keyboard = card
    nonce = next(
        button[1].split("|")[2]
        for row in keyboard for button in row
        if button[1].startswith("v1|tdone|")
    )
    answer = console._run_ticket_close(session, nonce=nonce, now=now)
    assert answer is not None, "the close nonce was refused"
    return answer[0]


def _status_of(session: Session, ticket_id: uuid.UUID) -> str:
    return str(session.execute(
        sql_text("SELECT status FROM support_events WHERE id = :i"),
        {"i": str(ticket_id)},
    ).scalar_one())


def test_a_status_nobody_invented_yet_still_reaches_the_operator(
    owner_session: Session, tenant: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    """D4. A word `career.support` has never heard of belongs ON the screen.

    This is the promise that module makes in prose and used to break in SQL.
    The asymmetry it argues from is real — noise the operator can see and
    complain about, versus a customer nobody is told about — but only the
    NEGATIVE question delivers it, and the queue was asking the positive one.

    Both halves are checked here, because the screen and the button under it
    have to agree about every possible word: a listed ticket whose own button
    answers «مغلقة أصلًا» is the same contradiction in a smaller place.
    """
    tenant_id, channel_id, code = tenant
    ticket_id = _insert_ticket(
        owner_session, ticket_id=uuid.uuid4(), tenant_id=tenant_id,
        channel_id=channel_id, status="snoozed",
        created_at=NOW - timedelta(days=3),
    )

    assert ticket_id in _listed_ids(owner_session, now=NOW), (
        "a ticket in a status nobody added to career/support.py is invisible "
        "on the operator's queue — the screen says «no open tickets» over a "
        "customer who has been waiting three days"
    )
    answer = _close_through_the_button(
        owner_session, ticket_id=ticket_id, now=NOW)
    assert code in answer
    assert "مغلقة أصلًا" not in answer, (
        "the close button refused a ticket no human had ever dealt with"
    )
    assert _status_of(owner_session, ticket_id) in CLOSED_STATUSES
    assert ticket_id not in _listed_ids(owner_session, now=NOW)


def test_the_queue_is_totally_ordered_when_the_clock_ties(
    owner_session: Session, tenant: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    """D5. Twelve tickets, one timestamp — the shape the worker manufactures.

    `process_pending_whatsapp` captures one ``now`` for a batch of up to 100
    events, so a tie is not a coincidence here; it is the normal case for
    anything that escalates in the same pass. The rows are INSERTED in
    descending id order so that «whatever the storage feels like» (physical
    order, which is insertion order on a fresh heap) is the exact opposite of
    the answer a total order gives — a tiebreaker that is only usually right
    would pass this by luck one time in 12!.

    Then one row is rewritten, which is what a close or a release does, and
    the order is asked again: an UPDATE moves the new tuple to the end of the
    heap, and with `created_at` alone that re-cuts the page boundary under an
    operator who is reading page two. Nobody has constructed a sequence where
    he demonstrably SKIPS a ticket, and this test does not claim one — it
    claims the order is the same before and after, which is cheap and is the
    only property the pagination was ever entitled to assume.
    """
    tenant_id, channel_id, _code = tenant
    same_moment = NOW - timedelta(days=1)
    ids = sorted(uuid.uuid4() for _ in range(12))
    for ticket_id in reversed(ids):
        _insert_ticket(
            owner_session, ticket_id=ticket_id, tenant_id=tenant_id,
            channel_id=channel_id, status=OPEN, created_at=same_moment,
        )

    mine = [t for t in _listed_ids(owner_session, now=NOW) if t in set(ids)]
    assert mine == ids, (
        "tickets sharing one created_at came back in storage order, not in a "
        "total order — the page boundary through this tie group is whatever "
        "Postgres last did to the heap"
    )

    # the rewrite a close or a release performs, without performing either:
    # `kind = kind` moves the tuple and touches nothing the screen reads.
    owner_session.execute(
        sql_text("UPDATE support_events SET kind = kind WHERE id = :i"),
        {"i": str(ids[0])},
    )
    owner_session.commit()
    after = [t for t in _listed_ids(owner_session, now=NOW) if t in set(ids)]
    assert after == ids, "an UPDATE re-ordered the queue under the operator"


def test_the_customer_card_answers_whether_anybody_dealt_with_him(
    owner_session: Session, tenant: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    """The owed half: `views._support_history_lines` had no data to render.

    A closed ticket leaves the queue by design — that is what the queue is for
    — and it left «هل تعامل معه أحد من قبل؟» with no answer anywhere on this
    console: the operator hears «راسلتكم وما رد أحد», opens the card, and
    cannot tell a ticket he closed last week from one that was never raised.
    `resolved_at` has held that answer since the close button shipped and
    nothing read it.

    The OPEN ticket in this test is not decoration: the count is «tickets we
    closed», so a customer who is waiting right now must not be reported as
    one who was already dealt with.
    """
    tenant_id, channel_id, code = tenant
    closed_at = NOW - timedelta(days=2)
    ticket_id = _insert_ticket(
        owner_session, ticket_id=uuid.uuid4(), tenant_id=tenant_id,
        channel_id=channel_id, status=OPEN,
        created_at=closed_at - timedelta(hours=1),
    )
    _close_through_the_button(owner_session, ticket_id=ticket_id, now=closed_at)
    _insert_ticket(
        owner_session, ticket_id=uuid.uuid4(), tenant_id=tenant_id,
        channel_id=channel_id, status=OPEN, created_at=NOW - timedelta(hours=2),
    )

    card = console._tenant_card(owner_session, code=code, now=NOW)
    assert card is not None
    assert card["support_tickets"] == {"closed": 1, "last_closed_days": 2}, (
        "the customer card carries no support history, so the reader in "
        "telegram/views renders nothing and the question stays unanswered"
    )
    text, _keyboard = views.render_tenant_card(card)
    assert "تذاكر دعم أغلقناها" in text
    assert "آخرها منذ ٢ يوم" in text
