"""§20 F-OUTCOME — the only measure the product is judged by.

We knew who applied and never once learned what happened next. These tests
pin the question AND every constraint on it, because a survey that nags is
worth less than no survey at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text as sql_text

from career.cv import outcome_followup as fu
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


def test_the_question_lines_are_direction_pure() -> None:
    """The job title is usually English and would scramble the line."""
    text = fu.QUESTION_AR.format(title="Senior Data Analyst — Aramco")
    for line in text.splitlines():
        has_ar = any("؀" <= ch <= "ۿ" for ch in line)
        has_lat = any(ch.isascii() and ch.isalpha() for ch in line)
        assert not (has_ar and has_lat), line
    for _, label in fu.BUTTONS:
        assert len(label) <= 20, label      # Meta's button ceiling
