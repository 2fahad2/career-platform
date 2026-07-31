"""Which subscription is THE customer's? (closure audit, 31 July)

A tenant can hold several subscription rows: the §04 funnel upgrade leaves the
29-SAR analysis row beside the new pass, and every renewal (§16) adds one more.
Nine reads across the codebase used to take «whichever row came back first»,
and the failure was silent and expensive:

* the upgraded customer's search policy was built from the ANALYSIS row, whose
  entitlement is daily_job_limit = 0 by design — so the pass they paid for
  delivered nothing;
* `activate()` flipped that one-shot row to ACTIVE (a legal transition, so no
  error), stamping a 30-day period on it while the real pass stayed in
  ONBOARDING with a NULL period forever — invisible to the lifecycle sweep.

These tests build the ambiguity on purpose and pin the right answer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.onboarding import policy, privacy
from career.salla import renewal
from career.salla import subscriptions as sub_states

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def _tenant(session: Session) -> uuid.UUID:
    tid = uuid.uuid4()
    session.execute(sql_text(
        "INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tid), "c": f"TEN-T{uuid.uuid4().int % 100_000:05d}"})
    return tid


def _sub(session: Session, tenant_id: uuid.UUID, *, plan: str, status: str,
         period_end: datetime | None = None) -> uuid.UUID:
    sid = uuid.uuid4()
    session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency, current_period_end)"
        " VALUES (:i, :t, :p, :s, :o, :a, 'SAR', :e)"),
        {"i": str(sid), "t": str(tenant_id), "p": plan, "s": status,
         "o": f"ORD-{uuid.uuid4()}", "a": Decimal("149"), "e": period_end})
    return sid


def _cleanup(session: Session, tenant_id: uuid.UUID) -> None:
    session.rollback()
    for table in ("subscription_events", "subscriptions"):
        session.execute(sql_text(
            f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
            {"t": str(tenant_id)})
    session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                    {"t": str(tenant_id)})
    session.commit()


def test_the_pass_wins_over_the_one_shot_analysis(owner_session):
    tid = _tenant(owner_session)
    try:
        _sub(owner_session, tid, plan="cv_analysis", status=sub_states.ONBOARDING)
        pass_id = _sub(owner_session, tid, plan="basic",
                       status=sub_states.ONBOARDING)
        owner_session.commit()

        current = renewal.current_subscription(owner_session, tid)

        assert current is not None
        assert current.id == pass_id
        assert current.plan_code == "basic"
    finally:
        _cleanup(owner_session, tid)


def test_the_pass_wins_even_when_the_analysis_row_looks_more_alive(owner_session):
    """The exact live corruption: the one-shot row already got flipped to
    ACTIVE with a stamped period, the pass is still ONBOARDING."""
    tid = _tenant(owner_session)
    try:
        _sub(owner_session, tid, plan="cv_analysis", status=sub_states.ACTIVE,
             period_end=NOW + timedelta(days=30))
        pass_id = _sub(owner_session, tid, plan="professional",
                       status=sub_states.ONBOARDING)
        owner_session.commit()

        current = renewal.current_subscription(owner_session, tid)

        assert current is not None and current.id == pass_id
    finally:
        _cleanup(owner_session, tid)


def test_a_funnel_only_customer_still_has_their_analysis_subscription(owner_session):
    tid = _tenant(owner_session)
    try:
        only = _sub(owner_session, tid, plan="cv_analysis",
                    status=sub_states.ONBOARDING)
        owner_session.commit()

        current = renewal.current_subscription(owner_session, tid)

        assert current is not None and current.id == only
    finally:
        _cleanup(owner_session, tid)


def test_the_policy_builder_asks_the_journey_which_subscription_it_is(owner_session):
    """`_subscription_of` prefers the journey's own subscription_id — the
    single fact that cannot be ambiguous."""
    tid = _tenant(owner_session)
    try:
        analysis = _sub(owner_session, tid, plan="cv_analysis",
                        status=sub_states.ACTIVE,
                        period_end=NOW + timedelta(days=30))
        pass_id = _sub(owner_session, tid, plan="executive",
                       status=sub_states.ONBOARDING)
        owner_session.execute(sql_text(
            "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id,"
            " state, state_entered_at) VALUES (:i, :t, :s, 'GREETING', now())"),
            {"i": str(uuid.uuid4()), "t": str(tid), "s": str(pass_id)})
        owner_session.commit()

        chosen = policy._subscription_of(owner_session, tid)

        assert chosen.id == pass_id
        assert chosen.id != analysis
    finally:
        owner_session.rollback()
        owner_session.execute(sql_text(
            "DELETE FROM onboarding_sessions WHERE tenant_id = :t"),
            {"t": str(tid)})
        owner_session.commit()
        _cleanup(owner_session, tid)


def test_the_data_export_names_the_plan_the_customer_is_on(owner_session):
    tid = _tenant(owner_session)
    try:
        _sub(owner_session, tid, plan="cv_analysis", status=sub_states.ACTIVE)
        _sub(owner_session, tid, plan="professional", status=sub_states.ACTIVE,
             period_end=NOW + timedelta(days=30))
        owner_session.commit()

        bundle = privacy.export_bundle(owner_session, tenant_id=tid)

        assert bundle["subscription"]["plan_code"] == "professional"
    finally:
        _cleanup(owner_session, tid)


def test_an_active_analysis_row_never_enrols_a_tenant_in_the_nightly_run(
    owner_session,
):
    """migration 0013: the analysis product entitles no daily search. An
    ACTIVE row for it must not put the tenant into tonight's families."""
    from career.engine.families import derive_query_families

    tid = _tenant(owner_session)
    try:
        _sub(owner_session, tid, plan="cv_analysis", status=sub_states.ACTIVE,
             period_end=NOW + timedelta(days=30))
        owner_session.commit()

        families = derive_query_families(owner_session, tenant_ids=[tid])

        assert families == []
    finally:
        _cleanup(owner_session, tid)
