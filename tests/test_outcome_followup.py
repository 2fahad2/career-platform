"""§20 F-OUTCOME — the only measure the product is judged by.

We knew who applied and never once learned what happened next. These tests
pin the question AND every constraint on it, because a survey that nags is
worth less than no survey at all.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event, select
from sqlalchemy import text as sql_text

from career.cv import outcome_followup as fu
from career.db.models import OutcomeEvent
from career.whatsapp.client import FakeWhatsAppClient

NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=15)


def _customer(session, *, last_inbound=NOW, opted_out=False):  # noqa: ANN001
    tid = uuid.uuid4()
    session.execute(sql_text("INSERT INTO tenants (id, code) VALUES (:i, :c)"),
                    {"i": str(tid), "c": f"TEN-O{uuid.uuid4().int % 100_000:05d}"})
    session.execute(sql_text(
        "INSERT INTO customer_channels (id, tenant_id, provider, phone_e164,"
        " last_inbound_at, opt_out_at) VALUES (:i, :t, 'whatsapp', :p, :l, :o)"),
        {"i": str(uuid.uuid4()), "t": str(tid),
         "p": f"+96650{uuid.uuid4().int % 10_000_000:07d}",
         "l": last_inbound, "o": NOW if opted_out else None})
    return tid


def _applied(session, tid, job="https://jobs.example/a", when=LONG_AGO):  # noqa: ANN001
    session.execute(sql_text(
        "INSERT INTO outcome_events (id, tenant_id, job_ref, outcome,"
        " occurred_at) VALUES (:i, :t, :j, 'applied', :w)"),
        {"i": str(uuid.uuid4()), "t": str(tid), "j": job, "w": when})
    return job


def _cleanup(session, *tids):  # noqa: ANN001
    session.rollback()
    for tid in tids:
        for t in ("outcome_events", "delivery_messages", "customer_channels"):
            session.execute(sql_text(
                f"DELETE FROM {t} WHERE tenant_id = :t"), {"t": str(tid)})  # noqa: S608
        session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                        {"t": str(tid)})
    session.commit()


def test_two_weeks_after_applying_the_customer_is_asked(owner_session):
    tid = _customer(owner_session)
    _applied(owner_session, tid)
    owner_session.commit()
    wa = FakeWhatsAppClient()
    try:
        counts = fu.sweep_outcome_questions(
            owner_session, whatsapp_client=wa, now=NOW)
        owner_session.commit()

        assert counts["asked"] == 1
        assert [m.kind for m in wa.sent] == ["interactive"]
        assert "وش صار" in (wa.sent[0].body or "")
    finally:
        _cleanup(owner_session, tid)


def test_nobody_is_asked_twice_ever(owner_session):
    """A question that repeats is a nuisance, and a nuisance costs more than
    the datum is worth."""
    tid = _customer(owner_session)
    _applied(owner_session, tid)
    owner_session.commit()
    wa = FakeWhatsAppClient()
    try:
        fu.sweep_outcome_questions(owner_session, whatsapp_client=wa, now=NOW)
        owner_session.commit()
        again = fu.sweep_outcome_questions(
            owner_session, whatsapp_client=wa, now=NOW + timedelta(days=1))
        owner_session.commit()

        assert again["asked"] == 0
        assert len(wa.sent) == 1
    finally:
        _cleanup(owner_session, tid)


def test_it_never_costs_a_paid_template(owner_session):
    """Outside the free window we WAIT — a survey is never worth a template,
    and a question at a bad hour is worse than no question."""
    tid = _customer(owner_session, last_inbound=NOW - timedelta(hours=30))
    _applied(owner_session, tid)
    owner_session.commit()
    wa = FakeWhatsAppClient()
    try:
        counts = fu.sweep_outcome_questions(
            owner_session, whatsapp_client=wa, now=NOW)
        owner_session.commit()

        assert counts["waiting_window"] == 1
        assert not wa.sent
    finally:
        _cleanup(owner_session, tid)


def test_someone_who_stopped_messages_is_never_asked(owner_session):
    tid = _customer(owner_session, opted_out=True)
    _applied(owner_session, tid)
    owner_session.commit()
    wa = FakeWhatsAppClient()
    try:
        fu.sweep_outcome_questions(owner_session, whatsapp_client=wa, now=NOW)
        owner_session.commit()
        assert not wa.sent
    finally:
        _cleanup(owner_session, tid)


def test_a_stale_question_is_closed_instead_of_asked_forever(owner_session):
    """The answer's value decays. After thirty days the row is closed so it is
    not re-examined every hour for the rest of time."""
    tid = _customer(owner_session, last_inbound=NOW - timedelta(hours=30))
    job = _applied(owner_session, tid, when=NOW - timedelta(days=40))
    owner_session.commit()
    wa = FakeWhatsAppClient()
    try:
        counts = fu.sweep_outcome_questions(
            owner_session, whatsapp_client=wa, now=NOW)
        owner_session.commit()

        assert counts["gave_up"] == 1
        assert not wa.sent
        stages = fu._stages(owner_session, tid, job)
        assert fu.GAVE_UP in stages
        # and it is never looked at again
        assert fu.due_applications(owner_session, now=NOW) == []
    finally:
        _cleanup(owner_session, tid)


def test_only_one_question_is_open_at_a_time(owner_session):
    """The buttons carry the ANSWER, not the job — a second open question
    would make the next tap ambiguous."""
    tid = _customer(owner_session)
    _applied(owner_session, tid, job="https://jobs.example/one")
    _applied(owner_session, tid, job="https://jobs.example/two")
    owner_session.commit()
    wa = FakeWhatsAppClient()
    try:
        counts = fu.sweep_outcome_questions(
            owner_session, whatsapp_client=wa, now=NOW)
        owner_session.commit()

        assert counts["asked"] == 1
        assert len(wa.sent) == 1
    finally:
        _cleanup(owner_session, tid)


def test_the_answer_becomes_the_next_stage_on_that_job(owner_session):
    tid = _customer(owner_session)
    job = _applied(owner_session, tid)
    owner_session.commit()
    wa = FakeWhatsAppClient()
    try:
        fu.sweep_outcome_questions(owner_session, whatsapp_client=wa, now=NOW)
        owner_session.commit()

        pending = fu.pending_job_ref(owner_session, tenant_id=tid)
        assert pending == job

        assert fu.parse_answer("oc_interview") == fu.INTERVIEW
        thanks = fu.record_answer(
            owner_session, tenant_id=tid, job_ref=job,
            outcome=fu.INTERVIEW, now=NOW)
        owner_session.commit()

        assert "توفيق" in thanks
        stages = fu._stages(owner_session, tid, job)
        assert stages == {"applied", fu.ASKED, fu.INTERVIEW}
        # answered → the thread is closed, nothing pending
        assert fu.pending_job_ref(owner_session, tenant_id=tid) is None
    finally:
        _cleanup(owner_session, tid)


def test_a_typed_answer_is_an_answer_and_not_a_complaint() -> None:
    """The customer who types «ما ردّوا» instead of tapping.

    He scrolled past the card, or his client rendered the buttons as plain
    text. Before this his sentence fell through every branch and opened a
    لمّاح+ direct-line ticket against him — our own question became a
    complaint, and the §20 datum that cannot be re-collected was thrown away.
    """
    for _button_id, label in fu.BUTTONS:
        assert fu.parse_answer(label, question_open=True) is not None
    assert fu.parse_answer("ما ردّوا", question_open=True) == fu.NO_REPLY
    assert fu.parse_answer("ما ردوا", question_open=True) == fu.NO_REPLY
    # the emoji is decoration on the card, not part of what a person types
    assert fu.parse_answer("جاني مقابلة", question_open=True) == fu.INTERVIEW
    assert fu.parse_answer("اعتذروا", question_open=True) == fu.REJECTED
    # a machine id needs no OPEN QUESTION — it needs a tap, which is a
    # different permission and a separate test below
    assert fu.parse_answer("oc_interview", tapped=True) == fu.INTERVIEW


def test_a_typed_label_never_eats_a_sentence_or_answers_an_unasked_question(
) -> None:
    """The trap this file's neighbours fell into twice: an ordinary Arabic
    word read as a command. The label must be the WHOLE message, and the
    question must actually be open."""
    # nothing pending → the sentence stays the customer's message
    assert fu.parse_answer("ما ردّوا") is None
    assert fu.parse_answer("اعتذروا") is None
    # pending, but this is a sentence that merely CONTAINS the label
    for text in (
        "ما ردّوا عليّ من الشركة اللي رشحتوها لي، وش تنصحني؟",
        "اعتذروا لي عن الموعد وأبي أعرف وش أسوي",
        "جاني مقابلة بكرة وأبي أجهز لها",
        "لا ما ردّوا",
    ):
        assert fu.parse_answer(text, question_open=True) is None, text
    assert fu.parse_answer(None, question_open=True) is None
    assert fu.parse_answer("", question_open=True) is None


def test_a_machine_id_the_customer_TYPED_is_not_an_answer() -> None:
    """ADVERSARIAL 2026-08-07. The file justified reading machine ids
    unconditionally with «it can only have come from a card we ourselves
    sent», and that construction does not exist. `whatsapp.worker` passes
    ``effective = _button_id_of(msg) or text_body``, so on a plain text
    message the argument IS what the customer typed. Someone who types
    «oc_interview» while a question is open had it written through
    :func:`record_answer` — a §20 datum that can never be re-collected,
    invented from a typed string.

    Only the caller can tell the two apart, so only the caller may say.
    """
    for button_id in fu._ANSWERS:
        # typed, question open — the worst case, and the reason for the fix
        assert fu.parse_answer(
            button_id, question_open=True, tapped=False) is None, button_id
        assert fu.parse_answer(button_id, tapped=False) is None, button_id
        # tapped — unchanged, and still independent of an open question,
        # because a tap on an old card is still our own card coming back
        assert fu.parse_answer(button_id, tapped=True) is not None, button_id
        assert fu.parse_answer(
            button_id, question_open=True, tapped=True) is not None, button_id
    # `tapped` gates the IDS only: a real typed label is still an answer
    assert fu.parse_answer(
        "ما ردّوا", question_open=True, tapped=False) == fu.NO_REPLY
    # …and a tapped button whose payload carried the human label instead of
    # the id (message_type="button") is read by the same typed rule
    assert fu.parse_answer(
        "اعتذروا", question_open=True, tapped=True) == fu.REJECTED


def test_the_call_site_that_has_not_been_taught_still_works_and_says_so(
    caplog: Any,
) -> None:
    """DELETE THIS WITH THE ``tapped is None`` ARM.

    `whatsapp.worker` does not pass ``tapped`` yet and it has another owner,
    so the old reading is kept alive for it rather than left as a red tree.
    It logs every time it is used, because a transitional default that is
    silent is a transitional default that becomes permanent.
    """
    with caplog.at_level(logging.WARNING, logger="career.cv"):
        assert fu.parse_answer("oc_interview") == fu.INTERVIEW
    said = [r.getMessage() for r in caplog.records]
    assert any("without the caller stating" in m for m in said), said
    # no id, no phone, no body ever goes to the log (§15.13)
    assert not any("oc_interview" in m for m in said), said

    # and it is silent about everything that is not an unattested id
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="career.cv"):
        assert fu.parse_answer("ما ردّوا", question_open=True) == fu.NO_REPLY
        assert fu.parse_answer("oc_interview", tapped=True) == fu.INTERVIEW
        assert fu.parse_answer("oc_interview", tapped=False) is None
    assert caplog.records == [], [r.getMessage() for r in caplog.records]


def test_the_question_lines_are_direction_pure() -> None:
    """The job title is usually English and would scramble the line."""
    text = fu.QUESTION_AR.format(title="Senior Data Analyst — Aramco")
    for line in text.splitlines():
        has_ar = any("؀" <= ch <= "ۿ" for ch in line)
        has_lat = any(ch.isascii() and ch.isalpha() for ch in line)
        assert not (has_ar and has_lat), line
    for _, label in fu.BUTTONS:
        assert len(label) <= 20, label      # Meta's button ceiling


# ── the open question, and what it costs to ask ─────────────────────────────
# `pending_job_ref` is no longer a sweep helper. It runs on EVERY inbound
# message (whatsapp/worker resolves the open question before parsing a typed
# label, so a sentence can never be read as an answer to a question nobody
# asked). That put a 1+N walk of the customer's whole ledger on the message
# path — and returning early only when a question IS open made the common case
# (nothing pending, most messages) the expensive one. These two tests pin the
# cost and the meaning separately, because a cheaper query that answers a
# slightly different question either writes a §20 datum from nothing or drops
# a real answer, and neither can be collected a second time.


class _Statements:
    """Every statement the engine actually sends, counted around a block."""

    def __init__(self, session) -> None:  # noqa: ANN001
        self._engine = session.get_bind()
        self.sent: list[str] = []

    def _note(self, conn, cursor, statement, params, context,  # noqa: ANN001
              executemany) -> None:  # noqa: ANN001
        self.sent.append(statement)

    def __enter__(self) -> _Statements:
        event.listen(self._engine, "before_cursor_execute", self._note)
        return self

    def __exit__(self, *exc: Any) -> None:
        event.remove(self._engine, "before_cursor_execute", self._note)


def _event(session, tid, job, outcome, days_ago):  # noqa: ANN001
    """One row on the ledger. `occurred_at` is SUPPLIED, never `now()` — the
    ledger is append-only and a replayed webhook or a backfill can land an
    answer with an earlier stamp than the question it answers."""
    session.execute(sql_text(
        "INSERT INTO outcome_events (id, tenant_id, job_ref, outcome,"
        " occurred_at) VALUES (:i, :t, :j, :o, :w)"),
        {"i": str(uuid.uuid4()), "t": str(tid), "j": job, "o": outcome,
         "w": NOW - timedelta(days=days_ago)})


def _pending_by_walking(session, tenant_id):  # noqa: ANN001
    """`pending_job_ref` EXACTLY as it stood before the one-query rewrite.

    Kept here as the ORACLE, not as a spare implementation: the question it
    answers is real logic — which asked question is still pending — and a
    GROUP BY that gets it subtly wrong is invisible until a customer is asked
    something he already answered, or his answer is dropped on the floor. The
    two are compared over the corpus below rather than reasoned about.
    """
    asked = session.execute(
        select(OutcomeEvent.job_ref, OutcomeEvent.occurred_at).where(
            OutcomeEvent.tenant_id == tenant_id,
            OutcomeEvent.outcome == fu.ASKED,
        ).order_by(OutcomeEvent.occurred_at.desc())
    ).all()
    for job_ref, _ in asked:
        stages = fu._stages(session, tenant_id, job_ref)
        if not (stages & {fu.INTERVIEW, fu.NO_REPLY, fu.REJECTED, fu.GAVE_UP}):
            return str(job_ref)
    return None


def test_the_open_question_is_one_query_however_long_the_history(owner_session):
    """The cost, measured where it is actually paid.

    A customer a year in has hundreds of applications on an append-only ledger
    that is never pruned, and every one of them was a query — per inbound
    message. The walk's early return does not help: it fires only when a
    question IS open, so the customer with nothing pending, writing «شكرًا»,
    paid for his entire history.
    """
    quiet = _customer(owner_session)      # long history, nothing open
    live = _customer(owner_session)       # long history + one open question
    try:
        for n in range(40):
            job = f"https://jobs.example/q{n}"
            _event(owner_session, quiet, job, "applied", 60 - n)
            _event(owner_session, quiet, job, fu.ASKED, 46 - n)
            _event(owner_session, quiet, job, fu.NO_REPLY, 40 - n)
            _event(owner_session, live, job, "applied", 60 - n)
            _event(owner_session, live, job, fu.ASKED, 46 - n)
            _event(owner_session, live, job, fu.REJECTED, 40 - n)
        _event(owner_session, live, "https://jobs.example/open", fu.ASKED, 1)
        owner_session.commit()

        with _Statements(owner_session) as counted:
            assert fu.pending_job_ref(owner_session, tenant_id=quiet) is None
        assert len(counted.sent) == 1, counted.sent

        with _Statements(owner_session) as counted:
            assert fu.pending_job_ref(owner_session, tenant_id=live) == \
                "https://jobs.example/open"
        assert len(counted.sent) == 1, counted.sent

        # and the walk it replaced, on the same rows, to show the difference
        # is real rather than an artefact of how the statements are counted
        with _Statements(owner_session) as walked:
            assert _pending_by_walking(owner_session, quiet) is None
        assert len(walked.sent) > 40
    finally:
        _cleanup(owner_session, quiet, live)


def test_the_one_query_and_the_walk_it_replaced_never_disagree(owner_session):
    """The equivalence corpus.

    Several tenants, several jobs each, asked / answered / expired / never
    asked / never applied, an answer stamped BEFORE its question, one job_ref
    shared by two tenants, and a tenant with no rows at all. Every case is
    checked twice: against the walk, and against the answer a human would give
    — so a corpus that quietly stops covering something cannot pass by
    agreeing with a broken oracle.
    """
    corpus: dict[str, tuple[list[tuple[str, str, int]], str | None]] = {
        # answered → closed; and the newer job was never asked about
        "answered": ([("a1", "applied", 20), ("a1", fu.ASKED, 14),
                      ("a1", fu.INTERVIEW, 13), ("a2", "applied", 10)], None),
        # one closed, one open, one only applied → the open one
        "one_open": ([("b1", "applied", 30), ("b1", fu.ASKED, 20),
                      ("b1", fu.NO_REPLY, 19),
                      ("b2", "applied", 25), ("b2", fu.ASKED, 15),
                      ("b3", "applied", 5)], "b2"),
        # two open at once (the sweep forbids it; the ledger can still hold
        # it) → the most recently ASKED, which is what the walk returned
        "two_open": ([("c1", fu.ASKED, 9), ("c2", fu.ASKED, 2)], "c2"),
        # the question nobody answered, closed by the sweep → nothing pending
        "expired": ([("d1", "applied", 40), ("d1", fu.ASKED, 35),
                     ("d1", fu.GAVE_UP, 28)], None),
        # never asked → nothing pending, and no row invented
        "never_asked": ([("e1", "applied", 30), ("e2", "applied", 2)], None),
        # THE ordering trap: the answer is stamped BEFORE the question. A
        # «latest row wins» reading calls this open and re-asks a customer
        # something he has already answered.
        "answer_first": ([("f1", "applied", 30), ("f1", fu.REJECTED, 20),
                          ("f1", fu.ASKED, 10)], None),
        # THE other ordering trap: a closed job with LATER activity must not
        # outrank an older question that is genuinely still open.
        "late_noise": ([("g1", fu.ASKED, 20), ("g1", fu.INTERVIEW, 19),
                        ("g1", "applied", 1), ("g2", fu.ASKED, 18)], "g2"),
        # asked twice (a re-ask that should never happen) and then answered
        "re_asked": ([("h1", fu.ASKED, 20), ("h1", fu.ASKED, 12),
                      ("h1", fu.INTERVIEW, 11), ("h2", fu.ASKED, 18)], "h2"),
        # a ledger gap — the applied row is missing and the question is real
        "asked_only": ([("i1", fu.ASKED, 3)], "i1"),
        # the rest of the ledger's vocabulary: «ignored» is `cv/deliver`'s
        # other button and is neither a question nor an answer to one. It must
        # not close a thread, and it must not open one either.
        "ignored_open": ([("j1", "ignored", 20), ("j1", fu.ASKED, 5)], "j1"),
        "ignored_only": ([("k1", "ignored", 3), ("k2", "applied", 2)], None),
        # the same job_ref in two tenants, open for one and closed for the
        # other: the grouping must not leak across the tenant boundary
        "shared_open": ([("SHARED", "applied", 20), ("SHARED", fu.ASKED, 9)],
                        "SHARED"),
        "shared_closed": ([("SHARED", "applied", 20),
                           ("SHARED", fu.ASKED, 9),
                           ("SHARED", fu.REJECTED, 8)], None),
        # the empty case
        "empty": ([], None),
    }
    tenants = {name: _customer(owner_session) for name in corpus}
    try:
        for name, (rows, _expected) in corpus.items():
            for job, outcome, days_ago in rows:
                _event(owner_session, tenants[name], job, outcome, days_ago)
        owner_session.commit()

        for name, (_rows, expected) in corpus.items():
            tid = tenants[name]
            walked = _pending_by_walking(owner_session, tid)
            grouped = fu.pending_job_ref(owner_session, tenant_id=tid)
            assert grouped == walked, f"{name}: {grouped!r} != walk {walked!r}"
            assert grouped == expected, f"{name}: {grouped!r} != {expected!r}"
    finally:
        _cleanup(owner_session, *tenants.values())
